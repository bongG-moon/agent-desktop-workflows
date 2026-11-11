"""Recorded natural instructions -> saved plan -> one-session Windows execution.

The task is built by the same instruction compiler as the recording editor.
Only fixture setup and independent observations use the fixture control pipe.
Actual selection, query and export use the installed Desktop Select commands.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_040_rebuild import ROOT, Fixture, cells, compile_fixture, install_skill, write_json
from smoke_040_workflow import PROBE


CREATE_TASK = r'''
import json, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import desktop_select as app
import learned_tasks as tasks

def combo(identifier, label, sentence):
    raw = {'action':'teach-target', 'field_label':label,
           'target':{'kind':'element','locator':{'automation_id':identifier,'role':'combobox','name':'','ancestors':[]}},
           'recognition':{'target':'identified','action':'requires_instruction','result':'manual','reason':'combo_choice_unproven'}}
    raw['instruction'] = tasks.compile_instruction(raw, sentence)
    return raw

def button(identifier, label, wait_after=None):
    raw = {'action':'click', 'field_label':label,
           'target':{'kind':'element','locator':{'automation_id':identifier,'role':'button','name':label,'ancestors':[]}},
           'recognition':{'target':'identified','action':'supported','result':'manual'}}
    sentence = label + ' 버튼을 눌러줘'
    if wait_after is not None:
        sentence = label + ' 버튼을 누른 뒤 ' + str(wait_after) + '초 기다려줘'
        raw['wait_after'] = wait_after
    raw['instruction'] = tasks.compile_instruction(raw, sentence, mode='recorded')
    return raw

steps = [
    combo('conditionState','MES Setup','MES Setup 조건을 N으로 해줘'),
    combo('conditionFamily','Family','입력받은 변수를 Family 조건에 입력해줘'),
    button('QueryCommand','Search',.1), button('ExportCommand','Excel')]
# Replay the reviewed capture result through the actual record-save handler.
# This exercises automatic plan creation, not a physical mouse recording.
args = app.parser().parse_args(['record','--task','Natural query','--exe',sys.argv[2],'--window',sys.argv[3]])
with patch('teach_capture.capture', return_value={'status':'recorded','steps':steps,'input_dispatched':False}):
    result = app.run(args)
plan = tasks.load_task('Natural query', sys.argv[4])
print(json.dumps({'plan':plan,'record_result':result,'capture_result_injected':True}, ensure_ascii=False))
'''


def exercise(source, work):
    installed = work / 'skills' / 'desktop-select'
    install_skill(source, installed)
    probe = work / 'probe'
    probe.mkdir()
    (probe / 'sitecustomize.py').write_text(PROBE, encoding='utf8')
    exe = compile_fixture(work)
    fixture = Fixture(exe)
    direct = installed / 'scripts/desktop_select.py'
    helper = installed / 'scripts/python_workflow.py'
    tasks_dir = work / 'tasks'
    generated = work / 'skills' / 'natural-query'
    book = work / 'natural-query.xlsx'
    fixture.command('export-path|' + str(book))
    sequence, checks, results = 0, [], []
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(tasks_dir), PYTHONDONTWRITEBYTECODE='1')

    def check(name, condition, detail=None):
        checks.append(dict(case=name, passed=bool(condition)))
        print(json.dumps(checks[-1]), flush=True)
        if not condition:
            write_json(work / 'failed-case.json', dict(case=name, detail=detail))
            raise AssertionError((name, detail))

    def call(name, script, *args):
        nonlocal sequence
        sequence += 1
        metrics_path = work / f'{sequence:02d}-{name}-processes.jsonl'
        run_env = dict(env, PYTHONPATH=str(probe), DS_SMOKE_SCRIPTS=str(installed / 'scripts'),
                       DS_SMOKE_METRICS=str(metrics_path))
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), *args],
                              capture_output=True, encoding='utf8', cwd=work, env=run_env, timeout=90)
        try:
            receipt = json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((name, proc.returncode, proc.stdout, proc.stderr)) from error
        processes = [json.loads(line) for line in metrics_path.read_text(encoding='utf8').splitlines()]
        metric = dict(seconds=round(time.perf_counter() - start, 6), processes=len(processes))
        for key in ('uia_sessions', 'full_tree_snapshots', 'native_findall', 'value_reads'):
            metric[key] = sum(row[key] for row in processes)
        result = dict(name=name, receipt=receipt, metrics=metric, exit_code=proc.returncode,
                      stderr=proc.stderr, state=fixture.command('state'))
        results.append(result)
        write_json(work / f'{sequence:02d}-{name}.json', result)
        print(json.dumps(dict(name=name, status=receipt.get('status'), metrics=metric)), flush=True)
        return result

    def setup():
        fixture.command('restore')
        fixture.command('clear-extra')
        fixture.command('reset')
        fixture.command('label-wrappers')
        fixture.command('query-caption|Search')
        for identifier in ('conditionState', 'conditionFamily'):
            fixture.command('mode|' + identifier + '|selected-after-write')

    try:
        setup()
        created = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', CREATE_TASK,
                                  str(installed / 'scripts'), exe.name, fixture.ready['title'], str(tasks_dir)],
                                 capture_output=True, encoding='utf8', cwd=work, env=env, timeout=30)
        if created.returncode:
            raise AssertionError((created.stdout, created.stderr))
        created_result = json.loads(created.stdout)
        task = created_result['plan']
        task_path = tasks_dir / (task['name'] + '.json')
        original = task_path.read_bytes()
        automatic = tasks_dir / 'execution-plans' / (task['name'] + '.json')
        check('record_save_handler_automatically_persists_execution_plan',
              created_result['record_result'].get('status') == 'recorded' and automatic.is_file()
              and created_result['record_result'].get('input_dispatched') is False, created_result)
        check('natural_location_only_steps_compile_fixed_and_required_values',
              task['steps'][0]['instruction']['value'] == 'N'
              and task['steps'][1]['instruction']['parameter'] == 'Family'
              and all(s['recorded_effect'] == 'unobserved' for s in task['steps'][:2]), task)

        before = fixture.command('state')
        planned = call('save_plan', direct, 'plan', '--task', task['name'])
        check('plan_saved_without_input_or_source_mutation',
              planned['receipt'].get('status') == 'planned' and planned['receipt'].get('input_dispatched') is False
              and planned['state'] == before and task_path.read_bytes() == original, planned)
        sidecars = sorted((tasks_dir / 'execution-plans').glob('*.json'))
        check('sidecar_preserves_original_natural_instructions', len(sidecars) == 1 and all(
              s['instruction']['text'] in sidecars[0].read_text(encoding='utf8') for s in task['steps']),
              [str(p.name) for p in sidecars])
        saved_before = sidecars[0].read_bytes()

        edited_name = 'Edited natural query'
        phrase = '이 조건에는 실행할 때 받은 Family 값을 사용해 줘'
        edited = call('instruct_copy', direct, 'instruct', '--task', task['name'], '--only', '2',
                      '--text', phrase, '--mode', 'required', '--argument', 'Family', '--save-as', edited_name)
        changed_task = tasks_dir / (edited_name + '.json')
        check('freeform_instruction_with_explicit_structure_saves_new_task_without_input',
              edited['receipt'].get('input_dispatched') is False and changed_task.is_file()
              and task_path.read_bytes() == original and edited['state'] == before, edited)
        edited_plan = json.loads(changed_task.read_text(encoding='utf8'))
        check('freeform_sentence_and_required_variable_both_preserved',
              edited_plan['steps'][1]['instruction'] == dict(mode='required', text=phrase, parameter='Family')
              and edited_plan['steps'][2].get('wait_after') == .1, edited_plan)
        wait_name = 'Explicit wait edited query'
        wait_text = 'Search 버튼을 누른 뒤 0.2초 기다려줘'
        edited_wait = call('instruct_wait', direct, 'instruct', '--task', task['name'], '--only', '3',
                           '--text', wait_text, '--mode', 'recorded', '--wait-after', '.2', '--save-as', wait_name)
        wait_task = json.loads((tasks_dir / (wait_name + '.json')).read_text(encoding='utf8'))
        wait_plan = json.loads((tasks_dir / 'execution-plans' / (wait_name + '.json')).read_text(encoding='utf8'))
        check('explicit_natural_wait_and_compiled_delay_are_saved_together_without_input',
              edited_wait['receipt'].get('input_dispatched') is False and edited_wait['state'] == before
              and task_path.read_bytes() == original
              and wait_task['steps'][2].get('wait_after') == .2
              and wait_task['steps'][2]['instruction']['text'] == wait_text
              and any(s == dict(action='wait', seconds=.2) for s in wait_plan['recipe']['steps']), edited_wait)

        fixture.command('minimize')
        before = fixture.command('state')
        binding = ['--exe', exe.name, '--window', fixture.ready['title']]
        missing = call('missing_parameter', direct, 'replay', '--task', task['name'], *binding)
        check('required_input_precedes_window_restore_and_first_action',
              missing['receipt'].get('status') == 'needs_input' and missing['state'] == before
              and missing['receipt'].get('input_dispatched') is False, missing)

        for family in ('Hybrid', 'DRAM'):
            setup()
            replay = call('replay_' + family, direct, 'replay', '--task', task['name'],
                          *binding, '--set', 'Family=' + family)
            observed = cells(book)
            check(family + '_normal_replay_uses_natural_plan_and_creates_current_workbook',
                  replay['receipt'].get('status') in ('completed_unverified', 'executed_unverified')
                  and replay['state']['opens'] == replay['state']['exports'] == 1
                  and observed.get('A2') == 'N' and observed.get('B2') == family
                  and [s.get('source_step') for s in replay['receipt'].get('steps', [])] == [1, 2, 3, 3, 4]
                  and [s.get('phase') for s in replay['receipt'].get('steps', [])] ==
                      ['action', 'action', 'action', 'wait', 'action'], replay)
            check(family + '_one_session_without_whole_window_inspection',
                  replay['metrics']['uia_sessions'] == 1 and replay['metrics']['full_tree_snapshots'] == 0,
                  replay['metrics'])
        check('replay_keeps_saved_instruction_source_unchanged', task_path.read_bytes() == original)

        setup()
        fixture.command('hide|ExportCommand')
        missing_export = call('missing_export_after_wait', direct, 'replay', '--task', task['name'],
                              *binding, '--set', 'Family=Hybrid')
        check('failure_after_inserted_wait_reports_original_recorded_step',
              missing_export['receipt'].get('status') == 'blocked'
              and missing_export['receipt'].get('stopped_at') == 5
              and missing_export['receipt'].get('source_stopped_at') == 4
              and missing_export['receipt']['steps'][-1].get('source_step') == 4
              and missing_export['receipt']['steps'][-1].get('phase') == 'action'
              and missing_export['state']['opens'] == 1 and missing_export['state']['exports'] == 0, missing_export)

        select_only = deepcopy(task)
        select_only['name'] = 'Selection pattern only'
        select_only['steps'] = [select_only['steps'][0]]
        write_json(tasks_dir / (select_only['name'] + '.json'), select_only)
        setup()
        fixture.command('mode|conditionState|select-only')
        selection = call('select_only_replay', direct, 'replay', '--task', select_only['name'], *binding)
        selected = selection['state']['controls']['conditionState']
        check('natural_replay_preserves_select_only_provider_path',
              selection['receipt'].get('status') == 'verified' and selected['value'] == 'N'
              and selected['writes'] == 0 and selected['selects'] == 1 and selected['expands'] == 0, selection)
        setup()
        fixture.command('mode|conditionState|uncertain-select-only')
        uncertain = call('select_only_uncertain', direct, 'replay', '--task', select_only['name'], *binding)
        uncertain_state = uncertain['state']['controls']['conditionState']
        check('uncertain_select_only_call_never_switches_to_setvalue_or_retries',
              uncertain['receipt'].get('status') == 'uncertain'
              and uncertain['receipt'].get('input_dispatched') is None
              and uncertain_state['writes'] == 0 and uncertain_state['selects'] == 1, uncertain)

        no_wait = deepcopy(task)
        no_wait['name'] = 'Query without explicit wait'
        no_wait['steps'][2].pop('wait_after', None)
        no_wait['steps'][2]['instruction']['text'] = 'Search 버튼을 눌러줘'
        write_json(tasks_dir / (no_wait['name'] + '.json'), no_wait)
        setup()
        unsafe_plan = call('no_wait_plan', direct, 'plan', '--task', no_wait['name'])
        check('nonterminal_query_without_explicit_wait_is_not_fast_compiled',
              unsafe_plan['receipt'].get('status') == 'needs_review'
              and unsafe_plan['receipt'].get('input_dispatched') is False, unsafe_plan)
        fallback = call('no_wait_replay', direct, 'replay', '--task', no_wait['name'],
                        *binding, '--set', 'Family=Hybrid')
        check('legacy_fallback_does_not_export_after_unverified_query',
              fallback['receipt'].get('status') == 'delivered_unverified'
              and fallback['receipt'].get('stopped_at') == 3
              and fallback['state']['opens'] == 1 and fallback['state']['exports'] == 0, fallback)

        changed = deepcopy(task)
        changed['steps'][0]['instruction'].update(value='P', text='MES Setup 조건을 P로 해줘')
        write_json(task_path, changed)
        setup()
        replay = call('changed_source', direct, 'replay', '--task', task['name'], *binding, '--set', 'Family=Hybrid')
        check('edited_instruction_replaces_stale_plan_before_execution',
              replay['receipt'].get('status') in ('completed_unverified', 'executed_unverified')
              and cells(book).get('A2') == 'P' and sidecars[0].read_bytes() != saved_before, replay)

        setup()
        before = fixture.command('state')
        registered = call('register_recorded_task', helper, 'register', '--task', task['name'], '--out', str(generated))
        check('register_task_exports_readonly_python_skill', registered['receipt'].get('status') == 'registered'
              and registered['state'] == before and registered['receipt'].get('input_dispatched') is False, registered)
        check('registered_workflow_preserves_required_family',
              json.loads((generated / 'workflow.json').read_text(encoding='utf8'))['steps'][1].get('parameter') == 'Family')
        files = sorted(str(p.relative_to(generated)).replace('\\', '/') for p in generated.rglob('*') if p.is_file())
        check('generated_skill_retains_original_natural_instructions_in_four_text_files',
              files == ['SKILL.md', 'scripts/run.py', 'source-task.json', 'workflow.json']
              and json.loads((generated / 'source-task.json').read_text(encoding='utf8')) == changed, files)
        exported = call('run_exported_skill', generated / 'scripts/run.py', '--set', 'Family=DRAM')
        check('exported_python_skill_uses_edited_natural_plan',
              exported['receipt'].get('status') == 'completed_unverified' and cells(book).get('A2') == 'P'
              and cells(book).get('B2') == 'DRAM', exported)

        unsupported = deepcopy(changed)
        unsupported['steps'].append(dict(action='manual', reason='capture_target_unavailable'))
        write_json(task_path, unsupported)
        setup()
        before = fixture.command('state')
        stopped = call('manual_step', direct, 'replay', '--task', task['name'], *binding, '--set', 'Family=Hybrid')
        check('unsupported_manual_step_never_reuses_old_plan_or_partially_executes',
              stopped['receipt'].get('status') == 'needs_review' and stopped['state'] == before
              and stopped['receipt'].get('input_dispatched') is False, stopped)
    finally:
        fixture.close()
        summary = dict(passed=sum(row['passed'] for row in checks), total=len(checks), checks=checks,
                       company_fx_tested=False, real_windows_provider=True,
                       recording_note='Record-save handler used injected reviewed capture result; no physical mouse recording.')
        write_json(work / 'summary.json', summary)
        write_json(work / 'results.json', results)
        print(json.dumps({k: v for k, v in summary.items() if k != 'checks'}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Requires Windows and a new absolute work directory')
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.work_dir)


if __name__ == '__main__':
    main()
