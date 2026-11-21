"""App hints use current metadata; uncertainty stays an explicit local choice."""
from contextlib import ExitStack
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Loads and checks the shared bridge without creating UIA.
import recorder_targets as targets
import recorder_theme


def row(hwnd, title, exe='Example.exe', pid=7):
    return dict(hwnd=hwnd, title=title, exe_name=exe, pid=pid, process_started_utc_ticks='1234')


class RecorderInventoryTests(unittest.TestCase):
    def inventory(self, rows, *, inaccessible=(), incomplete=False, limit=None):
        sources = {r['hwnd']: r for r in rows}
        identities = {}
        for value in rows:
            identities[value['pid']] = value['exe_name'], value['process_started_utc_ticks']

        def identity(pid):
            if pid in inaccessible:
                raise targets.bridge.BridgeError('target_unavailable')
            return identities[pid]

        def enumerate_windows(callback, arg):
            for hwnd in sources:
                if not callback(hwnd, arg):
                    return False
            return not incomplete

        with ExitStack() as stack:
            for name, values in (
                    ('_windows', {}),
                    ('root_window', {'side_effect': lambda h: h}),
                    ('hwnd_pid', {'side_effect': lambda h: sources[h]['pid']}),
                    ('window_text', {'side_effect': lambda h: sources[h]['title']}),
                    ('window_class', {'return_value': 'BusinessWindow'}),
                    ('_sig', {'side_effect': lambda _, name, *args: (
                        (lambda hwnd: len(sources[hwnd]['title'])) if name == 'GetWindowTextLengthW'
                        else enumerate_windows)}),
                    ('foreground', {'side_effect': AssertionError('foreground is not a selection rule')}),
                    ('UIA', {'side_effect': AssertionError('window inventory must not create UIA')})):
                stack.enter_context(patch.object(targets.bridge, name, **values))
            stack.enter_context(patch.object(targets.C, 'WINFUNCTYPE', return_value=lambda callback: callback,
                                            create=True))
            stack.enter_context(patch.object(targets.bridge, 'user32',
                               SimpleNamespace(IsWindowVisible=lambda hwnd: True)))
            lookup = stack.enter_context(patch.object(targets.bridge, '_process_identity', side_effect=identity))
            if limit is not None:
                stack.enter_context(patch.object(targets, '_MAX_WINDOWS', limit))
            return targets.inventory(), lookup

    def test_preserves_literal_captions_and_duplicate_hwnds_with_one_identity_read_per_pid(self):
        title = '조건  조회 "A" & [$value] (1) '
        first, second = row(42, title), row(43, title)
        third = row(44, 'Other', 'Other.exe', 8)
        actual, lookup = self.inventory([first, second, third])
        self.assertEqual(actual, [first, second, third])
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(actual[0]['title'], title)

    def test_inaccessible_pid_is_skipped_without_repeated_process_reads(self):
        actual, lookup = self.inventory([row(42, 'A'), row(43, 'B'), row(44, 'C', pid=8)],
                                        inaccessible=(7,))
        self.assertEqual(actual, [row(44, 'C', pid=8)])
        self.assertEqual(lookup.call_count, 2)

    def test_empty_or_truncated_titles_are_not_exact_recording_candidates(self):
        actual, lookup = self.inventory([row(1, '', pid=10), row(2, '  ', pid=11),
                                         row(3, 'x' * 1023, pid=12), row(4, ' Kept ')])
        self.assertEqual(actual, [row(4, ' Kept ')])
        self.assertNotIn(10, [call.args[0] for call in lookup.call_args_list])

    def test_incomplete_inventory_is_never_treated_as_a_unique_match(self):
        for options in ({'incomplete': True}, {'limit': 2}):
            with self.subTest(options=options), self.assertRaisesRegex(targets.bridge.BridgeError,
                                                                      'window_list_incomplete'):
                self.inventory([row(1, 'A'), row(2, 'B')], **options)


