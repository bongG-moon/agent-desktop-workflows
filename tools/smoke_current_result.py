"""Exercise current single/first result rows on an isolated native Windows grid."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zipfile import ZipFile

from smoke_lookup_trial import build_lookup_fixture
from smoke_native import Fixture
from smoke_registered_result import wait_for_idle


def exercise(package, work):
    work.mkdir(parents=True, exist_ok=False)
    skills = work / '.claude/skills'
    with ZipFile(package) as archive:
        archive.extractall(skills)
    scripts = skills / 'desktop-select/scripts'
    driver = work / 'guard'
    driver.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'tests/fixtures/registered_result_ui_driver.py',
                    driver / 'sitecustomize.py')
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work/'tasks'), LOCALAPPDATA=str(work/'local'),
               PYTHONPATH=str(driver))
    fixture = Fixture(build_lookup_fixture(work), '--provider-grid', '--row-only')
    exe = 'LookupDetailsFixture.exe'
    scope = dict(exe=exe, title='Item details', related=True, after_step=3)
    def field(role, key):
        return dict(role=role, automation_id=key, ancestors=[dict(role='group', automation_id='Filters')])
    base = dict(schema=1, name='Current result details', exe=exe, window=fixture.ready['title'], steps=[
        dict(action='set-value', selector=field('textfield', 'queryField'), parameter='Part'),
        dict(action='invoke', selector=field('button','searchButton'), command_name='VIEW'),
        dict(action='double-click-result', result_row='single', selector=dict(role='group', automation_id='ProviderResults')),
        dict(action='read-value', selector=dict(role='textfield',automation_id='CommentText'), label='Comment', window_context=scope),
        dict(action='invoke', selector=dict(role='button',automation_id='btnClose'), command_name='Exit', verify_closed=True,
             window_context=scope)])
    measurements = []
    def run(args, name, trace):
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-X','utf8','-B',*map(str,args)], cwd=work,
                                env=dict(env, DESKTOP_SELECT_SMOKE_ELEMENT_TRACE=str(trace)),
                                capture_output=True, encoding='utf8', timeout=90)
        if not result.stdout.strip():
            raise RuntimeError(result.stderr)
        report = json.loads(result.stdout)
        (work/(name+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        return report, round(time.monotonic()-started,3)
    try:
        fixture.command('focus')
        for mode in ('single','first'):
            base['steps'][2]['result_row'] = mode
            spec = work/(mode+'.json')
            spec.write_text(json.dumps(base),encoding='utf8')
            dest = skills/('current-'+mode)
            result, _ = run([scripts/'python_workflow.py','register','--recipe',spec,'--out',dest],
                             'register-'+mode,work/('register-'+mode+'.txt'))
            assert result['status']=='registered',result
            runner = dest/'scripts/run.py'
            for number, value in enumerate(('LAB-42','LAB-73','PART-A'),1):
                fixture.command(mode)
                fixture.command('reset')
                before = fixture.command('state')
                trace = work/(mode+'-'+str(number)+'.txt')
                wait_for_idle()
                result, elapsed = run([runner,'--set','Part='+value,'--allow-physical','--summary'],
                                      mode+'-'+str(number),trace)
                after = fixture.command('state')
                assert after['queries']==before['queries']+1 and after['opened']==before['opened']+1, (result,after)
                assert after['closed']==before['closed']+1 and not after['detailOpen'], (result,after)
                assert after['last']==value and not after['wrongRows'] and not after['wrongColumns'] and not after['archiveClicks'],after
                assert 'Note for '+value in json.dumps(result),result
                lines = trace.read_text(encoding='utf8').splitlines()
                assert 'worker_guard_enabled' in lines and not any('attempted' in line for line in lines),lines
                row = result['steps'][2]
                receipt = row.get('receipt',row)
                assert receipt['result_observation']['strategy']=='uia_row_found',result
                assert after['ValueReads']==before['ValueReads'],after
                measurement = dict(mode=mode,value=value,wall_seconds=elapsed,status=result['status'],
                    queries=1,opened=1,closed=1,image_calls=0,value_reads=0,key_column=after['keyColumn'],
                    wrong_rows=after['wrongRows'],wrong_columns=after['wrongColumns'],
                    observation=receipt['result_observation'])
                measurements.append(measurement)
                print(json.dumps(measurement),flush=True)
                (work/'measurements.json').write_text(json.dumps(measurements,indent=2),encoding='utf8')
        runner = skills/'current-single/scripts/run.py'
        for scenario, diagnostic in (('empty','result_row_missing'),('duplicate','result_row_ambiguous')):
            fixture.command(scenario)
            before = fixture.command('state')
            trace=work/(scenario+'.txt')
            wait_for_idle()
            result, elapsed = run([runner,'--set','Part=CHECK-'+scenario.upper(),'--allow-physical','--summary'],scenario,trace)
            after = fixture.command('state')
            assert after['queries']==before['queries']+1 and after['opened']==before['opened'],(result,after)
            row = result['steps'][-1]
            assert result['status']=='blocked' and row.get('receipt',row).get('diagnostic')==diagnostic,result
            assert not any('attempted' in line for line in trace.read_text().splitlines())
            measurement=dict(case=scenario,status=result['status'],diagnostic=diagnostic,wall_seconds=elapsed,
                             queries=1,opened=0,image_calls=0)
            measurements.append(measurement)
            print(json.dumps(measurement),flush=True)
            (work/'measurements.json').write_text(json.dumps(measurements,indent=2),encoding='utf8')
    finally:
        fixture.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('work',type=Path)
    args=parser.parse_args()
    exercise(args.package.resolve(strict=True),args.work.resolve())
