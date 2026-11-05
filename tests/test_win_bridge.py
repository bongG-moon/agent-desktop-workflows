"""Guard contracts without desktop input; real UIA cases live in smoke_native."""
import ctypes
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import win_bridge as w


class Properties:
    def __init__(self, values=None):
        self.values = values or {}
        self.reads = []
    def property(self, key):
        self.reads.append(key)
        return self.values.get(key)


class BridgeTests(unittest.TestCase):
    def test_native_layout_matches_windows_x64_abi(self):
        self.assertEqual(ctypes.sizeof(w.GUID), 16)
        self.assertEqual(ctypes.sizeof(w.VARIANT), 24)
        self.assertEqual(ctypes.sizeof(w.POINT), 8)
        self.assertEqual(ctypes.sizeof(w.RECT), 16)
        self.assertEqual(ctypes.sizeof(w.INPUT), 40)

    def test_password_and_unknown_security_never_read_value(self):
        uia = object.__new__(w.UIA)
        for secure in (True, None):
            element = Properties({30019: secure, 30043: True, 30045: 'private'})
            self.assertIsNone(uia.value(element))
            self.assertNotIn(30045, element.reads)
            self.assertEqual(uia.actions(element), [])

    def test_empty_value_is_distinct_from_missing_provider_value(self):
        uia = object.__new__(w.UIA)
        self.assertEqual(uia.value(Properties({30019: False, 30043: True, 30045: ''})), '')
        self.assertIsNone(uia.value(Properties({30019: False, 30043: False, 30045: ''})))

    def _combo_tree(self, value='', children=None):
        root = Properties({30003: 50003, 30002: 123, 30019: False, 30043: True, 30045: value})
        tree = {root: children or []}
        uia = object.__new__(w.UIA)
        uia.children = lambda item: iter(tree.get(item, []))
        return uia, root, tree

    def _combo_child(self, role, **values):
        return Properties({30003: role, 30002: 123, 30019: False,
                           **{int(key): value for key, value in values.items()}})

    def test_combo_blank_parent_and_option_names_are_unobserved(self):
        option = self._combo_child(50007, **{'30005': 'N', '30036': True, '30079': False})
        button = self._combo_child(50000, **{'30005': '[Editor] [valuelist] ValueListItem N'})
        uia, combo, _ = self._combo_tree(children=[option, button])
        self.assertEqual(uia.value_observation(combo), {
            'value': None, 'value_evidence': {'status': 'unavailable', 'source': 'none'}})

    def test_combo_blank_parent_uses_explicitly_selected_item(self):
        options = [self._combo_child(50007, **{'30005': value, '30036': True, '30079': value == 'N'})
                   for value in ('W', 'N', 'P')]
        uia, combo, _ = self._combo_tree(children=options)
        self.assertEqual(uia.value_observation(combo), {
            'value': 'N', 'value_evidence': {'status': 'observed', 'source': 'selection'}})

    def test_combo_blank_parent_uses_related_editor_but_not_editor_name(self):
        edit = self._combo_child(50004, **{'30005': '[Editor] Edit Area', '30043': True, '30045': 'Hybrid'})
        group = self._combo_child(50026)
        uia, combo, tree = self._combo_tree(children=[group])
        tree[group] = [edit]
        self.assertEqual(uia.value_observation(combo), {
            'value': 'Hybrid', 'value_evidence': {'status': 'observed', 'source': 'editor'}})
        edit.values[30045] = ''
        self.assertIsNone(uia.value(combo))

    def test_combo_nonempty_parent_remains_readable_without_child_evidence(self):
        option = self._combo_child(50007, **{'30005': 'Other', '30036': True, '30079': False})
        uia, combo, _ = self._combo_tree('N', [option])
        self.assertEqual(uia.value_observation(combo), {
            'value': 'N', 'value_evidence': {'status': 'observed', 'source': 'parent'}})

    def test_combo_conflicting_parent_editor_or_selection_is_unknown(self):
        edit = self._combo_child(50004, **{'30043': True, '30045': 'N'})
        selected = self._combo_child(50007, **{'30005': 'N', '30036': True, '30079': True})
        uia, combo, _ = self._combo_tree('N', [edit, selected])
        self.assertEqual(uia.value_observation(combo)['value_evidence']['source'], 'corroborated')
        for changed in (combo, edit, selected):
            key = 30005 if changed is selected else 30045
            changed.values[key] = 'W'
            self.assertEqual(uia.value_observation(combo), {
                'value': None, 'value_evidence': {'status': 'conflict', 'source': 'none'}})
            changed.values[key] = 'N'

    def test_combo_multiple_editors_or_selected_items_are_ambiguous_even_with_same_text(self):
        for role, values in ((50004, {'30043': True, '30045': 'N'}),
                             (50007, {'30005': 'N', '30036': True, '30079': True})):
            uia, combo, _ = self._combo_tree(children=[self._combo_child(role, **values),
                                                     self._combo_child(role, **values)])
            self.assertEqual(uia.value_observation(combo)['value_evidence']['status'], 'conflict')

    def test_combo_blank_editor_and_nonempty_editor_are_not_a_unique_editor(self):
        blank = self._combo_child(50004, **{'30043': True, '30045': ''})
        nonempty = self._combo_child(50004, **{'30043': True, '30045': 'N'})
        uia, combo, _ = self._combo_tree(children=[blank, nonempty])
        self.assertEqual(uia.value_observation(combo), {
            'value': None, 'value_evidence': {'status': 'conflict', 'source': 'none'}})

    def test_combo_unreadable_editor_and_nonempty_editor_are_not_a_unique_editor(self):
        for available in (True, False, None):
            unreadable = self._combo_child(50004, **{'30043': available, '30045': None})
            nonempty = self._combo_child(50004, **{'30043': True, '30045': 'N'})
            uia, combo, _ = self._combo_tree(children=[unreadable, nonempty])
            self.assertEqual(uia.value_observation(combo), {
                'value': None, 'value_evidence': {'status': 'conflict', 'source': 'none'}})

    def test_combo_editor_ambiguity_is_not_hidden_by_agreeing_parent_or_selection(self):
        blank = self._combo_child(50004, **{'30043': True, '30045': ''})
        nonempty = self._combo_child(50004, **{'30043': True, '30045': 'N'})
        selected = self._combo_child(50007, **{'30005': 'N', '30036': True, '30079': True})
        uia, combo, _ = self._combo_tree('N', [blank, nonempty, selected])
        self.assertEqual(uia.value_observation(combo)['value_evidence']['status'], 'conflict')

    def test_combo_does_not_read_protected_or_foreign_editor(self):
        for override in ({30019: True}, {30019: None}, {30002: 456}):
            edit = self._combo_child(50004, **{'30043': True, '30045': 'private'})
            edit.values.update(override)
            uia, combo, _ = self._combo_tree(children=[edit])
            self.assertIsNone(uia.value(combo))
            self.assertNotIn(30045, edit.reads)

    def test_combo_does_not_borrow_values_from_nested_combo_or_unselected_option(self):
        for role in (50003, 50007):
            unrelated = self._combo_child(role, **{'30043': True, '30045': 'Other', '30079': False})
            edit = self._combo_child(50004, **{'30043': True, '30045': 'Other', '30046': True})
            uia, combo, tree = self._combo_tree(children=[unrelated])
            tree[unrelated] = [edit]
            self.assertIsNone(uia.value(combo))
            self.assertNotIn(30045, edit.reads)

    def test_combo_partial_tree_does_not_claim_unique_value(self):
        edit = self._combo_child(50004, **{'30043': True, '30045': 'N'})
        uia, combo, _ = self._combo_tree(children=[edit] + [self._combo_child(50026) for _ in range(256)])
        self.assertIsNone(uia.value(combo))

    def test_regular_editor_clear_retains_explicit_empty_value(self):
        uia = object.__new__(w.UIA)
        self.assertEqual(uia.value(self._combo_child(50004, **{'30043': True, '30045': ''})), '')

    def test_toggle_unknown_is_not_unchecked(self):
        uia = object.__new__(w.UIA)
        self.assertIsNone(uia.checked(Properties({30041: True, 30086: None})))
        self.assertIsNone(uia.checked(Properties({30041: True, 30086: 2})))
        self.assertIs(uia.checked(Properties({30041: True, 30086: 0})), False)
        self.assertIs(uia.checked(Properties({30041: True, 30086: 1})), True)

    def test_numeric_runtime_ids_are_not_persistent_locators(self):
        uia = object.__new__(w.UIA)
        item = uia.basic(Properties({30011: '76532', 30005: 'Stable label', 30003: 50026}))
        self.assertNotIn('automation_id', item)
        self.assertEqual(item, {'name': 'Stable label', 'role': 'group'})

    def test_locator_uses_ordered_ancestor_context_and_unique_match(self):
        actual = {'name': 'Status', 'role': 'combobox', 'ancestors': [{'name': 'Filters'}, {'name': 'Business view'}]}
        self.assertTrue(w.match_locator(actual, {'name': 'Status', 'ancestors': [{'name': 'Business view'}]}))
        self.assertFalse(w.match_locator(actual, {'name': 'Status', 'ancestors': [{'name': 'Business view'}, {'name': 'Filters'}]}))
        self.assertFalse(w.match_locator(actual, {'role': 'combobox'}))
        with self.assertRaisesRegex(w.BridgeError, 'ambiguous_target'):
            w.unique([{'locator': actual}, {'locator': actual}], {'name': 'Status'})

    def test_error_diagnostic_cannot_echo_arbitrary_provider_content(self):
        error = w.BridgeError('private provider message at C:/secret', dispatched=None)
        self.assertEqual(error.code, 'bridge_operation_failed')
        self.assertIsNone(error.input_dispatched)

    def test_binding_rejects_different_session_before_process_read(self):
        binding = object.__new__(w.Binding)
        binding.pid, binding.hwnd, binding.exe = 123, 900, 'Fixture.exe'
        kernel, user = MagicMock(), MagicMock()
        def session(pid, pointer):
            pointer._obj.value = 99 if pid == 123 else 1
            return True
        kernel.ProcessIdToSessionId.side_effect = session
        user.IsWindow.return_value = True
        with patch.object(w, 'kernel32', kernel), patch.object(w, 'user32', user), \
             patch.object(w, 'hwnd_pid', return_value=123), patch.object(w, '_process_identity') as identity:
            with self.assertRaisesRegex(w.BridgeError, 'different_session'):
                binding._identity()
            identity.assert_not_called()

    def test_binding_lifetime_change_invalidates_existing_scope(self):
        binding = object.__new__(w.Binding)
        binding.created = '638000000000000001'
        binding._identity = MagicMock(return_value='638000000000000002')
        with self.assertRaisesRegex(w.BridgeError, 'target_lifetime_changed'):
            binding.check()

    def test_binding_does_not_accept_same_pid_unowned_sibling(self):
        binding = object.__new__(w.Binding)
        binding.pid, binding.hwnd = 123, 900
        user = MagicMock()
        user.IsChild.return_value = False
        with patch.object(w, 'user32', user), patch.object(w, 'hwnd_pid', return_value=123), \
             patch.object(w, 'root_window', return_value=901), patch.object(w, 'owned', return_value=False):
            self.assertFalse(binding.contains_window(901))

    def test_detached_combo_candidate_needs_worker_proof_and_not_sibling_scope(self):
        binding = object.__new__(w.Binding)
        binding.pid, binding.hwnd = 123, 900
        binding.contains_window = MagicMock(return_value=False)
        with patch.object(w, 'hwnd_pid', return_value=123), patch.object(w, 'root_window', return_value=901), \
             patch.object(w, 'window_class', return_value='ComboLBox'):
            self.assertTrue(binding.recording_candidate(901))
        with patch.object(w, 'hwnd_pid', return_value=123), patch.object(w, 'root_window', return_value=901), \
             patch.object(w, 'window_class', return_value='IndependentBusinessWindow'):
            self.assertFalse(binding.recording_candidate(901))

    def test_detached_popup_observation_requires_uia_ancestry_of_selected_root(self):
        binding = MagicMock(pid=123, hwnd=900)
        binding.contains_window.return_value = False
        uia = object.__new__(w.UIA)
        option = Properties({30003: 50007})
        uia.from_handle = MagicMock(return_value='main-root')
        uia.from_point = MagicMock(return_value=option)
        uia.pid = MagicMock(return_value=123)
        uia.within = MagicMock(return_value=True)
        uia.point_descendant = MagicMock(return_value=option)
        uia.combo_observation = MagicMock(return_value=None)
        uia.node = MagicMock(return_value={'name': 'N'})
        with patch.object(w, 'window_from_point', return_value=901), patch.object(w, 'hwnd_pid', return_value=123):
            result = uia.observe_point(binding, 10, 20, 901)
            self.assertEqual(result['window'], 900)
            self.assertEqual(result['hit_window'], 901)
            uia.within.assert_called_with(option, 'main-root')
            uia.within.return_value = False
            with self.assertRaisesRegex(w.BridgeError, 'point_identity_unproven'):
                uia.observe_point(binding, 10, 20, 901)

    def test_empty_submenu_peer_uses_unique_item_under_owned_parent(self):
        root, context, empty = [Properties({30003: 50032}) for _ in range(3)]
        item = Properties({30003: 50011, 30022: False})
        uia = object.__new__(w.UIA)
        binding = MagicMock(pid=123, hwnd=10)
        binding.scoped_windows.return_value = [10, 20, 30]
        uia.from_handle = lambda hwnd: {10: root, 20: context, 30: empty}[hwnd]
        uia.pid = lambda element: 123
        tree = {root: [], context: [item], empty: [], item: []}
        uia.children = lambda element: iter(tree[element])
        uia.same = lambda first, second: first is second
        uia.bounds = lambda element: {'x': 100, 'y': 100, 'width': 100, 'height': 30}
        uia.node = lambda element, parent: {'name': 'Export to Excel', 'locator': {'name': 'Export to Excel'}}
        with patch.object(w, 'root_window', return_value=30), patch.object(w, 'window_from_point', return_value=30):
            found = uia.menu_observation(binding, 150, 110, 30)
            self.assertIs(found['element'], item)
            self.assertIs(found['root'], context)
            self.assertEqual(found['window'], 20)
            other = Properties({30003: 50011, 30022: False})
            tree[root] = [other]; tree[other] = []
            self.assertIsNone(uia.menu_observation(binding, 150, 110, 30))

    def _point_context(self, *, role='group', path='0/1', complete=True, secure=False, partial=False):
        binding = MagicMock(pid=123, hwnd=900)
        element = Properties({30019: False, 30010: True, 30022: False})
        uia = MagicMock()
        uia.__enter__.return_value = uia
        uia.bounds.return_value = {'x': 10, 'y': 20, 'width': 200, 'height': 100}
        uia.secure.side_effect = [False, secure]
        uia.from_point.return_value = element
        uia.pid.return_value = 123
        uia.within.return_value = True
        user = MagicMock()
        user.IsWindowVisible.return_value = True
        user.IsIconic.return_value = False
        user.GetAsyncKeyState.return_value = 0
        user.GetSystemMetrics.side_effect = [0, 0, 1920, 1080]
        user.SendInput.return_value = 2 if partial else 3
        node = {'role': role, 'path': path, 'locator': {'name': 'Result region'}}
        patches = [patch.object(w, 'Binding', return_value=binding), patch.object(w, 'UIA', return_value=uia),
                   patch.object(w, 'read_tree', return_value=([node], [element], complete)),
                   patch.object(w, 'user32', user), patch.object(w, 'foreground', return_value=900),
                   patch.object(w, 'root_window', side_effect=lambda hwnd: hwnd),
                   patch.object(w, 'window_from_point', return_value=900),
                   patch.object(w, 'hwnd_pid', return_value=123)]
        return patches, user

    def _request(self):
        return {'pid': 123, 'hwnd': 900, 'exe_name': 'Fixture.exe', 'process_started_utc_ticks': '123',
                'action': 'right-click', 'locator': {'name': 'Result region'}, 'point': {'x_ratio': .4, 'y_ratio': .4}}

    def _run_point(self, patches, request=None):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return w.point(request or self._request())

    def test_incomplete_or_window_root_never_dispatches(self):
        for changes, code in (({'complete': False}, 'incomplete_tree'),
                              ({'role': 'window'}, 'window_root_point_rejected'),
                              ({'role': 'group', 'path': '0'}, 'window_root_point_rejected')):
            patches, user = self._point_context(**changes)
            with self.assertRaisesRegex(w.BridgeError, code):
                self._run_point(patches)
            user.SendInput.assert_not_called()

    def test_protected_live_hit_rejects_even_when_container_safe(self):
        patches, user = self._point_context(secure=True)
        with self.assertRaisesRegex(w.BridgeError, 'protected_point_target'):
            self._run_point(patches)
        user.SendInput.assert_not_called()

    def test_point_sends_one_batch_and_partial_is_not_retried(self):
        patches, user = self._point_context(partial=True)
        result = self._run_point(patches)
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(result['retry'], 'unsafe')
        user.SendInput.assert_called_once()
        args = user.SendInput.call_args.args
        self.assertEqual(args[0], 3)
        self.assertEqual(args[1][0].mi.dwFlags, 0xC001)
        self.assertEqual(args[1][1].mi.dwFlags, 0x0008)
        self.assertEqual(args[1][2].mi.dwFlags, 0x0010)

    def test_nonfinite_ratio_and_edge_target_never_dispatch(self):
        for ratio in (float('nan'), float('inf'), 0, 1):
            patches, user = self._point_context()
            request = self._request()
            request['point']['x_ratio'] = ratio
            with self.assertRaises(w.BridgeError):
                self._run_point(patches, request)
            user.SendInput.assert_not_called()

    def test_held_mouse_button_never_dispatches(self):
        patches, user = self._point_context()
        user.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key == 4 else 0
        with self.assertRaisesRegex(w.BridgeError, 'physical_input_busy'):
            self._run_point(patches)
        user.SendInput.assert_not_called()

    def test_dispatch_exception_has_unknown_outcome_and_no_retry(self):
        patches, user = self._point_context()
        user.SendInput.side_effect = OSError('provider failure')
        with self.assertRaisesRegex(w.BridgeError, 'point_delivery_unknown') as raised:
            self._run_point(patches)
        self.assertIsNone(raised.exception.input_dispatched)
        user.SendInput.assert_called_once()

    def test_com_context_rejects_other_thread_before_vtable_access(self):
        import threading
        uia = object.__new__(w.UIA)
        uia.owner, uia.closed = threading.get_ident() + 1, False
        with self.assertRaisesRegex(w.BridgeError, 'accessibility_wrong_thread'):
            uia._thread()


if __name__ == '__main__':
    unittest.main()
