"""Exercise live semantic recovery from unidentified recorded locations on Windows.

Capture results are injected into the real record-save handler; this is not a
physical mouse-recording test. Every mutation uses the installed Desktop Select
CLI. The fixture pipe is limited to setup and independent state observations.
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

phrases = ['MES Setup 조건을 N으로 해줘',
           '입력받은 변수를 Family 조건에 입력해줘',
           'Search 버튼을 누른 뒤 0.1초 기다려줘',
           'Excel 버튼을 눌러줘']
steps = []
for number, text in enumerate(phrases, 1):
    raw = {'id': 'unknown-' + str(number), 'action':'manual',
           'label':'직접 확인할 단계', 'target':{'kind':'manual','locator':{}},
           'recognition':{'action':'manual','result':'manual','reason':'capture_target_unavailable'}}
    raw['instruction'] = tasks.compile_instruction(raw, text, target='Search', role='button', mode='recorded') \
        if number == 3 else tasks.compile_instruction(raw, text)
    if number == 3:
        raw['wait_after'] = .1
    steps.append(raw)
args = app.parser().parse_args(['record','--task','Recovered query','--exe',sys.argv[2],'--window',sys.argv[3]])
with patch('teach_capture.capture', return_value={'status':'recorded','steps':steps,'input_dispatched':False}):
    result = app.run(args)
print(json.dumps({'raw':steps,'task':tasks.load_task('Recovered query',sys.argv[4]),
                  'record_result':result}, ensure_ascii=False))
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
    book = work / 'recovered-query.xlsx'
    fixture.command('export-path|' + str(book))
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(tasks_dir), PYTHONDONTWRITEBYTECODE='1')
    sequence, checks, results = 0, [], []

    def check(name, condition, detail=None):
        checks.append(dict(case=name, passed=bool(condition)))
        print(json.dumps(checks[-1]), flush=True)
        if not condition:
            write_json(work / 'failed-case.json', dict(case=name, detail=detail))
            raise AssertionError((name, detail))

    def call(name, script, *args):
        nonlocal sequence
        sequence += 1
        metric_path = work / f'{sequence:02d}-{name}-processes.jsonl'
        run_env = dict(env, PYTHONPATH=str(probe), DS_SMOKE_SCRIPTS=str(installed / 'scripts'),
                       DS_SMOKE_METRICS=str(metric_path))
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), *args],
                              capture_output=True, encoding='utf8', cwd=work, env=run_env, timeout=90)
        try:
            receipt = json.loads(proc.stdout)
        except ValueError as error:
            raise AssertionError((name, proc.returncode, proc.stdout, proc.stderr)) from error
        processes = [json.loads(line) for line in metric_path.read_text(encoding='utf8').splitlines()]
        metrics = dict(seconds=round(time.perf_counter() - start, 6), processes=len(processes))
        for key in ('uia_sessions', 'full_tree_snapshots', 'native_findall', 'value_reads'):
            metrics[key] = sum(row[key] for row in processes)
        result = dict(name=name, receipt=receipt, metrics=metrics, exit_code=proc.returncode,
                      stderr=proc.stderr, state=fixture.command('state'))
        results.append(result)
        write_json(work / f'{sequence:02d}-{name}.json', result)
        print(json.dumps(dict(name=name, status=receipt.get('status'), metrics=metrics)), flush=True)
        return result

    def setup():
        fixture.command('restore')
        fixture.command('clear-extra')
        fixture.command('reset')
        fixture.command('label-wrappers')
        fixture.command('query-caption|Search')
        for identifier in ('conditionState', 'conditionFamily'):
            fixture.command('automation-id|' + identifier + '|' + identifier)
            fixture.command('mode|' + identifier + '|selected-after-write')

    def no_input(result):
        state = result['state']
        return (result['receipt'].get('input_dispatched') is False
                and state['opens'] == state['exports'] == 0
                and all(c['writes'] == c['selects'] == c['expands'] == 0
                        for c in state['controls'].values()))

    try:
        setup()
        created = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', CREATE_TASK,
                                  str(installed / 'scripts'), exe.name, fixture.ready['title'], str(tasks_dir)],
                                 capture_output=True, encoding='utf8', cwd=work, env=env, timeout=30)
        if created.returncode:
            raise AssertionError((created.stdout, created.stderr))
        saved = json.loads(created.stdout)
        write_json(work / 'record-save.json', saved)
        task = saved['task']
        task_path = tasks_dir / (task['name'] + '.json')
        original = task_path.read_bytes()
        binding = ['--exe', exe.name, '--window', fixture.ready['title']]
        check('unidentified_capture_promotes_explicit_named_descriptions',
              saved['record_result'].get('status') == 'recorded'
              and saved['record_result'].get('input_dispatched') is False
              and all(raw['target']['locator'] == {} for raw in saved['raw'])
              and all(s.get('match') == 'query' for s in task['steps'])
              and task['steps'][0]['instruction'].get('value') == 'N'
              and task['steps'][1]['instruction'].get('parameter') == 'Family', saved)
        sidecar = tasks_dir / 'execution-plans' / (task['name'] + '.json')
        check('recovered_task_auto_saves_query_plan_without_native_ids',
              sidecar.is_file() and all('automation_id' not in s.get('selector', {})
                                      for s in json.loads(sidecar.read_text(encoding='utf8'))['recipe']['steps']),
              saved['record_result'])

        fixture.command('minimize')
        before = fixture.command('state')
        missing = call('missing_variable', direct, 'replay', '--task', task['name'], *binding)
        check('required_variable_is_checked_before_restore_or_discovery',
              missing['receipt'].get('status') == 'needs_input' and missing['state'] == before
              and no_input(missing), missing)

        setup()
        directed = call('direct_label_command', direct, 'select', *binding, '--target', 'MES Setup', '--value', 'N')
        check('reference_direct_command_sets_the_same_labelled_control',
              directed['receipt'].get('status') == 'verified'
              and directed['state']['controls']['conditionState']['value'] == 'N'
              and directed['state']['controls']['conditionState']['writes'] == 1, directed)

        for family in ('Hybrid', 'DRAM'):
            setup()
            if family == 'DRAM':
                fixture.command('automation-id|conditionState|FreshStateEditor')
            replay = call('recovered_' + family, direct, 'replay', '--task', task['name'],
                          *binding, '--set', 'Family=' + family)
            control = replay['state']['controls']['conditionState']
            observed = cells(book) if book.is_file() else {}
            check(family + '_description_only_replay_exports_current_values',
                  replay['receipt'].get('status') in ('completed_unverified', 'executed_unverified')
                  and observed.get('A2') == 'N' and observed.get('B2') == family
                  and control['writes'] == 1 and control['expands'] == control['selects'] == 0
                  and replay['state']['opens'] == replay['state']['exports'] == 1, replay)
            check(family + '_each_unresolved_step_gets_one_current_tree',
                  replay['metrics']['full_tree_snapshots'] == 4, replay['metrics'])
            if family == 'DRAM':
                check('next_execution_discovers_changed_native_id_from_same_description',
                      'FreshStateEditor' in json.dumps(replay['receipt']) and task_path.read_bytes() == original,
                      replay)

        setup()
        single_replay = call('one_recorded_query_step', direct, 'replay', '--task', task['name'],
                             *binding, '--only', '1')
        check('individual_recorded_step_uses_live_description_resolution',
              single_replay['receipt'].get('status') == 'verified'
              and single_replay['state']['controls']['conditionState']['writes'] == 1
              and single_replay['state']['controls']['conditionFamily']['writes'] == 0,
              single_replay)

        for case in ('missing_target', 'ambiguous_target'):
            setup()
            single = deepcopy(task)
            single['name'] = case
            single['steps'] = [single['steps'][0]]
            if case == 'missing_target':
                single['steps'][0]['target'] = 'Absent condition'
                single['steps'][0]['instruction']['text'] = 'Absent condition을 N으로 설정'
            else:
                fixture.command('duplicate|conditionState')
            write_json(tasks_dir / (single['name'] + '.json'), single)
            stopped = call(case, direct, 'replay', '--task', single['name'], *binding)
            check(case + '_stops_without_any_input', no_input(stopped)
                  and stopped['receipt'].get('status') in ('blocked', 'needs_review'), stopped)

        setup()
        fixture.command('mode|conditionState|uncertain-write')
        uncertain = call('uncertain_write', direct, 'replay', '--task', task['name'],
                         *binding, '--set', 'Family=Hybrid')
        current = uncertain['state']['controls']
        check('uncertain_mutation_never_retries_or_falls_back_to_other_input',
              uncertain['receipt'].get('status') == 'uncertain'
              and uncertain['receipt'].get('input_dispatched') is None
              and current['conditionState']['writes'] == 1
              and current['conditionState']['selects'] == current['conditionState']['expands'] == 0
              and current['conditionFamily']['writes'] == 0
              and uncertain['state']['opens'] == uncertain['state']['exports'] == 0, uncertain)

        setup()
        manual = deepcopy(task)
        manual['name'] = 'Unparsed draft'
        manual['steps'] = [dict(action='manual', reason='capture_target_unavailable',
                                instruction_draft='이전 시험 설정을 참고해서 공정 조건을 맞춰줘')]
        write_json(tasks_dir / (manual['name'] + '.json'), manual)
        stopped = call('unparsed_draft', direct, 'replay', '--task', manual['name'], *binding)
        check('unparsed_draft_is_preserved_and_never_claimed_executable',
              stopped['receipt'].get('status') == 'needs_review' and no_input(stopped), stopped)
        clarified = 'Clarified draft'
        edited = call('instruct_named_target', direct, 'instruct', '--task', manual['name'], '--only', '1',
                      '--text', manual['steps'][0]['instruction_draft'], '--target', 'MES Setup',
                      '--role', 'combobox', '--mode', 'fixed', '--argument', 'P', '--save-as', clarified)
        check('host_interpretation_of_draft_saves_explicit_target_without_input',
              (tasks_dir / (clarified + '.json')).is_file() and no_input(edited), edited)
        executed = call('clarified_draft_replay', direct, 'replay', '--task', clarified, *binding)
        check('clarified_freeform_draft_replays_actual_value',
              executed['receipt'].get('status') == 'verified'
              and executed['state']['controls']['conditionState']['value'] == 'P'
              and executed['state']['controls']['conditionState']['writes'] == 1, executed)

        setup()
        generated = work / 'skills' / 'recovered-query'
        registered = call('register_recovered_task', helper, 'register', '--task', task['name'], '--out', str(generated))
        check('register_description_plan_remains_read_only',
              registered['receipt'].get('status') == 'registered' and no_input(registered), registered)
        registered_recipe = json.loads((generated / 'workflow.json').read_text(encoding='utf8'))
        check('registration_pins_only_live_discovered_ids_and_retains_description_source',
              all('query' not in s.get('selector', {}) for s in registered_recipe['steps'])
              and registered_recipe['steps'][0]['selector']['automation_id'] == 'conditionState'
              and json.loads((generated / 'source-task.json').read_text(encoding='utf8')) == task,
              registered_recipe)
        exported = call('generated_python_runner', generated / 'scripts/run.py', '--set', 'Family=Hybrid')
        check('exported_python_runner_executes_registered_descriptions_without_full_discovery',
              exported['receipt'].get('status') == 'completed_unverified'
              and cells(book).get('A2') == 'N' and cells(book).get('B2') == 'Hybrid'
              and exported['metrics']['full_tree_snapshots'] == 0
              and task_path.read_bytes() == original, exported)
    finally:
        fixture.close()
        summary = dict(passed=sum(c['passed'] for c in checks), total=len(checks), checks=checks,
                       company_fx_tested=False, real_windows_provider=True,
                       recording_note='Injected unidentified capture into actual record-save handler; no physical mouse recording.')
        write_json(work / 'summary.json', summary)
        write_json(work / 'results.json', results)
        print(json.dumps({k: v for k, v in summary.items() if k != 'checks'}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    parser.add_argument('--work-dir', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Requires Windows and a new absolute work directory')
    args.work_dir.mkdir(parents=True)
    exercise(args.skill_dir.resolve(), args.work_dir)


if __name__ == '__main__':
    main()