class RecorderMatchTests(unittest.TestCase):
    def test_exact_exe_stem_and_title_win_over_partial_and_typo_candidates(self):
        exact = row(1, '업무  "A" ', 'FactoryAgent.exe')
        others = [row(2, 'FactoryAgent Tools', 'Other.exe'),
                  row(3, 'FactoryAgnet', 'Third.exe')]
        for hint in ('FactoryAgent.exe', 'factoryagent', exact['title']):
            with self.subTest(hint=hint):
                self.assertEqual(targets.match_windows(hint, [*others, exact]), [exact])

    def test_exact_ties_and_duplicate_titles_keep_all_hwnds_in_input_order(self):
        rows = [row(2, 'Same', 'Same.exe'), row(1, 'Same', 'Different.exe')]
        self.assertEqual(targets.match_windows('same', rows), rows)

    def test_normalized_partial_title_and_exe_match_without_mutating_rows(self):
        rows = [row(1, '조건 조회 — ｍｅｎｕ Agent  ', 'ShellHost.exe'),
                row(2, 'Task', 'MenuAgentClient.exe')]
        before = [dict(value) for value in rows]
        self.assertEqual(targets.match_windows('MENU-AGENT', rows), rows)
        self.assertEqual(rows, before)

    def test_generic_single_edit_and_adjacent_transposition_match_title_or_executable(self):
        cases = [('FXMENUAGNET', 'FxMenuAgent'), ('Warehosue', 'Warehouse'),
                 ('Warehous', 'Warehouse'), ('Warehhouse', 'Warehouse'),
                 ('Warehouze', 'Warehouse')]
        for hint, name in cases:
            with self.subTest(hint=hint, location='title'):
                title = row(1, 'Vendor ' + name + ' - Production', 'ShellHost.exe')
                self.assertEqual(targets.match_windows(hint, [title]), [title])
            with self.subTest(hint=hint, location='exe'):
                exe = row(2, '업무', name + '.exe')
                self.assertEqual(targets.match_windows(hint + '.exe', [exe]), [exe])

    def test_fuzzy_ties_are_not_broken_by_distance_or_row_order(self):
        rows = [row(1, 'Warehouse'), row(2, 'Warehousx'), row(3, 'Warehouse')]
        self.assertEqual(targets.match_windows('Warehouze', rows), [rows[0], rows[2]])
        self.assertEqual(targets.match_windows('Warehousz', rows), rows)

    def test_short_hints_two_edits_sentences_and_giant_caption_tokens_are_not_fuzzy(self):
        cases = [('Foam', 'Form'), ('Wazehouze', 'Warehouse'),
                 ('Vendor Warehosue', 'Vendor Warehouse'),
                 ('a' * 90 + 'b', 'a' * 91)]
        for hint, title in cases:
            with self.subTest(hint=hint):
                self.assertEqual(targets.match_windows(hint, [row(1, title)]), [])

    def test_empty_or_punctuation_hint_does_not_match_everything(self):
        for hint in (None, '', '  ', '---'):
            self.assertEqual(targets.match_windows(hint, [row(1, 'A')]), [])

    def test_unique_match_uses_snapshot_without_importing_or_opening_a_picker(self):
        chosen = row(1, 'FactoryAgent')
        ready = Mock()
        with patch.object(targets, '_choose_window') as picker:
            self.assertIs(targets.choose_window('FactoryAgnet', [chosen], ready), chosen)
        picker.assert_not_called()
        ready.assert_not_called()


class PickerHarness:
    """Small widget stand-ins exercise the picker without a desktop or event timing."""
    class Variable:
        def __init__(self, value=''):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class TclError(Exception):
        pass

    def __init__(self):
        self.root = Mock()
        self.root.winfo_viewable.return_value = False
        self.root.winfo_fpixels.return_value = 96
        self.root.winfo_screenwidth.return_value = 1280
        self.root.winfo_screenheight.return_value = 800
        self.font = Mock()
        self.font.metrics.return_value = 20
        self.font.measure.side_effect = lambda value: len(value) * 10
        self.root_callbacks, self.tree_callbacks, self.buttons = {}, {}, {}
        self.values, self.selected, self.variables = {}, (), []
        self.root.bind.side_effect = lambda event, fn, **kw: self.root_callbacks.update({event: fn})
        self.root.protocol.side_effect = lambda event, fn: self.root_callbacks.update({event: fn})
        self.tree = Mock()
        self.tree.bind.side_effect = lambda event, fn: self.tree_callbacks.update({event: fn})
        self.tree.get_children.side_effect = lambda: tuple(self.values)
        self.tree.selection.side_effect = lambda: self.selected
        self.tree.insert.side_effect = lambda parent, place, iid, values: self.values.update({iid: values})

        def delete(key):
            self.values.pop(key)
            self.selected = ()

        def variable(**kwargs):
            value = self.Variable(**kwargs)
            self.variables.append(value)
            return value

        def button(parent, **kwargs):
            widget = Mock()
            widget.command = kwargs['command']
            widget.state = kwargs.get('state', 'normal')
            widget.configure.side_effect = lambda **options: setattr(widget, 'state', options['state'])
            self.buttons[kwargs['text']] = widget
            return widget

        self.tree.delete.side_effect = delete
        self.ttk = SimpleNamespace(Frame=Mock(), Label=Mock(), Entry=Mock(), Scrollbar=Mock(),
                                   Button=button, Treeview=Mock(return_value=self.tree))
        self.tk = SimpleNamespace(Tk=Mock(return_value=self.root), StringVar=variable,
                                  TclError=self.TclError, ttk=self.ttk)

    def choose(self, hint, rows, ready=None):
        with patch.dict(sys.modules, {'tkinter': self.tk, 'tkinter.ttk': self.ttk}), \
                patch.object(recorder_theme, 'configure', return_value={'font': self.font}):
            return targets.choose_window(hint, rows, ready)


