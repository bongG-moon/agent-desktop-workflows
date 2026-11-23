"""A clipped result review keeps raw identity and authenticates visible frame pixels."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from test_control_lookup import Binding, FakeUIA, Node, win
import grounding
import python_workflow as flow
import result_cell
import result_trial as trial
import row_vision


class VisibleTableTests(unittest.TestCase):
    def setUp(self):
        self.rect = [100, 100, 8, 8]
        self.box = dict(x=102, y=102, width=12, height=4)
        self.pixels = bytes((31, 61, 91, 0)) * 64
        self.png = grounding._png(8, 8, self.pixels)

    def changed(self, x, y):
        pixels = bytearray(self.pixels)
        pixels[(y*8+x)*4] ^= 255
        return grounding._png(8, 8, pixels)

    def test_reported_oversized_table_clips_only_the_right_edge_without_scaling(self):
        raw = dict(x=10, y=197, width=1393, height=562)
        self.assertEqual(trial.visible_table_box([0, 0, 1296, 839], raw),
                         dict(x=10, y=197, width=1286, height=562))
        self.assertEqual(raw, dict(x=10, y=197, width=1393, height=562))

    def test_left_top_and_multi_monitor_origins_preserve_screen_coordinates(self):
        raw = dict(x=-220, y=90, width=120, height=90)
        self.assertEqual(trial.visible_table_box([-200, 100, 80, 60], raw),
                         dict(x=-200, y=100, width=80, height=60))

    def test_invalid_and_empty_geometry_do_not_produce_a_review_region(self):
        for bad in (dict(self.box, width=0), dict(self.box, x=float('nan')),
                    dict(self.box, x=True), dict(self.box, height=1.5), {}):
            with self.subTest(box=bad), self.assertRaisesRegex(ValueError, 'result_geometry_invalid'):
                trial.visible_table_box(self.rect, bad)
        for bad in (dict(self.box, x=108), dict(self.box, y=108),
                    dict(x=80, y=80, width=20, height=20)):
            with self.subTest(box=bad), self.assertRaisesRegex(ValueError, 'result_table_outside_frame'):
                trial.visible_table_box(self.rect, bad)

    def test_equal_png_bytes_still_require_exact_capture_dimensions(self):
        self.assertTrue(trial.same_table_pixels(self.png, self.png, self.rect, self.box))
        self.assertFalse(trial.same_table_pixels(self.png, self.png, [100, 100, 9, 8], self.box))
        self.assertFalse(trial.same_table_pixels(self.png, self.png, [100, 100, 8, 9], self.box))
        self.assertFalse(trial.same_table_pixels(b'not an image', b'not an image', self.rect, self.box))

    def test_every_visible_table_pixel_is_checked_but_unrelated_window_pixels_can_change(self):
        self.assertTrue(trial.same_table_pixels(self.png, self.changed(1, 1), self.rect, self.box))
        for x, y in ((2, 2), (7, 3), (7, 5)):
            with self.subTest(point=(x, y)):
                self.assertFalse(trial.same_table_pixels(self.png, self.changed(x, y), self.rect, self.box))

    def test_negative_relative_origin_uses_correct_png_offsets(self):
        raw = dict(x=98, y=98, width=6, height=6)
        self.assertFalse(trial.same_table_pixels(self.png, self.changed(0, 0), self.rect, raw))
        self.assertFalse(trial.same_table_pixels(self.png, self.changed(3, 3), self.rect, raw))
        self.assertTrue(trial.same_table_pixels(self.png, self.changed(4, 4), self.rect, raw))


class ReviewedPointTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.uia = Binding(), FakeUIA()
        for name, replacement in (
                ('_Com', lambda pointer, uia: uia.handles[pointer.value]),
                ('Element', lambda pointer, uia: uia.nodes[pointer.value]),
                ('_sig', self.uia.allocation)):
            self.enterContext(patch.object(win, name, replacement, create=True))
        self.rect = [100, 100, 8, 8]
        self.uia.root.box = dict(x=100, y=100, width=8, height=8)
        self.table = Node(self.uia, 'Results', 50026, parent=self.uia.root)
        self.table.box = dict(x=102, y=102, width=12, height=4)
        self.uia.bounds = lambda item: item.box
        self.uia.within = lambda a, b: a is b or (a.parent_node is not None and self.uia.within(a.parent_node, b))
        self.hit = self.table
        self.uia.from_point = lambda *point: self.hit
        self.native = self.enterContext(patch.object(win, 'user32', create=True))
        self.native.IsWindowVisible.return_value = True
        self.native.IsIconic.return_value = False
        self.enterContext(patch.object(win, 'window_from_point', return_value=88))
        self.native_root = self.enterContext(patch.object(win, 'root_window', return_value=self.binding.hwnd))
        self.enterContext(patch.object(win, 'hwnd_pid', return_value=self.binding.pid))
        self.enterContext(patch.object(flow, 'result_surface', return_value=self.table))
        self.selector = dict(role='group', automation_id='Results')

    def check(self, point=(103, 103), **kwargs):
        return flow.check_result_point(self.binding, self.uia, self.selector, point, **kwargs)

    def test_unknown_offscreen_needs_reviewed_frame_before_accepting_clipped_owned_table(self):
        with self.assertRaisesRegex(ValueError, 'result_point_outside_table'):
            self.check()
        self.assertEqual(self.check(reviewed_frame=self.rect), self.table.box)

    def test_unknown_clipped_container_can_be_read_without_authorizing_a_cell_click(self):
        with patch.object(row_vision, '_window_rect', return_value=self.rect):
            self.assertTrue(result_cell.readable_surface(self.binding, self.uia, self.table, self.uia.root))
            with self.assertRaisesRegex(ValueError, 'result_point_outside_table'):
                self.check()
            # The opaque group must now reach the column-read diagnostic and local handoff.
            with patch.object(result_cell, '_column', side_effect=win.BridgeError('result_column_unavailable')):
                with self.assertRaisesRegex(win.BridgeError, 'result_column_unavailable'):
                    result_cell.find(self.binding, self.uia, self.table, 'A1', column='ID')

    def test_partial_read_rejects_offscreen_minimized_hidden_and_nonoverlapping_surfaces(self):
        with patch.object(row_vision, '_window_rect', return_value=self.rect):
            for setting in ('offscreen', 'minimized', 'hidden', 'outside', 'invalid', 'not_container'):
                with self.subTest(setting=setting):
                    self.table.values.update({30022: None, 30003: 50026})
                    self.table.box = dict(x=102, y=102, width=12, height=4)
                    self.native.IsIconic.return_value = setting == 'minimized'
                    self.native.IsWindowVisible.return_value = setting != 'hidden'
                    if setting == 'offscreen':
                        self.table.values[30022] = True
                    elif setting == 'outside':
                        self.table.box['x'] = 108
                    elif setting == 'invalid':
                        self.table.box['width'] = 0
                    elif setting == 'not_container':
                        self.table.values[30003] = 50004
                    self.assertFalse(result_cell.readable_surface(self.binding, self.uia, self.table, self.uia.root))

    def test_partial_read_preserves_owner_and_protection_checks(self):
        with patch.object(row_vision, '_window_rect', return_value=self.rect):
            self.table.values[30002] = 8
            with self.assertRaisesRegex(win.BridgeError, 'target_owner_mismatch'):
                result_cell.readable_surface(self.binding, self.uia, self.table, self.uia.root)
            self.table.values[30002] = 7
            self.table.values[30019] = True
            with self.assertRaisesRegex(win.BridgeError, 'target_protected'):
                result_cell.readable_surface(self.binding, self.uia, self.table, self.uia.root)

    def test_point_in_raw_table_but_outside_captured_frame_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'result_point_outside_frame'):
            self.check((110, 103), reviewed_frame=self.rect)

    def test_explicit_offscreen_and_minimized_are_not_clipping_exceptions(self):
        self.table.values[30022] = True
        with self.assertRaisesRegex(ValueError, 'result_point_outside_table'):
            self.check(reviewed_frame=self.rect)
        self.table.values[30022] = None
        self.native.IsIconic.return_value = True
        with self.assertRaisesRegex(win.BridgeError, 'target_window_minimized'):
            self.check(reviewed_frame=self.rect)

    def test_wrong_owner_and_protected_table_remain_blocked(self):
        self.table.values[30002] = 8
        with self.assertRaisesRegex(win.BridgeError, 'target_owner_mismatch'):
            self.check(reviewed_frame=self.rect)
        self.table.values[30002] = 7
        self.table.values[30019] = True
        with self.assertRaisesRegex(win.BridgeError, 'target_protected'):
            self.check(reviewed_frame=self.rect)

    def test_occluding_native_window_and_form_control_are_not_data_points(self):
        self.native_root.return_value = 999
        with self.assertRaisesRegex(ValueError, 'result_cell_occluded'):
            self.check(reviewed_frame=self.rect)
        self.native_root.return_value = self.binding.hwnd
        self.hit = Node(self.uia, 'OverlayButton', 50000, parent=self.table)
        with self.assertRaisesRegex(ValueError, 'result_point_not_data'):
            self.check(reviewed_frame=self.rect)

    def test_before_click_requires_raw_table_geometry_even_if_visible_region_did_not_change(self):
        png = grounding._png(8, 8, bytes((31, 61, 91, 0)) * 64)
        proof = dict(receipt='1'*64, frame='2'*64, selector=self.selector, point=[3, 3],
                     window_rect=self.rect, table_box=deepcopy(self.table.box))
        with (patch.object(grounding, '_directory', return_value=Path('.')),
              patch.object(grounding, 'read_frame', return_value=(None, png)),
              patch.object(grounding, '_capture', return_value=(self.rect, png)) as capture,
              patch.object(flow.app.bridge, 'UIA') as context):
            context.return_value.__enter__.return_value = self.uia
            flow.check_result_frame(self.binding, proof, (103, 103), lambda: None)
            with self.assertRaisesRegex(ValueError, 'result_frame_changed'):
                flow.check_result_frame(self.binding, proof, (104, 103), lambda: None)
            self.table.box['width'] += 1  # Same clipped pixels, different provider geometry.
            with self.assertRaisesRegex(ValueError, 'result_table_changed'):
                flow.check_result_frame(self.binding, proof, (103, 103), lambda: None)
            self.table.box = deepcopy(proof['table_box'])
            capture.return_value = ([100, 100, 9, 8], png)
            with self.assertRaisesRegex(ValueError, 'result_frame_changed'):
                flow.check_result_frame(self.binding, proof, (103, 103), lambda: None)


if __name__ == '__main__':
    unittest.main()
