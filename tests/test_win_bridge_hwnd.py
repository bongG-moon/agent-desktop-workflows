"""High-bit HWND identity at the Win32, UIA and saved-request boundaries."""
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import win_bridge as w


SIGNED = -1158013160
UNSIGNED = 3136954136
POINTER = 18446744072551538456
PID = 112
CREATED = '638000000000000001'


class WindowHandleTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.user = stack.enter_context(patch.object(w, 'user32'))
        stack.enter_context(patch.object(w, 'kernel32'))
        stack.enter_context(patch.object(w, '_windows'))
        self.process = stack.enter_context(patch.object(w, '_process_identity', return_value=('Example.exe', CREATED)))
        self.user.IsWindow.side_effect = lambda hwnd: hwnd == UNSIGNED
        self.user.GetWindowThreadProcessId.side_effect = self.get_pid
        self.user.IsChild.return_value = False

    @staticmethod
    def get_pid(hwnd, pid):
        pid._obj.value = PID
        return 42

    def binding(self, hwnd=POINTER, **overrides):
        return w.Binding(dict({'hwnd': hwnd, 'pid': PID, 'exe_name': 'Example.exe',
                               'process_started_utc_ticks': CREATED}, **overrides))

    def test_all_three_representations_bind_and_revalidate_the_same_window(self):
        self.user.GetAncestor.return_value = POINTER
        self.user.GetWindow.return_value = POINTER + 2
        self.user.GetForegroundWindow.return_value = POINTER
        self.user.WindowFromPoint.return_value = POINTER
        for hwnd in (POINTER, SIGNED, UNSIGNED):
            with self.subTest(hwnd=hwnd):
                binding = self.binding(hwnd)
                binding.check()
                self.assertEqual(binding.hwnd, UNSIGNED)
                self.assertEqual(w.root_window(hwnd), UNSIGNED)
                self.assertEqual(w.foreground(), UNSIGNED)
                self.assertEqual(w.window_from_point(12, 23), UNSIGNED)
                self.assertTrue(binding.contains_window(hwnd))
                self.assertEqual(binding.info()['owner_hwnd'], UNSIGNED + 2)
                self.assertEqual(binding.info()['root_owner_hwnd'], UNSIGNED)
        self.user.IsWindow.assert_called_with(UNSIGNED)
        self.user.GetWindow.assert_called_with(UNSIGNED, 4)

    def test_owner_chain_uses_the_same_identity_and_never_owns_the_null_window(self):
        self.user.GetWindow.side_effect = [POINTER + 2, POINTER]
        self.assertTrue(w.owned(301, SIGNED))
        self.assertEqual(self.user.GetWindow.call_args_list, [call(301, 4), call(UNSIGNED + 2, 4)])
        self.user.GetWindow.reset_mock()
        self.assertFalse(w.owned(301, 0))
        self.user.GetWindow.assert_not_called()

    def test_malformed_values_do_not_alias_a_target_or_call_windows(self):
        invalid = (None, False, True, '3136954136', float(UNSIGNED), 0, 1, 0xffff,
                   0x100000000 | UNSIGNED, 0xffffffff00000017, -0x80000001, 1 << 64)
        for sentinel in (-1, -2, -3):
            invalid += (sentinel, sentinel & 0xffffffff, sentinel & 0xffffffffffffffff)
        for hwnd in invalid:
            with self.subTest(hwnd=hwnd):
                with self.assertRaisesRegex(w.BridgeError, 'invalid_target'):
                    self.binding(hwnd)
                self.assertEqual(w.hwnd_pid(hwnd), 0)
                self.assertEqual(w.root_window(hwnd), 0)
                self.assertEqual(w.owner_window(hwnd), 0)
        self.assertFalse(self.user.mock_calls)

    def test_shape_alone_does_not_grant_identity_or_scope(self):
        for change, code in (({'hwnd': POINTER + 2}, 'target_window_mismatch'),
                             ({'pid': PID + 1}, 'target_window_mismatch'),
                             ({'exe_name': 'Other.exe'}, 'target_executable_mismatch'),
                             ({'process_started_utc_ticks': 'old'}, 'target_lifetime_changed')):
            with self.subTest(change=change), self.assertRaisesRegex(w.BridgeError, code):
                self.binding(**change)
        binding = self.binding()
        self.user.GetAncestor.return_value = POINTER + 2
        self.user.GetWindow.return_value = None
        self.assertFalse(binding.contains_window(POINTER + 2))
        self.process.return_value = ('Example.exe', 'replacement')
        with self.assertRaisesRegex(w.BridgeError, 'target_lifetime_changed'):
            binding.check()

    def test_enumeration_and_uia_do_not_reintroduce_wide_or_signed_keys(self):
        binding = self.binding()
        self.user.GetAncestor.return_value = POINTER
        self.user.GetWindow.return_value = None
        self.user.EnumWindows.side_effect = lambda callback, context: callback(POINTER, context)
        self.assertEqual(binding.scoped_windows(), [UNSIGNED])
        self.assertEqual([row['hwnd'] for row in w.inventory()['data']], [UNSIGNED])
        uia = object.__new__(w.UIA)
        uia._element = MagicMock()
        uia.from_handle(SIGNED)
        uia._element.assert_called_once_with(6, (w.W.HWND,), UNSIGNED)
        self.assertIsNone(uia.from_handle(0x100000000 | UNSIGNED))
        self.assertEqual(uia._element.call_count, 1)


if __name__ == '__main__':
    unittest.main()