class RecorderPickerTests(unittest.TestCase):
    def test_ready_requires_mapped_root_and_explicit_selection_preserves_literal_row(self):
        ui, ready = PickerHarness(), Mock()
        rows = [row(42, '업무 "A" '), row(43, '업무 "A" ')]

        def interact():
            ready.assert_not_called()
            self.assertEqual(ui.selected, ())
            self.assertEqual(ui.buttons['이 창에서 녹화'].state, 'disabled')
            self.assertEqual(ui.values['1'], (rows[1]['title'], 'Example.exe', 7, 43))
            ui.root.winfo_viewable.return_value = True
            ui.root_callbacks['<Map>'](SimpleNamespace(widget=ui.tree))
            ready.assert_not_called()
            ui.root_callbacks['<Map>'](SimpleNamespace(widget=ui.root))
            ui.root_callbacks['<Map>'](SimpleNamespace(widget=ui.root))
            ready.assert_called_once_with()
            ui.selected = ('1',)
            ui.tree_callbacks['<<TreeviewSelect>>']()
            self.assertEqual(ui.buttons['이 창에서 녹화'].state, 'normal')
            self.assertEqual(ui.variables[3].get(), rows[1]['title'])
            self.assertIn('창 번호: 43', ui.variables[2].get())
            ui.buttons['이 창에서 녹화'].command()

        ui.root.mainloop.side_effect = interact
        self.assertIs(ui.choose('Example', rows, ready), rows[1])

    def test_no_match_shows_all_current_candidates_with_notice_and_cancellation(self):
        ui = PickerHarness()
        rows = [row(1, 'First', 'First.exe'), row(2, 'Second', 'Second.exe')]

        def interact():
            self.assertEqual(len(ui.values), 2)
            self.assertIn('모든 현재 창', ui.variables[1].get())
            self.assertEqual(ui.selected, ())
            ui.buttons['취소'].command()

        ui.root.mainloop.side_effect = interact
        self.assertIsNone(ui.choose('Unavailable', rows))

    def test_refresh_replaces_snapshots_and_never_selects_even_a_unique_new_match(self):
        ui = PickerHarness()
        rows = [row(1, 'A'), row(2, 'B')]
        replacement = row(3, 'New title  ')

        def interact():
            ui.selected = ('1',)
            ui.tree_callbacks['<<TreeviewSelect>>']()
            ui.buttons['새로 고침'].command()
            self.assertEqual(ui.selected, ())
            self.assertEqual(ui.buttons['이 창에서 녹화'].state, 'disabled')
            self.assertEqual(ui.values, {'0': (replacement['title'], 'Example.exe', 7, 3)})
            ui.selected = ('0',)
            ui.tree_callbacks['<<TreeviewSelect>>']()
            ui.buttons['이 창에서 녹화'].command()

        ui.root.mainloop.side_effect = interact
        with patch.object(targets, 'inventory', return_value=[replacement]) as refresh:
            self.assertIs(ui.choose('Example', rows), replacement)
        refresh.assert_called_once_with()

    def test_close_before_map_has_no_readiness_event(self):
        ui, ready = PickerHarness(), Mock()
        ui.root.mainloop.side_effect = lambda: ui.root_callbacks['WM_DELETE_WINDOW']()
        self.assertIsNone(ui.choose('Unmatched', [], ready))
        ready.assert_not_called()

    def test_tk_creation_failure_uses_terminal_recorder_diagnostic(self):
        ui = PickerHarness()
        ui.tk.Tk.side_effect = ui.TclError('no display')
        with self.assertRaisesRegex(targets.bridge.BridgeError, 'recording_ui_failed'):
            ui.choose('Unmatched', [])


if __name__ == '__main__':
    unittest.main()
