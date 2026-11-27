"""Windows placement contracts for coordinate-based replay."""
from contextlib import nullcontext
from copy import deepcopy
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import window_placement as placement


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.binding = Mock(hwnd=70)
        self.guard = Mock()
        # The top taskbar consumes 40px; the recorded monitor is left of primary.
        self.spec = dict(rect=[-1200, 220, 800, 450], normal_rect=[-1200, 180, -400, 630],
                         monitor='display-left', dpi=144, maximized=False)
        self.user = self.start(patch.object(placement, '_user')).return_value
        self.user.MonitorFromWindow.return_value = 11
        self.user.MonitorFromRect.return_value = 11
        self.device = self.start(patch.object(placement, '_device', side_effect={11: 'display-left', 12: 'main'}.get))
        self.lease = self.start(patch.object(placement.coordination, 'desktop_input', return_value=nullcontext()))
        for name in ('input_tick', 'foreground', 'wait_for_quiet_foreground', 'check_foreground_input'):
            self.start(patch.object(app.bridge, name))

    def start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_validation_and_capture_use_separate_screen_and_workspace_coordinates(self):
        def read_rect(hwnd, rect):
            r = C.cast(rect, C.POINTER(W.RECT)).contents
            r.left, r.top, r.right, r.bottom = -1200, 220, -400, 670
            return True

        def read_placement(hwnd, ptr):
            p = C.cast(ptr, C.POINTER(placement._Placement)).contents
            p.showCmd, p.rcNormalPosition = 1, W.RECT(*self.spec['normal_rect'])
            return True

        self.user.IsIconic.return_value = False
        self.user.GetDpiForWindow.return_value = 144
        self.user.GetWindowRect.side_effect = read_rect
        self.user.GetWindowPlacement.side_effect = read_placement
        self.assertEqual(self.spec, placement.capture(70, expected_rect=tuple(self.spec['rect'])))
        self.assertIsNone(placement.capture(70, expected_rect=[0, 0, 800, 450]))
        self.user.IsIconic.return_value = True
        self.assertIsNone(placement.capture(70))
        for invalid in (dict(dpi=True), dict(monitor=''), dict(rect=[0, 0, 800, 0]),
                        dict(normal_rect=[0, 0, -1, 200]), dict(maximized=1), dict(extra=True)):
            self.assertFalse(placement.valid({**self.spec, **invalid}))

    def test_matching_geometry_does_not_acquire_input_or_change_window(self):
        self.start(patch.object(placement, 'capture', return_value=self.spec))
        self.assertFalse(placement.restore(self.binding, self.spec, self.guard))
        self.lease.assert_not_called()
        self.user.SetWindowPlacement.assert_not_called()

    def test_restore_uses_raw_windows_placement_once_then_checks_actual_screen_rect(self):
        states = [dict(self.spec, rect=[0, 0, 400, 300])] * 2 + [self.spec]
        self.start(patch.object(placement, 'capture', side_effect=states))
        applied = []

        def apply(hwnd, ptr):
            p = C.cast(ptr, C.POINTER(placement._Placement)).contents
            r = p.rcNormalPosition
            applied.append((hwnd, p.showCmd, [r.left, r.top, r.right, r.bottom]))
            return True

        self.user.SetWindowPlacement.side_effect = apply
        self.assertTrue(placement.restore(self.binding, self.spec, self.guard))
        self.assertEqual([(70, 4, self.spec['normal_rect'])], applied)

    def test_other_monitor_is_restored_before_maximize(self):
        spec = dict(self.spec, maximized=True, rect=[-1928, -8, 1936, 1056])
        other = dict(spec, monitor='main', dpi=96, rect=[-8, -8, 1936, 1056])
        self.user.MonitorFromWindow.return_value = 12
        self.start(patch.object(placement, 'capture', side_effect=[other, other, self.spec, spec]))
        apply = self.start(patch.object(placement, '_set', return_value=True))
        self.assertTrue(placement.restore(self.binding, spec, self.guard))
        self.assertEqual([False, True], [call.kwargs['maximize'] for call in apply.call_args_list])

    def test_monitor_or_dpi_change_blocks_before_mutation(self):
        self.start(patch.object(placement, 'capture', return_value=dict(self.spec, dpi=96)))
        with self.assertRaisesRegex(app.bridge.BridgeError, 'recorded_window_placement_unavailable'):
            placement.restore(self.binding, self.spec, self.guard)
        self.user.MonitorFromRect.return_value = 12
        with self.assertRaisesRegex(app.bridge.BridgeError, 'recorded_window_placement_unavailable'):
            placement.restore(self.binding, self.spec, self.guard)
        self.user.SetWindowPlacement.assert_not_called()

    def test_unsupported_size_and_user_activity_stop_without_retry(self):
        wrong = deepcopy(self.spec)
        wrong['rect'][2] -= 1
        self.start(patch.object(placement, 'capture', return_value=wrong))
        self.start(patch.object(placement.time, 'monotonic', side_effect=[0, 1]))
        with self.assertRaisesRegex(app.bridge.BridgeError, 'recorded_window_placement_unconfirmed'):
            placement.restore(self.binding, self.spec, self.guard)
        self.user.SetWindowPlacement.assert_called_once()
        app.bridge.wait_for_quiet_foreground.side_effect = app.bridge.BridgeError('foreground_or_input_changed')
        with self.assertRaisesRegex(app.bridge.BridgeError, 'foreground_or_input_changed'):
            placement.restore(self.binding, self.spec, self.guard)
        self.user.SetWindowPlacement.assert_called_once()


if __name__ == '__main__':
    unittest.main()
