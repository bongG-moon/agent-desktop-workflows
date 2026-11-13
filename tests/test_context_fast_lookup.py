"""Exact context identities avoid full snapshots without widening target scope."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import context_menu as menu
import result_menu as result
import learned_tasks as tasks


class FastContextTests(unittest.TestCase):
    def setUp(self):
        self.binding = Mock(hwnd=41, pid=7)
        self.binding.scoped_windows.return_value = [41]
        self.root, self.element = Mock(), Mock()
        self.root.property.side_effect = lambda key: {30022: False}.get(key)
        self.flags = {30022: None, 30010: True}
        self.element.property.side_effect = lambda key: self.flags.get(key)
        self.uia = MagicMock()
        self.uia.__enter__.return_value = self.uia
        self.uia.from_handle.return_value = self.root
        self.uia.pid.return_value = 7
        self.uia.secure.return_value = False
        self.uia.same.side_effect = lambda a, b: a is b
        self.uia.within.return_value = True
        self.uia.bounds.side_effect = lambda e: (
            dict(x=10, y=20, width=500, height=400) if e is self.root
            else dict(x=30, y=40, width=100, height=80))
        self.target = dict(role='table', automation_id='Results')

    def test_owned_geometry_allows_unknown_visibility_without_whole_snapshot(self):
        with patch.object(menu.lookup, 'resolve', return_value=self.element) as resolve, \
             patch.object(menu.visibility, '_shown', return_value=True), \
             patch.object(app, 'observe', side_effect=AssertionError('whole snapshot')):
            self.assertIs(menu._region(self.binding, self.uia, self.target), self.element)
            resolve.assert_called_once_with(self.binding, self.uia, self.target)

    def test_unknown_visibility_does_not_override_hidden_or_outside_window(self):
        for shown, contained, state in ((False, True, None), (True, False, None), (True, True, True)):
            with self.subTest(shown=shown, contained=contained, state=state), \
                 patch.object(menu.lookup, 'resolve', return_value=self.element), \
                 patch.object(menu.visibility, '_shown', return_value=shown):
                self.flags[30022] = state
                self.uia.within.return_value = contained
                with self.assertRaisesRegex(menu.win.BridgeError, 'context_region_unavailable'):
                    menu._region(self.binding, self.uia, self.target)

    def test_missing_ambiguous_or_changed_native_identity_stops(self):
        for native, expected in (('target_missing', 'context_region_missing'),
                                 ('target_ambiguous', 'context_region_ambiguous'),
                                 ('target_lifetime_changed', 'target_lifetime_changed')):
            with self.subTest(native=native), patch.object(menu.lookup, 'resolve',
                    side_effect=menu.win.BridgeError(native)), \
                 self.assertRaisesRegex(menu.win.BridgeError, expected):
                menu._region(self.binding, self.uia, self.target)

    def test_result_text_uses_native_identity_and_same_visibility_contract(self):
        locator = dict(role='statictext', automation_id='State')
        with patch.object(result.lookup, 'resolve', return_value=self.element), \
             patch.object(result.visibility, '_shown', return_value=True), \
             patch.object(app, 'observe', side_effect=AssertionError('whole snapshot')):
            self.assertEqual(result._resolve(self.binding, self.uia, locator), (self.element, []))

    def test_missing_result_still_detects_protected_subtree(self):
        with patch.object(result.lookup, 'resolve', side_effect=menu.win.BridgeError('target_missing')), \
             patch.object(app, 'observe', return_value=([dict(secure=True)], [self.root])), \
             self.assertRaisesRegex(tasks.TaskError, 'result_indicator_cannot_be_resolved_safely'):
            result._resolve(self.binding, self.uia, dict(role='statictext', automation_id='State'))

    def test_visual_resolution_retains_unrelated_protected_overlays(self):
        secret = Mock()
        nodes = [dict(secure=False, role='window'), dict(secure=False, **self.target), dict(secure=True)]
        with patch.object(result.lookup, 'resolve', side_effect=AssertionError('target-only visual read')), \
             patch.object(result.visibility, '_shown', return_value=True), \
             patch.object(app, 'observe', return_value=(nodes, [self.root, self.element, secret])):
            current, protected = result._resolve(self.binding, self.uia, self.target, include_protected=True)
            self.assertIs(current, self.element)
            self.assertEqual(protected, [(nodes[2], secret)])

    def test_query_revalidates_identity_without_window_snapshot(self):
        locator = dict(role='button', automation_id='Query')
        receipt = dict(status='delivered_unverified', input_dispatched=True)
        def execute(*args, still_valid, **kwargs):
            still_valid()
            return receipt
        with patch.object(result.bridge, 'UIA', return_value=self.uia), \
             patch.object(result.visibility, 'restore_for_action'), patch.object(app, 'guard') as guard, \
             patch.object(result.lookup, 'resolve', return_value=self.element) as resolve, \
             patch.object(app, 'observe', side_effect=AssertionError('whole snapshot')), \
             patch('live_action.execute', side_effect=execute) as invoke:
            self.assertEqual(result._query(self.binding, locator, 'App'), receipt)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(guard.call_count, 2)
            invoke.assert_called_once()

    def test_menu_unknown_visibility_requires_geometry_and_explicit_true_stays_hidden(self):
        with patch.object(menu.visibility, 'visible', return_value=False) as visible:
            self.assertFalse(menu._menu_visible(self.binding, self.uia, self.element))
            visible.assert_called_once()
            self.flags[30022] = True
            visible.reset_mock()
            self.assertFalse(menu._menu_visible(self.binding, self.uia, self.element))
            visible.assert_not_called()


if __name__ == '__main__':
    unittest.main()
