"""Check recording and review against the same window/step data in real Tk widgets."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_teach_capture import capture, combo


class RecorderStepWindowsTests(unittest.TestCase):
    def test_window_column_detail_and_saved_instructions_after_edit_and_delete(self):
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as error:
            self.skipTest(str(error))
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        manager = SimpleNamespace(exe='Manager.exe', title='Device Manager', check=lambda: None)
        device = '04 Device with a long title for testing'
        steps = []
        for key in ('extra', 'launch', 'action', 'other'):
            step = capture.capture_step(combo())
            step['id'] = key
            steps.append(step)
        steps[2]['_capture_window'] = dict(title=device, exe='Viewer.exe', after_id='launch', related=True)
        steps[3]['_capture_window'] = dict(title='Editor', exe='Editor.exe', mode='existing')
        session = Mock(started=False, ended=False, binding=manager)
        session.notes.configure_mock(state='recording', limit=False)
        session.notes.snapshot.side_effect = lambda: deepcopy(steps)
        session.start.side_effect = lambda: setattr(session, 'started', True)
        session.finish.return_value = dict(status='recorded', steps=deepcopy(steps))
        errors = []

        def widgets(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from widgets(widget)

        def button(text):
            return next(w for w in widgets(root) if isinstance(w, ttk.Button) and w.cget('text') == text)

        def inspect():
            try:
                tree = next(w for w in widgets(root) if w.winfo_name() == 'steps')
                detail = next(w for w in widgets(root) if w.winfo_name() == 'step_window')
                self.assertEqual(tree.heading('window', 'text'), '작업 창')
                for review in (False, True):
                    if review:
                        button('마치고 검토').invoke()
                        root.update()
                    self.assertEqual([tree.set(key, 'window') for key in tree.get_children()],
                                     [manager.title, manager.title, device, 'Editor'])
                    self.assertNotIn(device, tree.set('action', 'instruction'))
                    for key, title, origin, exe in [('extra', manager.title, '기본 창', 'Manager.exe'),
                                                    ('action', device, '2단계에서 열린 창', 'Viewer.exe'),
                                                    ('other', 'Editor', '기존 창', 'Editor.exe')]:
                        tree.selection_set(key)
                        root.update()
                        for field in (title, origin, exe):
                            self.assertIn(field, detail.cget('text'))
                tree.selection_set('extra')
                root.update()
                button('선택 단계 삭제').invoke()
                tree.selection_set('action')
                root.update()
                self.assertIn('1단계에서 열린 창', detail.cget('text'))
                editor = next(w for w in widgets(root) if w.winfo_name() == 'instruction')
                editor.delete(0, 'end')
                editor.insert(0, 'Mode를 N으로 선택')
                button('적용하고 다음 단계').invoke()
                root.update()
                self.assertEqual(tree.set('action', 'instruction'), 'Mode를 N으로 선택')
                button('저장').invoke()
            except Exception as error:
                errors.append(error)
                root.destroy()

        root.after(20, lambda: button('시작').invoke())
        root.after(450, inspect)  # First let the recorder's normal poll render its live rows.
        with patch.object(tk, 'Tk', return_value=root), patch.object(capture, 'CaptureSession', return_value=session):
            result = capture.capture(manager, title=manager.title)
        if errors:
            raise errors[0]
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual([s['id'] for s in result['steps']], ['launch', 'action', 'other'])
        self.assertNotIn('window_context', result['steps'][0])
        self.assertEqual(result['steps'][1]['window_context']['after_step'], 1)
        self.assertEqual(result['steps'][1]['instruction']['text'], 'Mode를 N으로 선택')
        self.assertEqual(result['steps'][2]['window_context']['mode'], 'existing')
