"""Exercise current single/first result rows on an isolated native Windows grid."""
import argparse
from copy import deepcopy
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


def check_coordinate_stops(fixture, recipe, runner, scripts, run, work):
    """Exercise an actual owned popup, interrupted results and recorded placement on Windows."""
    sys.path.insert(0, str(scripts))
    import win_bridge
    import window_placement
    win_bridge.set_dpi_awareness()
    original = window_placement.capture(fixture.ready['hwnd'])
    assert original and original['dpi'] > 0, original

    def call(args, case, runner=runner):
        wait_for_idle()
        before = fixture.command('state')
        trace = work / (case + '.txt')
        result, elapsed = run([runner, *args], case, trace)
        after = fixture.command('state')
        delta = tuple(after[key] - before[key] for key in ('queries', 'opened', 'closed'))
        lines = trace.read_text(encoding='utf8').splitlines() if trace.exists() else []
        assert not any('attempted' in line for line in lines), lines
        print(json.dumps(dict(case=case, elapsed=elapsed, status=result['status'], diagnostic=result.get('diagnostic'),
                              queries=delta[0], opens=delta[1], closes=delta[2])), flush=True)
        return result, delta

    # One positively identified opener, then a new process to read/close that exact HWND.
    opener, counts = call(['--only','4','--allow-physical','--summary'], 'scoped-opener')
    assert counts == (0, 1, 0) and opener['next_action']['kind'] == 'resume_detail', opener
    ticket = opener['next_action']['args']
    resumed, counts = call(ticket, 'detail-suffix')
    assert counts == (0, 0, 1) and resumed['run_id'] == opener['run_id'], resumed
    assert resumed['status'] == 'verified' and resumed['prior_steps_replayed'] is False, resumed
    assert 'Note for ' in json.dumps(resumed) and 'recorded_window_closed' in json.dumps(resumed), resumed
    spent, counts = call(ticket, 'spent-ticket')
    assert counts == (0, 0, 0) and spent['input_dispatched'] is False, spent

    # A click on an empty coordinate cannot verify a result. Missing popup blocks the suffix.
    fixture.command('empty')
    args = ['--set','Part=EMPTY','--allow-physical','--summary']
    empty, counts = call(args, 'empty-no-popup')
    assert counts == (1, 0, 0) and empty['status'] == 'blocked', empty
    assert 'result_review' not in empty and 'observations' not in empty, empty
    assert any(row.get('receipt',row).get('target_resolution') == 'explicit_table_coordinate'
               and row.get('receipt',row).get('input_dispatched') is True for row in empty['steps']), empty
    repeat, counts = call(args, 'empty-no-repeat')
    assert counts == (0, 0, 0) and repeat['input_dispatched'] is False and repeat['run_id'] == empty['run_id'], repeat
    cancelled, counts = call(repeat['cancel_args'], 'cancel-empty')
    assert counts == (0, 0, 0) and cancelled['input_dispatched'] is False, cancelled

    # A manually authored point has no placement to restore. An actual resize must block.
    fixture.command('single')
    fixture.command('resize')
    resized, counts = call(['--only','4','--allow-physical','--summary'], 'resized-coordinate')
    assert counts == (0, 0, 0) and resized.get('diagnostic') == 'recorded_result_surface_resized', resized
    assert resized['input_dispatched'] is False and 'result_review' not in resized, resized

    # The query succeeds in the resized window. Only its unsent click and the
    # opened dialog's read/close should run after restoring the original size.
    pending, counts = call(['--set', 'Part=RESUME', '--allow-physical', '--summary'], 'query-before-resize-stop')
    assert counts == (1, 0, 0) and pending.get('diagnostic') == 'recorded_result_surface_resized', pending
    assert pending['stopped_at'] == 4 and pending['steps'][-1].get('receipt', pending['steps'][-1])['input_dispatched'] is False, pending
    assert pending['next_action']['kind'] == 'resume_coordinate' and 'result_review' not in pending, pending
    resume_args = pending['next_action']['args']
    assert '--resume-input' in resume_args and '--allow-physical' in resume_args, pending
    fixture.command('resize')
    completed, counts = call(resume_args, 'unsent-coordinate-resumed')
    assert counts == (0, 1, 1) and completed['run_id'] == pending['run_id'], completed
    assert completed['status'] == 'verified' and completed['prior_steps_replayed'] is False, completed
    assert 'Note for RESUME' in json.dumps(completed) and 'recorded_window_closed' in json.dumps(completed), completed
    spent, counts = call(resume_args, 'spent-coordinate-ticket')
    assert counts == (0, 0, 0) and spent['status'] == 'blocked' and spent['input_dispatched'] is False, spent
    fixture.command('resize')

    saved = deepcopy(recipe)
    saved['steps'][3]['result_point']['window_placement'] = original
    spec = work/'coordinate-with-placement.json'
    spec.write_text(json.dumps(saved), encoding='utf8')
    dest = runner.parents[2]/'current-placement'
    registered, _ = run([scripts/'python_workflow.py','register','--recipe',spec,'--out',dest],
                          'register-placement', work/'register-placement.txt')
    assert registered['status'] == 'registered', registered
    restored, counts = call(['--set','Part=RESTORED','--allow-physical','--summary'], 'restored-placement',
                            dest/'scripts/run.py')
    assert counts == (1, 1, 1) and restored['status'] == 'verified', restored
    assert restored['steps'][3]['recorded_placement_restored'] is True, restored
    current = window_placement.capture(fixture.ready['hwnd'])
    assert all(current[key] == original[key] for key in ('rect','dpi','monitor','maximized')), (original,current)


