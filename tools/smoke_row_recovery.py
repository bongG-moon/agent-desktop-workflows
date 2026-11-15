"""Recover an unresolved row from a window image, reorder the list, then replay the same recording."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_040_rebuild import install_skill
from smoke_native import Fixture
from smoke_painted_rows import compile_fixture


ROOT = Path(__file__).resolve().parents[1]


def exercise(source, work):
    skill = work / 'skills/desktop-select'
    install_skill(source, skill)
    scripts = skill / 'scripts'
    sys.path.insert(0, str(scripts))
    import desktop_select as app
    import row_vision

    app.bridge.set_dpi_awareness()
    exe = compile_fixture(work)
    fixture = Fixture(exe, '--linked')
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work / 'tasks'))
    task_name = 'Named row details'

    def command(*arguments):
        proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(scripts / 'desktop_select.py'),
                               *arguments, '--task', task_name], env=env, cwd=work,
                              capture_output=True, encoding='utf8', timeout=90)
        if not proc.stdout.strip():
            raise RuntimeError((proc.returncode, proc.stderr))
        return json.loads(proc.stdout)

    try:
        title = fixture.ready['title']
        target = 'Atlas details'
        opener = dict(action='open', target=target, match='query', role='button', expand=False,
                      instruction=dict(mode='recorded', text=target + ' 버튼 누르기'))
        child = dict(action='open', target='Confirm', match='automation_id', role='button', expand=False,
                     window_context=dict(exe=exe.name, title='Details Atlas', after_step=1, related=True))
        original = dict(schema=1, name=task_name, exe=exe.name, window=title, steps=[opener, child])
        path = app.tasks.save_task(original, work / 'tasks')
        before = path.read_bytes()
        scope = ['--exe', exe.name, '--window', title, '--hwnd', str(fixture.ready['hwnd'])]
        unresolved = command('replay', *scope)
        assert unresolved.get('status') == 'needs_grounding' and unresolved['input_dispatched'] is False, unresolved
        fixture.command('activate')
        observed = command('ground', '--only', '1')
        assert observed.get('row_binding_command') == 'bind-row' and observed['input_dispatched'] is False, observed
        binding = app.bridge.Binding(app.pick_window(app.windows(exe.name), title, fixture.ready['hwnd']))
        left, top, _, _ = row_vision._window_rect(binding)
        rect = sorted(row_vision.visible_rows(binding), key=lambda row: row[1])[0]
        button_x, button_y = fixture.command('state')['points'][0]
        bound = command('bind-row', '--only', '1', '--frame', observed['frame'],
                        '--row-point', str(rect[0] + 48 - left), str(rect[1] + 26 - top),
                        '--point', str(button_x - left), str(button_y - top))
        assert bound.get('status') == 'bound' and bound['input_dispatched'] is False, bound
        assert fixture.command('reset-reorder')['order'] == ['Cobalt', 'Birch', 'Atlas']
        for run in (1, 2):
            result = command('replay', *scope)
            assert result.get('status') in ('completed_unverified', 'verified'), result
            assert len(result['steps']) == 2 and result['steps'][0]['receipt']['input_dispatched'] is (run == 1), result
            deadline = time.monotonic() + 2
            state = fixture.command('state')
            while state['childCalls'].get('Atlas') != run and time.monotonic() < deadline:
                time.sleep(.05)
                state = fixture.command('state')
            assert state['calls'] == {'Atlas': 1, 'Birch': 0, 'Cobalt': 0} and state['childCalls'] == {'Atlas': run}, state
        registered = skill.parent / 'details-check'
        registration = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(scripts / 'python_workflow.py'),
                                       'register', '--task', task_name, '--out', str(registered)],
                                      env=env, capture_output=True, encoding='utf8', timeout=60)
        assert json.loads(registration.stdout)['status'] == 'registered', registration.stdout
        preview = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(registered / 'scripts/run.py'),
                                  '--read-only'], env=env, capture_output=True, encoding='utf8', timeout=60)
        observed_recipe = json.loads(preview.stdout)
        assert observed_recipe['status'] == 'observed' and observed_recipe['input_dispatched'] is False, observed_recipe
        assert path.read_bytes() == before
        print(json.dumps(dict(initial_handoff=unresolved['host_action'], bound_without_input=True, reordered=True,
                              launches=state['calls'], detail_actions=state['childCalls'], registered=True,
                              original_preserved=True)))
    finally:
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    exercise(args.skill_dir.resolve(), work)
