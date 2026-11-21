"""Real Tk checks for the recorder's non-native menu interaction paths."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))

try:
    import tkinter as tk
    from tkinter import ttk
    import recorder_theme
    from recorder_menu import PopupMenu
except ImportError:
    tk = None


@unittest.skipIf(tk is None, 'Tk is unavailable')
class RecorderMenuTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)
        recorder_theme.configure(self.root)
        self.root.geometry('420x220+80+80')
        self.button = ttk.Menubutton(self.root, text='단계 편집')
        self.button.pack(anchor='w', padx=20, pady=20)
        self.other = ttk.Entry(self.root)
        self.other.pack()
        self.menu = PopupMenu(self.button, tearoff=False)
        self.menu.bind_to(self.button)
        self.root.update()
        self.root.focus_force()
        self.button.focus_set()
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.assertFalse(self.errors, self.errors)

    def open(self, keyboard=False):
        event = '<KeyPress-Down>' if keyboard else '<ButtonPress-1>'
        self.button.event_generate(event, x=5, y=5)
        self.root.update()
        self.assertTrue(self.menu.is_posted)
        self.assertFalse(self.menu.winfo_ismapped(), 'native OS menu was posted')
        view = self.menu._session.views[0]
        self.assertTrue(view.window.winfo_viewable())
        return view

    def test_mouse_opens_modern_menu_and_command_runs_once_on_release(self):
        invoke = Mock()
        self.menu.add_command(label='선택 단계 삭제', description='선택한 단계만 삭제합니다',
                              destructive=True, command=invoke)
        view = self.open()
        self.button.event_generate('<ButtonRelease-1>', x=5, y=5)
        self.root.update()
        self.assertTrue(self.menu.is_posted)
        label = view.rows[0].winfo_children()[0]
        self.assertEqual(label.cget('foreground'), '#BD3B46')
        self.assertEqual(len(view.rows[0].winfo_children()), 2)
        label.event_generate('<ButtonPress-1>', x=3, y=3)
        label.event_generate('<ButtonRelease-1>', x=3, y=3)
        self.root.update()
        invoke.assert_called_once_with()
        self.assertFalse(self.menu.is_posted)
        self.assertIsNone(self.root.grab_current())

    def test_native_model_remains_discoverable_and_metadata_survives_delete(self):
        first, second = Mock(), Mock()
        self.menu.add_command(label='old', description='old description', command=first)
        self.menu.add_separator()
        self.menu.add_command(label='kept', description='kept description', command=second)
        self.menu.delete(0, 1)
        self.menu.entryconfigure(0, label='new', description='new description', destructive=True)
        self.assertIs(self.button.nametowidget(str(self.button.cget('menu'))), self.menu)
        self.assertEqual(self.menu.index('end'), 0)
        self.assertEqual(self.menu.type(0), 'command')
        self.assertEqual(self.menu.entrycget(0, 'description'), 'new description')
        self.menu.invoke(0)
        first.assert_not_called()
        second.assert_called_once_with()

    def test_keyboard_skips_disabled_and_separator_and_invokes_once(self):
        disabled, first, second = Mock(), Mock(), Mock()
        self.menu.add_command(label='disabled', command=disabled, state='disabled')
        self.menu.add_separator()
        self.menu.add_command(label='first', command=first)
        self.menu.add_command(label='second', command=second)
        view = self.open(keyboard=True)
        self.assertEqual(view.selected, 2)
        view.window.event_generate('<Down>')
        self.root.update()
        self.assertEqual(view.selected, 3)
        view.window.event_generate('<Return>')
        self.root.update()
        disabled.assert_not_called()
        first.assert_not_called()
        second.assert_called_once_with()
        self.assertFalse(self.menu.is_posted)

    def test_disabled_mouse_row_does_not_invoke(self):
        invoke = Mock()
        self.menu.add_command(label='disabled', state='disabled', command=invoke)
        view = self.open()
        view.rows[0].event_generate('<ButtonRelease-1>', x=3, y=3)
        self.root.update()
        invoke.assert_not_called()
        self.assertTrue(self.menu.is_posted)
        view.window.event_generate('<Escape>')
        self.root.update()
        self.assertFalse(self.menu.is_posted)

    def test_cascade_keeps_parent_open_and_keyboard_back_works(self):
        invoke = Mock()
        submenu = PopupMenu(self.menu)
        submenu.add_command(label='행 버튼 지정', command=invoke)
        self.menu.add_cascade(label='위치 직접 보완', menu=submenu)
        parent = self.open(keyboard=True)
        parent.window.event_generate('<Right>')
        self.root.update()
        session = self.menu._session
        self.assertEqual(len(session.views), 2)
        child = session.views[-1]
        self.assertTrue(parent.window.winfo_viewable())
        self.assertFalse(submenu.winfo_ismapped())
        child.window.event_generate('<Escape>')
        self.root.update()
        self.assertEqual(len(session.views), 1)
        self.assertTrue(self.menu.is_posted)
        parent.window.event_generate('<Right>')
        self.root.update()
        session.views[-1].window.event_generate('<Return>')
        self.root.update()
        invoke.assert_called_once_with()
        self.assertFalse(self.menu.is_posted)
        self.assertFalse(submenu.is_posted)

    def test_outside_press_dismisses_and_does_not_invoke_other_control(self):
        self.menu.add_command(label='command', command=Mock())
        view = self.open()
        invoke = Mock()
        self.other.bind('<ButtonPress-1>', invoke)
        view.window.event_generate('<ButtonPress-1>', x=-30, y=-30,
                                   rootx=view.window.winfo_rootx() - 30,
                                   rooty=view.window.winfo_rooty() - 30)
        self.root.update()
        self.assertFalse(self.menu.is_posted)
        self.assertIsNone(self.root.grab_current())
        invoke.assert_not_called()

    def test_focus_loss_dismisses_without_restoring_menu_focus(self):
        self.menu.add_command(label='command')
        self.open(keyboard=True)
        self.other.focus_set()
        self.root.update()
        self.assertFalse(self.menu.is_posted)
        self.assertIs(self.root.focus_get(), self.other)
        self.assertIsNone(self.root.grab_current())

    def test_rebuild_closes_parent_or_child_without_leaving_stale_callback(self):
        invoke = Mock()
        submenu = PopupMenu(self.menu)
        submenu.add_command(label='stale', command=invoke)
        self.menu.add_cascade(label='more', menu=submenu)
        parent = self.open(keyboard=True)
        parent.window.event_generate('<Right>')
        self.root.update()
        submenu.delete(0, 'end')
        submenu.add_command(label='new')
        self.root.update()
        self.assertFalse(self.menu.is_posted)
        self.assertFalse(submenu.is_posted)
        self.assertIsNone(self.root.grab_current())
        invoke.assert_not_called()
        self.open()
        self.menu.delete(0, 'end')
        self.root.update()
        self.assertFalse(self.menu.is_posted)

    def test_existing_modal_grab_is_retained_and_bindings_are_removed(self):
        self.menu.add_command(label='command')
        self.root.grab_set()
        self.open()
        self.menu.unpost()
        self.assertIs(self.root.grab_current(), self.root)
        self.root.grab_release()
        blocker = tk.Toplevel(self.root)
        blocker.update()
        blocker.grab_set()
        self.button.event_generate('<ButtonPress-1>', x=5, y=5)
        self.root.update()
        self.assertFalse(self.menu.is_posted)
        self.assertIs(self.root.grab_current(), blocker)
        blocker.grab_release()
        blocker.destroy()
        binding = self.button.bind('<ButtonPress-1>')
        self.menu.bind_to(self.button)
        self.assertEqual(self.button.bind('<ButtonPress-1>'), binding)
        self.menu.destroy()
        self.assertFalse(self.button.bind('<ButtonPress-1>'))
        self.assertFalse(self.button.bind('<KeyPress-Down>'))

    def test_anchor_near_screen_bottom_places_popup_above_and_within_bounds(self):
        self.menu.add_command(label='command', description='An explanation')
        left = self.root.winfo_rootx() - 5
        top = self.button.winfo_rooty() - 100
        right = left + 320
        bottom = self.button.winfo_rooty() + self.button.winfo_height() + 5
        with patch('recorder_menu._screen_bounds', return_value=(left, top, right, bottom)):
            view = self.open()
        self.assertLess(view.window.winfo_rooty(), self.button.winfo_rooty())
        self.assertGreaterEqual(view.window.winfo_rootx(), left)
        self.assertLessEqual(view.window.winfo_rootx() + view.window.winfo_width(), right)
        self.assertLessEqual(view.window.winfo_rooty() + view.window.winfo_height(), bottom)

    def test_destroying_anchor_closes_live_popup_and_releases_grab(self):
        self.menu.add_command(label='command')
        view = self.open(keyboard=True)
        self.button.destroy()
        self.root.update()
        self.assertFalse(view.window.winfo_exists())
        self.assertIsNone(self.root.grab_current())


if __name__ == '__main__':
    unittest.main()
