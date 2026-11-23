"""Run the same registered workflow on an accessible or owner-drawn Windows grid."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

from smoke_lookup_trial import build_lookup_fixture
from smoke_native import Fixture


def wait_for_idle(seconds=1.5, timeout=30):
    """Give the person on this input desktop priority over the native trial."""
    import ctypes
    from ctypes import wintypes

    class LastInput(ctypes.Structure):
        _fields_ = [('size', wintypes.UINT), ('tick', wintypes.DWORD)]
    state = LastInput(ctypes.sizeof(LastInput), 0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(state)):
            raise RuntimeError('fixture_input_state_unavailable')
        age = (ctypes.windll.kernel32.GetTickCount() - state.tick) & 0xffffffff
        if age >= seconds * 1000:
            return
        time.sleep(.2)
    raise RuntimeError('fixture_input_still_active')


def exercise(package, work, baseline, repeat, drive_review=False, separate_review=False, native_table=False,
             provider_grid=False, verify_only=False, oversized_grid=False, unknown_offscreen=False,
             duplicate_column_value=False, cover_during_review=False):
    work.mkdir(parents=True, exist_ok=False)
    skills = work / '.claude/skills'
    with ZipFile(package) as archive:
        archive.extractall(skills)
    scripts = skills/'desktop-select/scripts'
    exe = build_lookup_fixture(work)
    flags = ['--provider-grid'] if provider_grid else [] if native_table else ['--group-only']
    if oversized_grid:
        flags.append('--oversized-grid')
    if duplicate_column_value:
        flags.append('--duplicate-column-value')
    if cover_during_review:
        flags.append('--cover-during-review')
    fixture = Fixture(exe, *flags)
    title = fixture.ready['title']
    scope = dict(exe=exe.name,title='Item details',related=True,after_step=4)
    def source(role, key):
        return dict(role=role,automation_id=key,ancestors=[dict(role='group',automation_id='Filters')])
    recipe = dict(schema=1,name='Lookup details',exe=exe.name,window=title,steps=[
        dict(action='set-value', selector=source('textfield','queryField'),parameter='Part',label='Equip ID'),
        dict(action='invoke',selector=source('button','searchButton'),label='VIEW',command_name='VIEW'),
        dict(action='verify-value', selector={'role':'table'},parameter='Part',result_column='Equip ID',label='Equip ID'),
        dict(action='double-click-result',label='Open matched row'),
        dict(action='read-value',selector=dict(role='textfield',automation_id='CommentText'),label='Comment',window_context=scope),
        dict(action='invoke',selector=dict(role='button',automation_id='btnClose'),verify_closed=True,
             label='Exit',command_name='Exit',window_context=scope)])
    if verify_only:
        recipe['steps'] = recipe['steps'][:3]
    if oversized_grid:
        recipe['steps'][2]['selector'] = dict(role='group', automation_id='PaintedResults')
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work/'tasks'), LOCALAPPDATA=str(work/'local'))
    if drive_review or provider_grid:
        driver = work / 'review-driver'
        driver.mkdir()
        fixture_source = Path(__file__).resolve().parents[1] / 'tests/fixtures/registered_result_ui_driver.py'
        shutil.copyfile(fixture_source, driver / 'sitecustomize.py')
    def run(argv, name, extra_env=None):
        started = time.monotonic()
        call = subprocess.run([sys.executable,'-X','utf8','-B',*map(str,argv)],cwd=work,env=dict(env, **(extra_env or {})),
                              capture_output=True,encoding='utf8',timeout=300)
        if not call.stdout.strip():
            raise RuntimeError(call.stderr)
        report = json.loads(call.stdout)
        (work/(name+'.json')).write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf8')
        return report, round(time.monotonic()-started,3)
    try:
        fixture.command('focus')
        source_path=work/'original.json'
        source_path.write_text(json.dumps(recipe),encoding='utf8')
        dest = skills/'lookup-details'
        registered, _ = run([scripts/'python_workflow.py','register','--recipe',source_path,'--out',dest], 'registered')
        assert registered['status']=='registered', registered
        runner = dest/'scripts/run.py'
        original = (dest/'workflow.json').read_bytes()
        measurements = []
        for n in range(1,repeat+1):
            fixture.command('reset')
            before = fixture.command('state')
            value = ('LAB-42','LAB-73','PART-A')[n-1] if drive_review or provider_grid else 'LAB-42'
            cover_this_review = cover_during_review and n == 1
            args = [runner,'--set','Part='+value,'--allow-physical','--summary']
            layout = None
            if verify_only:
                args.remove('--allow-physical')
            def review_env():
                nonlocal layout
                if provider_grid:
                    return dict(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_ELEMENT_TRACE=str(trace_path))
                if not drive_review:
                    return {}
                layout = fixture.command('review-layout' if separate_review else 'review-layout-next')
                (work/('layout-'+str(n)+'.json')).write_text(json.dumps(layout), encoding='utf8')
                extra = dict(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_UI_REVIEW=json.dumps(
                    dict(layout=layout, expected=value, receipt=str(ui_receipt),
                         cover_hwnd=fixture.ready.get('coverHwnd') if cover_this_review else 0)))
                if unknown_offscreen:
                    extra['DESKTOP_SELECT_SMOKE_UNKNOWN_OFFSCREEN'] = str(work / ('offscreen-'+str(n)+'.txt'))
                return extra
            if not baseline and not separate_review and not native_table and not verify_only:
                args.append('--local-result-review')
            if drive_review:
                ui_receipt = work / ('ui-' + str(n) + '.json')
            if provider_grid:
                trace_path = work / ('element-trace-' + str(n) + '.txt')
                if not verify_only:
                    wait_for_idle()
            prefix_report = None
            if separate_review:
                prefix_report, prefix_elapsed = run(args, 'query-'+str(n), review_env())
                review_state = fixture.command('state')
                (work/('state-query-'+str(n)+'.json')).write_text(json.dumps(review_state), encoding='utf8')
                assert prefix_report.get('stopped_at')==3 and prefix_report['status']=='blocked',prefix_report
                assert review_state['queries']==before['queries']+1 and review_state['opened']==before['opened'],review_state
                continuation = prefix_report['result_review']['resume_args']
                assert continuation[0] == '--confirm-result-locally' and '--allow-physical' in continuation
                identity = os.path.normcase(str((dest / 'workflow.json').absolute()))
                key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                ticket_file = Path(tempfile.gettempdir()) / 'desktop-select-menu' / key / 'result-review.json'
                ticket_data = ticket_file.read_bytes()
                ticket = json.loads(ticket_data)
                assert value.encode() not in ticket_data and len(ticket['expected']) == len(ticket['variables']) == 64
                resume_args = list(continuation)
                if '--set' not in resume_args:
                    resume_args.extend(['--set', 'Part='+value])
                started_review = time.monotonic()
                for attempt in (1,2):
                    wait_for_idle()
                    report, _ = run([runner, *resume_args],
                                    'resume-'+str(n)+'-'+str(attempt),review_env())
                    current = fixture.command('state')
                    (work/('state-resume-'+str(n)+'-'+str(attempt)+'.json')).write_text(json.dumps(current),encoding='utf8')
                    assert current['queries']==review_state['queries'] and current['hwnd']==review_state['hwnd'], current
                    if (attempt == 1 and report.get('diagnostic')=='foreground_or_input_changed'
                            and report.get('input_dispatched') is False and current['opened']==review_state['opened']
                            and report.get('result_review',{}).get('resume_args')==continuation):
                        continue  # only the unconsumed review ticket, never the query
                    break
                assert report.get('prior_steps_replayed') is False and report.get('resumed_at')==4,report
                assert [row['number'] for row in report['steps']]==[4,5,6],report
                assert not ticket_file.exists()
                elapsed = round(prefix_elapsed + time.monotonic()-started_review,3)
            else:
                if drive_review:
                    wait_for_idle()
                report, elapsed = run(args,'run-'+str(n),review_env())
            after = fixture.command('state')
            assert after['queries']==before['queries']+1 and not after['archiveClicks'], (after, report)
            assert (dest/'workflow.json').read_bytes()==original
            if baseline:
                assert report.get('stopped_at')==3 and after['opened']==before['opened'], report
            elif verify_only:
                step = report['steps'][-1]
                proof = step.get('receipt',step)
                assert step['number'] == 3 and proof['status']=='verified' and proof['value']==value, report
                assert after['opened']==before['opened'] and not report.get('physical_input'),report
            else:
                assert after['last']==value and after['opened']==before['opened']+1 and after['closed']==before['closed']+1, (after,report)
                assert not after['detailOpen'] and after['wrongRows']==0 and ('Note for '+value) in json.dumps(report), report
            if drive_review:
                review = json.loads(ui_receipt.read_text(encoding='utf8'))
                assert review['wrong_value_rejected'] and review['cropped'] == (oversized_grid or n>1),review
                assert after['wrongColumns'] == 0, after
                assert review['covered_target'] == cover_this_review, review
                if oversized_grid:
                    assert layout['rawEnd'][0] == layout['imageSize'][0] + 107, layout
                    assert review['crop_origin'] == layout['visibleOrigin'], review
                if unknown_offscreen:
                    assert 'fixture_group_offscreen_unknown' in (work / ('offscreen-'+str(n)+'.txt')).read_text(), n
            observation = next((row.get('receipt',row).get('result_observation') for row in (prefix_report or report).get('steps',[])
                                if row['number']==3),None)
            if native_table:
                assert observation and observation['strategy'] == 'uia_matched', observation
                assert 'result_review' not in report
            if provider_grid:
                trace = trace_path.read_text(encoding='utf8').splitlines()
                assert 'worker_guard_enabled' in trace and not any('attempted' in line for line in trace), trace
                assert after['wrongColumns'] == 0 and 'result_review' not in report, (after, report)
                assert after['HeaderReads'] > before['HeaderReads'] and after['ItemReads'] > before['ItemReads'], after
                assert after['ValueReads'] > before['ValueReads'], after
                assert observation and observation.get('wait_seconds') == 0, observation
                if n > 1:
                    assert observation.get('table_lookup') == 'remembered_element', observation
            if drive_review:
                assert observation and observation['strategy']==('uia_timeout' if n==1 else 'reviewed_group_pixels'), observation
                if n>1:
                    assert observation['attempts']==1 and observation['wait_seconds']==0, observation
            result = dict(run=n,wall_seconds=elapsed,status=report['status'],queries=after['queries']-before['queries'],
                          opened=after['opened']-before['opened'],closed=after['closed']-before['closed'],
                          wrong_rows=after['wrongRows'],archive_clicks=after['archiveClicks'],result_observation=observation)
            if separate_review:
                result.update(query_seconds=prefix_elapsed, confirm_seconds=round(elapsed-prefix_elapsed,3),
                              resumed_at=report['resumed_at'], prior_steps_replayed=report['prior_steps_replayed'])
            if oversized_grid:
                result.update(native_right_overflow=107, unknown_offscreen_fault_injected=unknown_offscreen,
                              duplicate_column_value=duplicate_column_value,
                              covered_during_review=cover_this_review,
                              row=after['matchRow'], column=after['keyColumn'], query_value=value,
                              visible_region=[*layout['visibleOrigin'],
                                  *[end-start for end,start in zip(layout['visibleEnd'],layout['visibleOrigin'])]],
                              local_review_seconds=report.get('local_review_seconds'))
            if provider_grid:
                result.update(column=after['keyColumn'], row=after['matchRow'], query_value=value,
                              header_reads=after['HeaderReads']-before['HeaderReads'],
                              cell_reads=after['ItemReads']-before['ItemReads'],
                              value_reads=after['ValueReads']-before['ValueReads'], image_calls=0)
            measurements.append(result)
            (work/'measurements.json').write_text(json.dumps(measurements,indent=2),encoding='utf8')
            print(json.dumps(result),flush=True)
        if provider_grid:
            for scenario in ('missing', 'duplicate'):
                before = fixture.command(scenario)
                trace = work / (scenario+'-trace.txt')
                result, elapsed = run(
                    [runner, '--set', 'Part=CHECK-'+scenario.upper(), '--summary',
                     *([] if verify_only else ['--allow-physical','--local-result-review'])],
                    scenario, dict(PYTHONPATH=str(driver), DESKTOP_SELECT_SMOKE_ELEMENT_TRACE=str(trace)))
                after = fixture.command('state')
                assert result.get('stopped_at') == 3 and 'result_review' not in result, result
                assert after['opened'] == before['opened'] and after['queries'] == before['queries'] + 1, after
                assert after['ValueReads'] > before['ValueReads'] and not after['wrongRows'], after
                events = trace.read_text(encoding='utf8').splitlines()
                assert 'worker_guard_enabled' in events and not any('attempted' in line for line in events), events
                step = result['steps'][-1]
                diagnostic = step.get('receipt',step)['diagnostic']
                assert diagnostic == ('result_value_not_found' if scenario == 'missing' else 'result_cell_ambiguous'),result
                print(json.dumps(dict(case=scenario, wall_seconds=elapsed, queries=1, opened=0, image_calls=0,
                                      diagnostic=diagnostic)),flush=True)
    finally:
        fixture.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package',type=Path)
    parser.add_argument('work',type=Path)
    parser.add_argument('--baseline',action='store_true',help='measure the old registration stopping at step 3')
    parser.add_argument('--drive-review',action='store_true',help='exercise local Tk review using only this test fixture')
    parser.add_argument('--separate-review',action='store_true',help='continue a live query in a separate process by its review ticket')
    parser.add_argument('--native-table',action='store_true',help='verify automatic lookup on an accessible grid')
    parser.add_argument('--provider-grid',action='store_true',
                        help='verify a value-only grid with changing columns and rows, forbidding image capture')
    parser.add_argument('--verify-only',action='store_true',help='register just the query and value comparison')
    parser.add_argument('--oversized-grid',action='store_true',
                        help='native UIA group extends 107 px past window; rows and columns change each query')
    parser.add_argument('--unknown-offscreen',action='store_true',
                        help='fault-inject missing IsOffscreen only for the oversized fixture group')
    parser.add_argument('--duplicate-column-value',action='store_true',
                        help='repeat the requested value in another column of the same owner-drawn row')
    parser.add_argument('--cover-during-review',action='store_true',
                        help='cover the isolated target after selection before the first local confirmation')
    parser.add_argument('--repeat',type=int,choices=(1,2,3),default=1)
    args=parser.parse_args()
    if args.baseline and (args.drive_review or args.separate_review):
        parser.error('baseline has no local review')
    if (args.native_table or args.provider_grid) and (args.baseline or args.drive_review or args.separate_review):
        parser.error('element-readable fixtures run without a review driver')
    if args.native_table and args.provider_grid:
        parser.error('choose one fixture')
    if args.verify_only and not args.provider_grid:
        parser.error('verify-only exercises the provider grid')
    if args.oversized_grid and (not args.drive_review or args.native_table or args.provider_grid or args.baseline):
        parser.error('oversized-grid requires the real local review driver')
    if args.unknown_offscreen and not args.oversized_grid:
        parser.error('unknown-offscreen requires oversized-grid')
    if args.duplicate_column_value and not args.oversized_grid:
        parser.error('duplicate-column-value requires oversized-grid')
    if args.cover_during_review and not args.drive_review:
        parser.error('cover-during-review requires the real local review driver')
    exercise(args.package.resolve(strict=True),args.work.resolve(),args.baseline,args.repeat,
             args.drive_review,args.separate_review,args.native_table,args.provider_grid,args.verify_only,
             args.oversized_grid,args.unknown_offscreen,args.duplicate_column_value,args.cover_during_review)
