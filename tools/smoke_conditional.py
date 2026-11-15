"""Replay two painted device windows; use test-only image marks as a deterministic local host."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_040_rebuild import install_skill
from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]


def marker(pixels, width, rgb):
    """Find the unique 3-by-3 test mark in the image actually sent to the host."""
    bgra = bytes((*reversed(rgb), 0))
    points = []
    offset = pixels.find(bgra * 3)
    while offset != -1:
        if offset % 4 == 0:
            points.append(((offset // 4) % width, (offset // 4) // width))
        offset = pixels.find(bgra * 3, offset + 1)
    if not points:
        return None
    x, y = points[0]
    if points != [(x, y), (x, y+1), (x, y+2)]:
        raise AssertionError('Fixture marker ambiguous')
    return x + 1, y + 1


def exercise(source, work, seconds):
    installed = work/'skills/desktop-select'
    install_skill(source, installed)
    sys.path.insert(0, str(installed/'scripts'))
    import desktop_select  # Load the package's own Windows bridge.
    import grounding

    exe = work/'ConditionalSceneFixture.exe'
    compiler = Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', '/out:' + str(exe), '/r:System.Windows.Forms.dll',
                    '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
                    str(ROOT/'tests/fixtures/ConditionalSceneFixture.cs')], check=True)
    fixture = Fixture(exe)
    cli = [sys.executable, '-X', 'utf8', '-B', str(installed/'scripts/python_workflow.py')]
    env = dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(work/'tasks'))
    checks, decisions = [], []

    def check(case, passed):
        checks.append(dict(case=case, passed=bool(passed)))
        if not passed:
            raise AssertionError(case)

    def command(args):
        proc = subprocess.run(cli + args, cwd=work, env=env, capture_output=True, encoding='utf8', timeout=120)
        reply = json.loads(proc.stdout)
        if reply.get('status') not in ('needs_scene', 'completed_unverified', 'verified'):
            raise AssertionError((args[0], reply.get('diagnostic'), reply.get('steps')))
        return reply

    def capture(reply, phase):
        check('fresh_scene_' + phase, reply.get('status') == 'needs_scene' and reply['phase'] == phase)
        window, pixels = grounding._pixels(Path(reply['image']).read_bytes())
        check('frame_dimensions_' + phase, window[2:] == [reply['width'], reply['height']])
        return marker(pixels, window[2], (223, 3, 198)), marker(pixels, window[2], (17, 199, 151))

    def answer(reply, decision, point):
        decisions.append(decision)
        return command(['scene', '--ticket', reply['ticket'], '--decision', decision,
                        '--point', str(point[0]), str(point[1])])

    try:
        opens = [dict(action='invoke', selector=dict(role='button', automation_id=f'Launch{i}'))
                 for i in range(2)]
        scenes = [dict(action='conditional-open', label='광고가 있으면 닫고 게임을 실행해',
                       seconds=seconds, when_present=dict(name='광고'), dismiss=dict(name='X'),
                       always=dict(name='게임'), window_context=dict(exe=exe.name, title=f'Painted device {i}',
                                                                 after_step=i+1, related=True))
                  for i in range(2)]
        recipe = dict(schema=1, name='Two painted devices', exe=exe.name, window=fixture.ready['title'],
                      steps=opens + scenes)
        recipe_path = work/'recipe.json'
        recipe_path.write_text(json.dumps(recipe, ensure_ascii=False), encoding='utf8')
        started = time.monotonic()
        reply = command(['run', '--recipe', str(recipe_path)])
        close, game = capture(reply, 'condition')
        check('game_already_visible_behind_ad', close is not None and game is not None)
        check('wait_before_first_scene', time.monotonic() - started >= seconds)
        state = fixture.command('state')
        check('both_openers_dispatched_once', state['opens'] == [1, 1])
        check('first_scene_no_game_input', all(d['launched'] == d['closed'] == 0 for d in state['devices']))

        reply = answer(reply, 'present', close)
        close, game = capture(reply, 'after_dismiss')
        check('ad_closed_before_game', close is None and game is not None)
        state = fixture.command('state')
        check('dismiss_once_on_correct_device', [d['closed'] for d in state['devices']] == [1, 0]
              and all(d['launched'] == 0 for d in state['devices']))

        started = time.monotonic()
        reply = answer(reply, 'absent', game)
        close, game = capture(reply, 'condition')
        check('wait_before_second_scene', time.monotonic() - started >= seconds)
        check('other_device_has_game_and_no_ad', close is None and game is not None)
        reply = answer(reply, 'absent', game)
        check('recording_completed', reply.get('status') in ('completed_unverified', 'verified') and len(reply['steps']) == 4)
        state = fixture.command('state')
        check('game_launched_once_in_each_window', [d['launched'] for d in state['devices']] == [1, 1])
        check('no_extra_input_on_resume', state['opens'] == [1, 1]
              and [d['closed'] for d in state['devices']] == [1, 0]
              and all(d['missed'] == d['premature'] == 0 for d in state['devices']))
        print(json.dumps(dict(checks=len(checks), decisions=decisions, opens=state['opens'],
                              devices=state['devices'], seconds_per_device=seconds)))
    finally:
        fixture.close()
        (work/'summary.json').write_text(json.dumps(checks, indent=2), encoding='utf8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT/'skills/desktop-select')
    parser.add_argument('--work-dir', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=10, choices=range(0, 121), metavar='0..120')
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=False)
    exercise(args.skill_dir.resolve(), work, args.seconds)
