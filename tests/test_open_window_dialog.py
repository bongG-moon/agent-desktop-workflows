"""Exercise stale list selection and cancellation with the actual Tk window picker."""
from pathlib import Path
import sys
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import capture_windows
from teach_capture import pick_recording_window


class OpenWindowDialogTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except (ImportError, RuntimeError, OSError) as error:
            self.skipTest(f'Tk unavailable: {error}')
        except tk.TclError as error:
            self.skipTest(f'No Tk display: {error}')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        primary = SimpleNamespace(binding=object())
        self.session = SimpleNamespace(choice_scope=lambda: ((primary.binding,), primary.binding), ended=False,
                                       active_hint='', connect_choice=Mock())
        self.row = dict(title='Second device', exe='Viewer.exe', hwnd=2, pid=3, created='4',
                        current=False, connected=False, minimized=False, ambiguous=False)

    def drive(self, inspect, scan):
        deadline = time.monotonic() + 5
        errors = []
        def tick():
            dialog = self.root.nametowidget('window_picker')
            widgets = {}
            def walk(parent):
                for child in parent.winfo_children():
                    widgets[child.winfo_name()] = child
                    walk(child)
            walk(dialog)
            try:
                self.assertLess(time.monotonic(), deadline, 'Dialog did not reach expected state')
                inspect(widgets)
            except Exception as error:
                errors.append(error)
                dialog.destroy()
            if dialog.winfo_exists():
                self.root.after(10, tick)
        self.root.after(10, tick)
        with patch.object(capture_windows, 'window_choices', side_effect=scan, create=True):
            result = pick_recording_window(self.root, self.session)
        if errors:
            raise errors[0]
        return result

    def test_stale_choice_stays_open_and_refresh_requires_a_new_selection(self):
        selected = []
        def connect(item):
            selected.append(item['created'])
            if len(selected) == 1:
                self.session.active_hint = '같은 프로그램에 같은 제목의 창이 생겼습니다.'
                return False
            return True
        self.session.connect_choice.side_effect = connect
        def scan(connected, active, *, all_windows):
            self.assertFalse(all_windows)
            return [dict(self.row, created='5' if selected else '4')]
        def inspect(w):
            tree = w['windows']
            if not tree.get_children() or w['refresh'].instate(['disabled']):
                return
            if selected == ['4'] and w['use'].instate(['disabled']) and not tree.selection():
                if self.session.active_hint:
                    w['refresh'].invoke()
                    self.session.active_hint = ''
                    return
            tree.selection_set(tree.get_children()[0])
            tree.event_generate('<<TreeviewSelect>>')
            w['use'].invoke()
        self.assertEqual(self.drive(inspect, scan), 'selected')
        self.assertEqual(selected, ['4', '5'])

    def test_cancel_during_scan_dispatches_nothing(self):
        release = Event()
        def scan(*_, **kwargs):
            release.wait(5)
            return [self.row]
        try:
            result = self.drive(lambda w:w['cancel'].invoke(), scan)
        finally:
            release.set()
        self.assertIsNone(result)
        self.session.connect_choice.assert_not_called()
