"""Exercise real Tk editing and JSON persistence on an isolated Windows desktop.

The UIA observations are synthetic. This test sends no mouse/keyboard input and
does not demonstrate compatibility with a company's application or provider.
"""
import copy
import argparse
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import traceback
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def isolated_desktop():
    user = C.WinDLL('user32', use_last_error=True)
    user.CreateDesktopW.argtypes = (W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p)
    user.CreateDesktopW.restype = W.HANDLE
    user.SetThreadDesktop.argtypes = (W.HANDLE,)
    desk = user.CreateDesktopW('DesktopSelectReview' + str(os.getpid()), None, None, 0, 0x10000000, None)
    if not desk or not user.SetThreadDesktop(desk):
        raise RuntimeError('isolated_desktop_unavailable')
    return desk


def raw(number, identifier, label=None):
    step = {'id': str(number), 'action': 'teach-target',
            'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': identifier,
                       'name': 'temporary peer caption', 'ancestors': [{'role': 'group', 'automation_id': 'conditions'}]}},
            'recognition': {'target': 'identified', 'action': 'requires_instruction',
                            'result': 'manual', 'reason': 'combo_choice_unproven'}}
    if label:
        step['field_label'] = label
    return step


def exercise(case, skill):
    import tkinter as tk
    from tkinter import ttk, messagebox
    import desktop_select
    import learned_tasks as tasks
    import teach_capture as capture
    import context_teach
    for module in (desktop_select, tasks, capture, context_teach):
        assert Path(module.__file__).resolve().parent == skill / 'scripts', module.__file__

    sessions, errors = [], []
    original_tk = tk.Tk
    initial = [raw(0, 'gcboStatus', None if case == 'unknown-name' else 'MES Setup'), raw(1, 'gcboFamily', 'Family')]
    extra = context_teach.raw_result_step('결과 내보내기',
             {'target': {'role': 'group', 'automation_id': 'Results'}, 'point': [.4, .4], 'path': ['EXPORT', 'EXPORT TO EXCEL']})

    def start(self):
        sessions.append(self)
        self.started = True
        for item in initial:
            self.notes.add(copy.deepcopy(item))

    def finish(self, **_):
        self.ended = True
        return {'status': 'recorded', 'steps': self.notes.snapshot(), 'input_dispatched': False}

    def root_factory():
        root = original_tk()

        def children(parent):
            for child in parent.winfo_children():
                yield child
                yield from children(child)

        def field(name):
            return next(w for w in children(root) if w.winfo_name() == name)

        def click(text):
            next(w for w in children(root) if isinstance(w, ttk.Button) and w.cget('text') == text).invoke()

        def enter(name, value):
            entry = field(name)
            entry.delete(0, 'end')
            entry.insert(0, value)

        def select(number):
            field('steps').selection_set(str(number))
            root.update()

        def preview():
            widget = field('preview')
            return widget.getvar(widget.cget('textvariable'))

        def check_and_save():
            select(0)
            enter('instruction', 'MES SETUP조건을 N으로 해줘')
            if case == 'unknown-name':
                assert 'instruction_target_mismatch' in preview(), preview()
                click('문장의 이름을 이 대상에 연결')
                assert field('alias').get() == 'MES SETUP'
            else:
                enter('alias', '고정조건')
                click('동작 적용')

            if case == 'changed-target':
                changed = raw(0, 'otherStatus', 'Other Condition')
                sessions[0].notes.add(changed)
                click('마치고 검토')
                assert field('instruction').get() == 'MES SETUP조건을 N으로 해줘'
                click('저장')
                assert root.winfo_exists() and 'recorded_target_changed' in preview()
                enter('instruction', 'Other Condition조건을 N으로 해줘')
                click('동작 적용')

            select(1)
            enter('alias', '실행변수')
            enter('instruction', '입력받은 변수를 FAMILY조건에 입력해줘')
            if case == 'peer-change':
                for item in initial:
                    upgraded = copy.deepcopy(item)
                    upgraded['target']['locator'].update(name='different caption', path='0/29', runtime_id=[99, 1])
                    upgraded.update(action='set-value', value='observed',
                                    recognition={'target': 'identified', 'action': 'supported', 'result': 'checkable'})
                    sessions[0].notes.add(upgraded)
            click('마치고 검토')
            # Family's form is still unapplied when teaching the next step.
            click('조회 후 우클릭 메뉴 가르치기')
            root.update()
            click('저장')

        def attempt():
            try:
                check_and_save()
            except BaseException:
                errors.append(traceback.format_exc())
                root.destroy()

        def begin():
            click('시작')
            root.after(300, attempt)

        root.after(20, begin)
        root.after(5000, lambda: (errors.append('GUI timeout'), root.destroy()))
        return root

    with patch.object(tk, 'Tk', root_factory), patch.object(capture._BoundWindow, 'check'), \
            patch.object(capture.CaptureSession, 'start', start), patch.object(capture.CaptureSession, 'finish', finish), \
            patch.object(context_teach, 'teach_context_dialog', return_value=extra), \
            patch.object(messagebox, 'askyesno', return_value=True) as confirm:
        result = capture.capture(Mock(), title='Synthetic form')
    assert not errors, errors
    assert result['status'] == 'recorded', result
    plan = tasks.from_recording('Tk review', 'fixture.exe', 'Synthetic form', result['steps'])
    (ROOT / '.build').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / '.build') as folder:
        tasks.save_task(plan, folder)
        saved = tasks.load_task(plan['name'], folder)
    first, second, third = saved['steps']
    assert first['target'] == ('otherStatus' if case == 'changed-target' else 'gcboStatus'), first
    assert first['instruction']['value'] == 'N', first
    assert first['alias'] == ('MES SETUP' if case == 'unknown-name' else '고정조건'), first
    assert second['target'] == 'gcboFamily' and second['alias'] == '실행변수', second
    assert second['instruction']['parameter'] == 'Family', second
    assert third['action'] == 'result-menu', third
    assert confirm.call_count == (1 if case == 'unknown-name' else 0), confirm.call_args_list
    print(case + ': real Tk instruction/alias editing, mixed-step save and JSON reload passed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case', nargs='?', choices=('peer-change', 'unknown-name', 'changed-target'))
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select',
                        help='Source skill or extracted package whose real Tk runtime will be tested')
    args = parser.parse_args()
    skill = args.skill_dir.resolve()
    if not (skill / 'scripts/teach_capture.py').is_file():
        parser.error('Desktop Select skill directory required')
    sys.path.insert(0, str(skill / 'scripts'))
    if args.case:
        desktop = isolated_desktop()
        exercise(args.case, skill)
    else:
        # A fresh Tcl interpreter/desktop per case also checks that no global
        # alias state is needed to read the stored result in a later session.
        for name in ('peer-change', 'unknown-name', 'changed-target'):
            subprocess.run([sys.executable, '-X', 'utf8', '-B', __file__, name, '--skill-dir', str(skill)], check=True)
