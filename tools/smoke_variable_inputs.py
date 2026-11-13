"""Verify recorded variable selections and text inputs on a real Windows UIA fixture.

Capture drafts are supplied to the normal save handler. This does not simulate
physical typing during recording. All mutations go through the packaged CLI;
fixture commands only prepare providers and independently inspect results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_040_rebuild import ROOT, Fixture, cells, compile_fixture, install_skill, write_json, CAPTURE_PROBE
from smoke_040_workflow import PROBE


SAVE_TASK = r'''
import json, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import desktop_select as app
import learned_tasks as tasks
rows=[]
for field, identifier, role, sentence in (
    ('MES Setup','conditionState','combobox','MES Setup 조건을 N으로 설정해줘'),
    ('Family','conditionFamily','combobox','입력받은 변수를 Family 조건에 입력해줘'),
    ('Search text','searchText','textfield','입력받은 {SearchText} 변수를 Search text 입력란에 입력해줘')):
    raw=dict(action='teach-target', field_label=field,
             target=dict(kind='element',locator=dict(automation_id=identifier, role=role)),
             recognition=dict(target='identified',action='requires_instruction',result='manual',
                              reason='combo_choice_unproven' if role=='combobox' else 'text_value_unobserved'))
    raw['instruction']=tasks.compile_instruction(raw,sentence)
    rows.append(raw)
for identifier, caption in (('QueryCommand','Search'),('ExportCommand','Excel')):
    raw=dict(action='click',field_label=caption,
             target=dict(kind='element',locator=dict(automation_id=identifier,name=caption,role='button')),
             recognition=dict(target='identified',action='supported',result='manual'))
    raw['instruction']=tasks.compile_instruction(raw,caption+' 버튼을 눌러줘')
    if identifier=='QueryCommand':raw['wait_after']=.1
    rows.append(raw)
args=app.parser().parse_args(['record','--task','Variable query','--exe',sys.argv[2],'--window',sys.argv[3]])
with patch('teach_capture.capture',return_value=dict(status='recorded',steps=rows,input_dispatched=False)):
    receipt=app.run(args)
print(json.dumps(dict(receipt=receipt,source=tasks.load_task('Variable query',app.task_directory())),ensure_ascii=False))
'''


def exercise(source, work):
    skills = work / 'project/.claude/skills'
    installed = skills / 'desktop-select'
    install_skill(source, installed)
    probe = work / 'probe'
    probe.mkdir()
    (probe / 'sitecustomize.py').write_text(PROBE, encoding='utf8')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env.pop('DESKTOP_SELECT_TASK_DIR', None)
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    fixture.command('text-add')
    output = work / 'variable-query.xlsx'
    fixture.command('export-path|' + str(output))
    direct = installed / 'scripts/desktop_select.py'
    helper = installed / 'scripts/python_workflow.py'
    generated = skills / 'variable-query'
    launcher = generated / 'scripts/run.py'
    bound = ['--exe', exe.name, '--window', fixture.ready['title']]
    results, checks = [], []

    def check(name, ok, detail=None):
        checks.append(dict(case=name, passed=bool(ok)))
        print(json.dumps(checks[-1]), flush=True)
        if not ok:
            write_json(work / 'failed-case.json', dict(case=name, detail=detail))
            raise AssertionError(name)

    def call(name, script, *args, inline=False):
        metric_path = work / (str(len(results)+1).zfill(2) + '-' + name + '-metrics.jsonl')
        current = dict(env, PYTHONPATH=str(probe), DS_SMOKE_SCRIPTS=str(installed / 'scripts'),
                       DS_SMOKE_METRICS=str(metric_path))
        command = [sys.executable, '-X', 'utf8', '-B', *(['-c', script] if inline else [str(script)]), *args]
        start = time.monotonic()
        done = subprocess.run(command, cwd=work, env=current, capture_output=True, encoding='utf8', timeout=90)
        try:
            receipt = json.loads(done.stdout)
        except ValueError as error:
            raise AssertionError((name, done.returncode, done.stdout, done.stderr)) from error
        metrics = [json.loads(line) for line in metric_path.read_text(encoding='utf8').splitlines()]
        row = dict(case=name, receipt=receipt, code=done.returncode, seconds=round(time.monotonic()-start,4),
                   full_tree_snapshots=sum(m['full_tree_snapshots'] for m in metrics),
                   uia_sessions=sum(m['uia_sessions'] for m in metrics), state=fixture.command('state'))
        results.append(row)
        write_json(work / (str(len(results)).zfill(2) + '-' + name + '.json'), row)
        return row

    def reset():
        fixture.command('reset')
        fixture.command('text-reset')
        fixture.command('restore')

    try:
        reset()
        # Point capture needs an unobscured fixture; pattern actions below do not.
        fixture.command('point-front')
        time.sleep(.2)
        point_probe = CAPTURE_PROBE.replace("'conditionState'", "'searchText'").replace("'combobox'", "'textfield'")
        pointed = call('capture-text-point', point_probe, str(installed/'scripts'), exe.name,
                       fixture.ready['title'], str(fixture.ready['hwnd']), inline=True)
        fixture.command('point-done')
        rows = pointed['receipt'].get('steps', [])
        check('live_text_point_is_preserved_for_instruction', len(rows)==1
              and rows[0].get('target',{}).get('locator',{}).get('role')=='textfield'
              and rows[0]['action'] in ('teach-target','set-value') and pointed['state']['text_writes']==0, pointed)
        inspected = call('inspect-empty', direct, 'inspect', *bound, '--target', 'Search text', '--role', 'textfield')
        check('empty_text_observed_without_input', inspected['receipt'].get('value') == ''
              and inspected['receipt'].get('value_evidence',{}).get('status') == 'observed'
              and inspected['state']['text_writes'] == 0, inspected)
        literal = '0012 한글 "A&B" = test'
        filled = call('direct-fill', direct, 'fill', *bound, '--target', 'Search text', '--value', literal)
        check('direct_text_keeps_literal_characters', filled['receipt']['status']=='verified'
              and filled['state']['text_value']==literal and filled['state']['text_writes']==1, filled)
        cleared = call('direct-clear', direct, 'fill', *bound, '--target', 'Search text', '--value', '')
        check('explicit_empty_text_clears_once', cleared['receipt']['status']=='verified'
              and cleared['state']['text_value']=='' and cleared['state']['text_writes']==2, cleared)
        embedded = call('combo-editor', direct, 'fill', *bound, '--target', 'conditionStateEditor0', '--value', 'N')
        check('combo_editor_not_treated_as_text_field', embedded['receipt']['input_dispatched'] is False
              and embedded['state']['controls']['conditionState']['writes']==0, embedded)
        for mode in ('readonly','disabled','password'):
            fixture.command('text-mode|' + mode)
            blocked = call('guard-'+mode, direct, 'fill', *bound, '--target', 'Search text', '--value', 'blocked')
            check(mode+'_field_receives_no_input', blocked['receipt']['input_dispatched'] is False
                  and blocked['state']['text_writes']==2, blocked)
        reset()
        saved = call('record-save', SAVE_TASK, str(installed/'scripts'), exe.name, fixture.ready['title'], inline=True)
        source_task = saved['receipt']['source']
        check('recorded_fixed_and_variable_instructions_preserved', source_task['steps'][0]['instruction']['value']=='N'
              and source_task['steps'][1]['instruction']['parameter']=='Family'
              and source_task['steps'][2]['action']=='fill'
              and source_task['steps'][2]['instruction']['parameter']=='SearchText', saved)
        registered = call('register', helper, 'register', '--task', 'Variable query', '--out', str(generated))
        check('register_is_readonly_and_keeps_required_values', registered['receipt']['status']=='registered'
              and registered['receipt']['required_parameters']==['Family','SearchText']
              and registered['state']['text_writes']==0
              and json.loads((generated/'source-task.json').read_text(encoding='utf8'))==source_task, registered)
        missing = call('missing-variables', launcher)
        check('all_missing_values_reported_before_input', missing['receipt']['status']=='needs_input'
              and set(missing['receipt']['missing_parameters'])=={'Family','SearchText'}
              and missing['state']['text_writes']==0
              and missing['state']['controls']['conditionState']['writes']==0, missing)
        for family, text, label in (('Hybrid',literal,'first-run'),('DRAM','000045','second-run')):
            reset()
            ran = call(label, launcher, '--set', 'Family='+family, '--set', 'SearchText='+text)
            workbook = cells(output)
            check(label+'_values_reach_actual_workbook', ran['receipt']['status']=='completed_unverified'
                  and workbook.get('A2')=='N' and workbook.get('B2')==family and workbook.get('D2')==text
                  and ran['state']['text_writes']==1 and ran['uia_sessions']==1
                  and ran['full_tree_snapshots']==0, ran)
        reset()
        ran = call('replay-variables', direct, 'replay', *bound, '--task', 'Variable query',
                   '--set','Family=Standard','--set','SearchText=000099')
        check('recorded_replay_uses_current_text_parameter', ran['receipt']['status']=='completed_unverified'
              and cells(output).get('D2')=='000099', ran)
        reset()
        only = call('only-text', direct, 'replay', *bound, '--task','Variable query','--only','3','--set','SearchText=007')
        check('single_recorded_text_step_runs', only['receipt']['status']=='verified'
              and only['state']['text_value']=='007' and only['state']['opens']==0, only)
        reset()
        empty = call('empty-parameter', launcher, '--set','Family=Hybrid','--set','SearchText=')
        check('empty_text_parameter_is_intentional_clear', empty['receipt']['status']=='completed_unverified'
              and empty['state']['text_value']=='' and empty['state']['text_writes']==1, empty)
        reset()
        invalid = call('empty-combo', launcher, '--set','Family=','--set','SearchText=test')
        check('empty_combo_prevents_all_input', invalid['receipt']['input_dispatched'] is False
              and invalid['state']['controls']['conditionState']['writes']==0 and invalid['state']['text_writes']==0, invalid)
        for mode in ('uncertain-write','unreadable','mismatch'):
            reset()
            fixture.command('text-mode|' + mode)
            stopped = call(mode, launcher, '--set','Family=Hybrid','--set','SearchText=003')
            check(mode+'_stops_after_one_text_write', stopped['receipt']['status'] in
                  ('uncertain','delivered_unverified','mismatch') and stopped['state']['text_writes']==1
                  and stopped['state']['opens']==0 and stopped['state']['exports']==0, stopped)
    finally:
        fixture.close()
        write_json(work/'results.json', results)
        summary = dict(passed=sum(c['passed'] for c in checks), total=len(checks), checks=checks,
                       real_windows_provider=True, company_fx_tested=False, physical_recording_tested=False)
        write_json(work/'summary.json', summary)
        print(json.dumps({k:v for k,v in summary.items() if k!='checks'}), flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir',type=Path,default=ROOT/'skills/desktop-select')
    parser.add_argument('--work-dir',type=Path,required=True)
    args=parser.parse_args()
    if os.name!='nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Requires Windows and a new absolute work directory')
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(),args.work_dir)


if __name__=='__main__':
    main()
