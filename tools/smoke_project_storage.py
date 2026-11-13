"""Exercise isolated project stores using installed Desktop Select CLI processes.

This test creates local JSON fixtures, not physical recordings, and sends no UI
input. It checks real package paths, subprocess working directories, source task
and derived-plan isolation, and preservation of an unrelated shared store.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SEED = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import learned_tasks as tasks
raw = {'action':'teach-target', 'field_label':'MES Setup',
       'recognition':{'target':'identified','action':'requires_instruction',
                      'result':'manual','reason':'combo_choice_unproven'},
       'target':{'kind':'element','locator':{'automation_id':'stateField',
                 'role':'combobox','ancestors':[{'role':'group','automation_id':'conditions'}]}}}
raw['instruction'] = tasks.compile_instruction(raw, 'MES Setup 조건을 ' + sys.argv[3] + '으로 설정해줘')
task = tasks.from_recording(sys.argv[2], 'Example.exe', 'Example window', [raw])
path = tasks.save_task(task)
print(json.dumps({'status':'recorded','path':str(path),'input_dispatched':False}))
'''


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def install(source, destination):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('__pycache__', '.desktop-select'))
    for name in ('win_bridge.py', 'recorder.py'):
        target = destination / 'scripts' / name
        if not target.is_file():
            shutil.copyfile(ROOT / 'scripts' / name, target)


def snapshot(directory):
    return {str(path.relative_to(directory)): sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob('*') if path.is_file()}


def exercise(source, work):
    projects = {key: work / ('project-' + key) for key in ('a', 'b')}
    skills = {key: folder / '.claude/skills/desktop-select' for key, folder in projects.items()}
    stores = {key: folder / '.desktop-select/tasks' for key, folder in projects.items()}
    for key, installed in skills.items():
        install(source, installed)
    unrelated = work / 'unrelated-working-directory'
    unrelated.mkdir()
    local = work / 'local-appdata'
    shared = local / 'DesktopSelect/tasks'
    env = dict(os.environ, LOCALAPPDATA=str(local), PYTHONDONTWRITEBYTECODE='1')
    env.pop('DESKTOP_SELECT_TASK_DIR', None)
    checks, results = [], []

    def check(case, condition, detail=None):
        row = dict(case=case, passed=bool(condition))
        checks.append(row)
        print(json.dumps(row), flush=True)
        if not condition:
            write_json(work / 'failed-case.json', dict(case=case, detail=detail))
            raise AssertionError(case)

    def call(case, installed, *arguments, cwd=None, override=None, seed=False):
        current_env = dict(env)
        if override is not None:
            current_env['DESKTOP_SELECT_TASK_DIR'] = str(override)
        command = [sys.executable, '-X', 'utf8', '-B']
        if seed:
            command += ['-c', SEED, str(installed / 'scripts'), *arguments]
        else:
            command += [str(installed / 'scripts/desktop_select.py'), *arguments]
        completed = subprocess.run(command, cwd=cwd or unrelated, env=current_env,
                                   capture_output=True, encoding='utf8', timeout=30)
        try:
            receipt = json.loads(completed.stdout)
        except ValueError as error:
            raise AssertionError((case, completed.returncode, completed.stdout, completed.stderr)) from error
        row = dict(case=case, receipt=receipt, exit_code=completed.returncode, stderr=completed.stderr)
        results.append(row)
        write_json(work / (str(len(results)).zfill(2) + '-' + case + '.json'), row)
        return receipt, completed.returncode

    task_name = 'Install test'
    try:
        seeded, code = call('seed-shared', skills['a'], 'Shared only', 'W', override=shared, seed=True)
        check('shared_fixture_created', code == 0 and Path(seeded['path']) == shared / 'Shared only.json', seeded)
        original_shared = snapshot(shared)
        for key, installed in skills.items():
            observed, code = call('storage-' + key, installed, 'storage',
                                  cwd=installed / 'scripts' if key == 'a' else unrelated)
            check('project_' + key + '_default_is_install_anchored', code == 0
                  and observed.get('scope') == 'project'
                  and Path(observed.get('project_root', '')) == projects[key]
                  and Path(observed.get('task_directory', '')) == stores[key]
                  and Path(observed.get('execution_plan_directory', '')) == stores[key] / 'execution-plans'
                  and observed.get('input_dispatched') is False, observed)
            listed, code = call('empty-tasks-' + key, installed, 'tasks', cwd=projects['b' if key == 'a' else 'a'])
            check('project_' + key + '_reads_do_not_create_or_import', code == 0
                  and listed.get('tasks') == [] and not stores[key].exists(), listed)

        for key, value in (('a', 'N'), ('b', 'P')):
            seeded, code = call('seed-project-' + key, skills[key], task_name, value, seed=True)
            check('project_' + key + '_default_library_save', code == 0
                  and Path(seeded.get('path', '')) == stores[key] / (task_name + '.json'), seeded)
            listed, code = call('listed-' + key, skills[key], 'tasks', cwd=skills[key] / 'scripts')
            check('project_' + key + '_list_stays_local', code == 0 and listed.get('tasks') == [task_name], listed)
            shown, code = call('show-' + key, skills[key], 'show', '--task', task_name)
            check('project_' + key + '_same_name_keeps_own_value', code == 0
                  and shown.get('task', {}).get('steps', [{}])[0].get('instruction', {}).get('value') == value, shown)
            planned, code = call('plan-' + key, skills[key], 'plan', '--task', task_name,
                                 cwd=projects['b' if key == 'a' else 'a'])
            path = stores[key] / 'execution-plans' / (task_name + '.json')
            data = json.loads(path.read_text(encoding='utf8')) if path.is_file() else {}
            check('project_' + key + '_plan_stays_with_recording', code == 0
                  and Path(planned.get('execution_plan', {}).get('path', '')) == path
                  and data.get('recipe', {}).get('steps', [{}])[0].get('value') == value, planned)

        originals = {key: (stores[key] / (task_name + '.json')).read_bytes() for key in skills}
        edited, code = call('instruct-a', skills['a'], 'instruct', '--task', task_name, '--only', '1',
                            '--text', 'MES Setup 조건을 C로 설정해줘', '--mode', 'fixed', '--argument', 'C',
                            '--save-as', 'Edited test', cwd=projects['b'])
        check('instruct_saves_task_and_plan_in_origin_project', code == 0
              and (stores['a'] / 'Edited test.json').is_file()
              and (stores['a'] / 'execution-plans/Edited test.json').is_file()
              and not (stores['b'] / 'Edited test.json').exists(), edited)
        check('instruct_preserves_both_original_recordings', all(
            (stores[key] / (task_name + '.json')).read_bytes() == before for key, before in originals.items()))
        listed, code = call('explicit-shared', skills['b'], 'tasks', override=shared)
        check('shared_store_requires_explicit_opt_in', code == 0 and listed.get('tasks') == ['Shared only'], listed)
        rejected, code = call('relative-override', skills['a'], 'storage', override='relative-store')
        check('relative_override_is_rejected_without_cwd_spill', code == 2
              and rejected.get('status') == 'blocked' and rejected.get('input_dispatched') is False
              and not (unrelated / 'relative-store').exists(), rejected)

        portable = work / 'portable/desktop-select'
        install(source, portable)
        observed, code = call('portable-storage', portable, 'storage')
        portable_store = portable / '.desktop-select/tasks'
        check('portable_storage_is_inside_unpacked_skill', code == 0
              and observed.get('scope') == 'portable'
              and Path(observed.get('task_directory', '')) == portable_store
              and not portable_store.exists(), observed)
        seeded, code = call('portable-save', portable, 'Portable test', 'N', seed=True)
        check('portable_default_save_ignores_working_directory', code == 0
              and Path(seeded.get('path', '')) == portable_store / 'Portable test.json', seeded)
        check('existing_shared_data_is_byte_unchanged', snapshot(shared) == original_shared)
        check('unrelated_working_directory_has_no_task_store', not (unrelated / '.desktop-select').exists())
    finally:
        summary = dict(passed=sum(row['passed'] for row in checks), total=len(checks), checks=checks,
                       company_fx_tested=False, ui_input_dispatched=False,
                       test_kind='Installed CLI subprocesses and local JSON storage fixtures; no physical recording or UI action.')
        write_json(work / 'summary.json', summary)
        write_json(work / 'results.json', results)
        print(json.dumps({key: value for key, value in summary.items() if key != 'checks'}), flush=True)


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
