"""A field submission is one focused Enter pair, never an arbitrary key macro."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import live_action as live
import python_workflow as flow


class SubmitActionTests(unittest.TestCase):
    def setUp(self):
        self.binding = MagicMock(hwnd=100, pid=11)
        self.root = MagicMock()
        self.root.property.return_value = 50032
        self.props = {30003: 50004, 30009: True, 30010: True, 30043: True, 30046: False}
        self.element = MagicMock()
        self.element.property.side_effect = self.props.get
        self.uia = MagicMock()
        self.uia.same.side_effect = lambda left, right: left is right
        self.uia.from_handle.return_value = self.root
        self.uia.focused.return_value = self.element
        self.uia.secure.return_value = False
        self.uia.within.return_value = True
        self.check = MagicMock()
        self.events = []
        patches = {
            'live_action.control_lookup.ancestry': dict(side_effect=lambda u, el, root, **kw: [el, root]),
            'live_action._visible': dict(return_value=True),
            'live_action._combo_visible': dict(return_value=True),
            'live_action.win._combo_surface': dict(return_value=None),
            'live_action.win.foreground': dict(return_value=100),
            'live_action.win.root_window': dict(return_value=100),
            'live_action.win.user32.GetAsyncKeyState': dict(return_value=0),
            'live_action.win.user32.SendInput': dict(side_effect=self.send),
            'row_action.foreground': dict(return_value=None),
            'row_action._input_tick': dict(return_value=42),
        }
        for name, options in patches.items():
            p = patch(name, **options)
            p.start()
            self.addCleanup(p.stop)

    def send(self, count, events, size):
        self.events.extend((event.type, event.ki.wVk, event.ki.wScan, event.ki.dwFlags) for event in events)
        self.assertEqual(count, 2)
        return 2

    def run_submit(self, role='textfield'):
        return live.execute(self.binding, self.uia, self.element, {'role': role}, 'submit', still_valid=self.check)

    def test_sends_exactly_one_enter_pair_after_fresh_focus_checks(self):
        result = self.run_submit()
        self.assertEqual(result['status'], 'delivered_unverified')
        self.assertEqual(result['diagnostic'], 'submit_enter_sent')
        self.assertEqual(result['keyboard_events_sent'], 2)
        self.assertTrue(result['input_dispatched'])
        self.assertTrue(result['physical_input'])
        self.assertEqual(result['business_commit'], 'not_verified')
        self.assertEqual(self.events, [(1, 13, 0, 0), (1, 13, 0, 2)])
        self.element.call.assert_called_once_with(3, ())
        self.assertGreaterEqual(self.check.call_count, 3)
        live.win.user32.SendInput.assert_called_once()

    def test_unknown_or_partial_delivery_is_not_retried(self):
        for count in (0, 1):
            with self.subTest(count=count):
                live.win.user32.SendInput.reset_mock()
                live.win.user32.SendInput.side_effect = None
                live.win.user32.SendInput.return_value = count
                result = self.run_submit()
                self.assertEqual((result['status'], result['keyboard_events_sent']), ('uncertain', count))
                self.assertIsNone(result['input_dispatched'])
                live.win.user32.SendInput.assert_called_once()
        live.win.user32.SendInput.reset_mock()
        live.win.user32.SendInput.side_effect = OSError('unknown input delivery')
        result = self.run_submit()
        self.assertEqual(result['diagnostic'], 'submit_delivery_unknown')
        self.assertIsNone(result['input_dispatched'])
        live.win.user32.SendInput.assert_called_once()

    def test_protected_readonly_disabled_or_unfocusable_fields_never_receive_enter(self):
        for prop, value in ((30043, False), (30046, True), (30046, None), (30009, False), (30010, False), (30003, 50000)):
            old = self.props[prop]
            with self.subTest(prop=prop, value=value):
                self.props[prop] = value
                result = self.run_submit()
                self.assertEqual(result['status'], 'blocked')
                self.assertFalse(result['input_dispatched'])
                self.props[prop] = old
        self.uia.secure.return_value = True
        self.assertEqual(self.run_submit()['status'], 'blocked')
        self.element.call.assert_not_called()
        live.win.user32.SendInput.assert_not_called()

    def test_hidden_field_or_foreign_ancestry_does_not_receive_focus(self):
        live._visible.return_value = False
        self.assertEqual(self.run_submit()['diagnostic'], 'target_offscreen')
        live._visible.return_value = True
        live.control_lookup.ancestry.side_effect = live.win.BridgeError('target_owner_mismatch')
        self.assertEqual(self.run_submit()['diagnostic'], 'target_owner_mismatch')
        self.element.call.assert_not_called()
        live.win.user32.SendInput.assert_not_called()

    def test_focus_mismatch_or_lost_focus_does_not_submit_other_control(self):
        other = MagicMock()
        other.property.side_effect = self.props.get
        self.uia.focused.return_value = other
        self.assertEqual(self.run_submit()['diagnostic'], 'submit_focus_mismatch')
        self.uia.focused.side_effect = [self.element, other]
        self.assertEqual(self.run_submit()['diagnostic'], 'submit_focus_changed')
        live.win.user32.SendInput.assert_not_called()

    def test_focus_exception_never_falls_back_to_current_focus(self):
        self.element.call.side_effect = live.win.BridgeError('focus_unknown')
        result = self.run_submit()
        self.assertFalse(result['input_dispatched'])
        self.assertIsNone(result['focus_changed'])
        live.win.user32.SendInput.assert_not_called()

    def test_user_input_or_other_foreground_blocks_enter(self):
        live.win.user32.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key == 0x11 else 0
        self.assertEqual(self.run_submit()['diagnostic'], 'physical_input_busy')
        live.win.user32.GetAsyncKeyState.side_effect = None
        live.win.user32.GetAsyncKeyState.return_value = 0
        live.win.root_window.return_value = 200
        self.assertEqual(self.run_submit()['diagnostic'], 'target_not_foreground')
        live.win.root_window.return_value = 100
        with patch('row_action._input_tick', side_effect=[42, 43]):
            self.assertEqual(self.run_submit()['diagnostic'], 'physical_input_changed')
        live.win.user32.SendInput.assert_not_called()

    def test_combo_can_submit_only_its_own_safe_editor(self):
        self.props[30003] = 50003
        editor = MagicMock()
        editor.property.side_effect = {30003: 50004, 30010: True, 30043: True, 30046: False}.get
        self.uia.focused.return_value = editor
        live.control_lookup.ancestry.side_effect = lambda u, el, root, **kw: ([editor, self.element, root]
                                                                           if el is editor else [el, root])
        self.assertEqual(self.run_submit('combobox')['diagnostic'], 'submit_enter_sent')
        live.win.user32.SendInput.reset_mock()
        another_combo = MagicMock()
        another_combo.property.return_value = 50003
        live.control_lookup.ancestry.side_effect = lambda u, el, root, **kw: ([editor, another_combo, root]
                                                                           if el is editor else [el, root])
        self.assertEqual(self.run_submit('combobox')['diagnostic'], 'submit_focus_mismatch')
        live.win.user32.SendInput.assert_not_called()

    def test_readonly_readiness_check_never_focuses_or_sends_keys(self):
        self.assertIs(live.submit_ready(self.binding, self.uia, self.element, {'role': 'textfield'}), self.root)
        self.element.call.assert_not_called()
        self.uia.focused.assert_not_called()
        live.win.user32.SendInput.assert_not_called()

    def test_arbitrary_key_or_value_options_are_not_supported(self):
        base = dict(schema=1, name='Submit test', exe='Example.exe', window='Example window', steps=[
            dict(action='submit', selector=dict(name='Filter', role='textfield'))])
        self.assertEqual(flow.validate(base), base)
        for field in ('key', 'value', 'parameter'):
            data = dict(base, steps=[dict(base['steps'][0], **{field: 'Delete'})])
            with self.subTest(field=field), self.assertRaises(ValueError):
                flow.validate(data)
        result = live.execute(self.binding, self.uia, self.element, {'role': 'textfield'},
                              'submit', 'Delete', still_valid=self.check)
        self.assertEqual(result['diagnostic'], 'submit_value_not_allowed')
        live.win.user32.SendInput.assert_not_called()


if __name__ == '__main__':
    unittest.main()
