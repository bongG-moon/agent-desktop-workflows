"""Large Windows fonts must not hide the picker actions or overlap rows."""
from pathlib import Path
from contextlib import ExitStack
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Load the same checked bridge as the public entry point.
import recorder_targets as targets


class RecorderPickerLayoutTests(unittest.TestCase):
    def test_100_and_200_percent_scaling_keep_long_caption_and_actions_inside_window(self):
        try:
            import tkinter as tk
            from tkinter import ttk, font
        except ImportError as error:
            self.skipTest(str(error))
        rows = [dict(exe_name='PickerFixture.exe', title='Fixture ' + ('업무 화면 ' * 12) + str(i),
                     hwnd=i + 10, pid=i + 100, process_started_utc_ticks=str(i)) for i in range(2)]

        def children(widget):
            for child in widget.winfo_children():
                yield child
                yield from children(child)

        for scale, screen in ((1, None), (2, None), (2, (1024, 768))):
            with self.subTest(scale=scale, screen=screen):
                self.check_layout(tk, ttk, font, rows, children, scale, screen)

    def check_layout(self, tk, ttk, font, rows, children, scale, screen=None):
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        root.tk.call('tk', 'scaling', scale * 96 / 72)
        errors, checked = [], []

        def check():
            try:
                root.deiconify()
                root.update()
                controls = list(children(root))
                listing = next(w for w in controls if isinstance(w, ttk.Treeview))
                listing.selection_set(listing.get_children()[0])
                listing.event_generate('<<TreeviewSelect>>')
                root.update_idletasks()
                self.assertEqual(listing.cget('style'), 'Steps.Treeview')
                row_height = int(ttk.Style(root).lookup(listing.cget('style'), 'rowheight'))
                default = font.nametofont('TkDefaultFont', root=root)
                self.assertGreater(row_height, default.metrics('linespace'))
                self.assertLessEqual(root.winfo_width(), root.winfo_screenwidth())
                self.assertLessEqual(root.winfo_height(), root.winfo_screenheight())
                caption = next(w for w in controls if isinstance(w, ttk.Entry)
                               and str(w.cget('state')) == 'readonly')
                self.assertEqual(caption.get(), rows[0]['title'])
                for label in ('취소', '이 창에서 녹화', '새로 고침'):
                    button = next(w for w in controls if isinstance(w, ttk.Button) and w.cget('text') == label)
                    self.assertTrue(button.winfo_viewable(), label)
                    self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight(), label)
                    self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), label)
                    bottom = button.winfo_rooty() - root.winfo_rooty() + button.winfo_height()
                    self.assertLessEqual(bottom, root.winfo_height(), label)
                    right = button.winfo_rootx() - root.winfo_rootx() + button.winfo_width()
                    self.assertLessEqual(right, root.winfo_width(), label)
                    self.assertEqual(str(button.cget('style')), 'Primary.TButton'
                                     if label == '이 창에서 녹화' else 'Quiet.TButton')
                self.assertGreater(listing.winfo_height(), row_height)
                first = listing.bbox(listing.get_children()[0])
                self.assertTrue(first, 'the first row must be available for selection')
                self.assertEqual(first[3], row_height)
                self.assertLessEqual(first[1] + first[3], listing.winfo_height(),
                                     'the header and a complete first row must fit')
                checked.append(True)
            except BaseException as error:
                errors.append(error)
            finally:
                root.after_cancel(deadline)
                root.destroy()

        root.after(30, check)
        deadline = root.after(3000, root.destroy)
        with ExitStack() as patches:
            patches.enter_context(patch.object(tk, 'Tk', return_value=root))
            if screen is not None:
                patches.enter_context(patch.object(root, 'winfo_screenwidth', return_value=screen[0]))
                patches.enter_context(patch.object(root, 'winfo_screenheight', return_value=screen[1]))
            targets.choose_window('Fixture', rows)
        if errors:
            raise errors[0]
        self.assertEqual(checked, [True])