def exercise(package, work, *, coordinate=False, check_stops=False):
    work.mkdir(parents=True, exist_ok=False)
    skills = work / ('.agents/skills' if coordinate else '.claude/skills')
    with ZipFile(package) as archive:
        archive.extractall(work if coordinate else skills)
    scripts = (work if coordinate else skills) / 'desktop-select/scripts'
    driver = work / 'guard'
    driver.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'tests/fixtures/registered_result_ui_driver.py',
                    driver / 'sitecustomize.py')
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work/'tasks'), LOCALAPPDATA=str(work/'local'),
               PYTHONPATH=str(driver))
    options = ('--group-only', '--scroll-pane', '--scoped-detail') if coordinate else ('--provider-grid', '--row-only')
    fixture = Fixture(build_lookup_fixture(work), *options)
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
        row_index = 2
        if coordinate:
            layout = fixture.command('review-layout')
            left, top = layout['groupOrigin']
            right, bottom = layout['groupEnd']
            x, y = layout['cell']
            base['steps'][2].update(selector=dict(role='group', automation_id='PaintedResults'),
                                    result_point=dict(x=x-left, y=y-top, width=right-left, height=bottom-top))
            base['steps'][3]['selector']['ancestors'] = [dict(role='group', automation_id='EquipmentInfo')]
            base['steps'].insert(2, dict(action='wait', seconds=5))
            scope.update(after_step=4, timeout=3)
            row_index = 3
        for mode in (('coordinate',) if coordinate else ('single', 'first')):
            base['steps'][row_index]['result_row'] = mode
            spec = work/(mode+'.json')
            spec.write_text(json.dumps(base),encoding='utf8')
            dest = skills/('current-'+mode)
            result, _ = run([scripts/'python_workflow.py','register','--recipe',spec,'--out',dest],
                             'register-'+mode,work/('register-'+mode+'.txt'))
            assert result['status']=='registered',result
            runner = dest/'scripts/run.py'
            if check_stops:
                check_coordinate_stops(fixture, base, runner, scripts, run, work)
                return
            values = ('D311','D613','D124') if coordinate else ('LAB-42','LAB-73','PART-A')
            for number, value in enumerate(values,1):
                if not coordinate:
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
                row = result['steps'][row_index]
                receipt = row.get('receipt',row)
                if coordinate:
                    assert receipt['target_resolution']=='explicit_table_coordinate' and receipt['value_comparison'] is False,result
                    assert result['status']=='verified',result
                else:
                    assert receipt['result_observation']['strategy']=='uia_row_found',result
                assert after['ValueReads']==before['ValueReads'],after
                measurement = dict(mode=mode,value=value,wall_seconds=elapsed,status=result['status'],
                    queries=1,opened=1,closed=1,image_calls=0,value_reads=0,key_column=after['keyColumn'],
                    wrong_rows=after['wrongRows'],wrong_columns=after['wrongColumns'],
                    observation=receipt.get('result_observation'), engine_seconds=result.get('elapsed_seconds'))
                measurements.append(measurement)
                print(json.dumps(measurement),flush=True)
                (work/'measurements.json').write_text(json.dumps(measurements,indent=2),encoding='utf8')
        if coordinate:
            return
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
    parser.add_argument('--coordinate',action='store_true')
    parser.add_argument('--check-stops',action='store_true',help='test coordinate interruption, resume and placement on Windows')
    args=parser.parse_args()
    exercise(args.package.resolve(strict=True),args.work.resolve(),coordinate=args.coordinate or args.check_stops,
             check_stops=args.check_stops)
