"""Large Windows fonts must not hide the picker actions or overlap rows."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Load the same checked bridge as the public entry point.
import recorder_targets as targets


class RecorderPickerLayoutTests(unittest.TestCase):
    def test_large_font_and_selected_caption_keep_actions_inside_window(self):
        try:
            import tkinter as tk
            from tkinter import ttk, font
        except ImportError as error:
            self.skipTest(str(error))
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        default = font.nametofont('TkDefaultFont', root=root)
        default.configure(size=18)
        errors = []
        checked = []
        rows = [dict(exe_name='PickerFixture.exe', title='Fixture ' + ('업무 화면 ' * 12) + str(i),
                     hwnd=i + 10, pid=i + 100, process_started_utc_ticks=str(i)) for i in range(2)]

        def children(widget):
            for child in widget.winfo_children():
                yield child
                yield from children(child)

        def check():
            try:
                root.deiconify()
                root.update_idletasks()
                controls = list(children(root))
                listing = next(w for w in controls if isinstance(w, ttk.Treeview))
                listing.selection_set(listing.get_children()[0])
                listing.event_generate('<<TreeviewSelect>>')
                root.update_idletasks()
                row_height = int(ttk.Style(root).lookup(listing.cget('style'), 'rowheight'))
                self.assertGreater(row_height, default.metrics('linespace'))
                for label in ('취소', '이 창에서 녹화', '새로 고침'):
                    button = next(w for w in controls if isinstance(w, ttk.Button) and w.cget('text') == label)
                    self.assertTrue(button.winfo_viewable(), label)
                    self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight(), label)
                    bottom = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height()
                    self.assertLessEqual(bottom, root.winfo_height(), label)
                self.assertGreater(listing.winfo_height(), row_height)
                checked.append(True)
            except BaseException as error:
                errors.append(error)
            finally:
                root.after_cancel(deadline)
                root.destroy()

        root.after(30, check)
        deadline = root.after(3000, root.destroy)
        with patch.object(tk, 'Tk', return_value=root):
            targets.choose_window('Fixture', rows)
        if errors:
            raise errors[0]
        self.assertEqual(checked, [True])
