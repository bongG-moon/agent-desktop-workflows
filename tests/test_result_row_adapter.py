"""Exact result evidence and one-time click checks for a fully visible painted grid."""
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import Mock, patch

from test_control_lookup import win
import result_row_adapter as adapter


def word(value, x, y):
    return dict(text=value, x=x, y=y, width=9*len(value), height=12)


def surface():
    width, height = 240, 110
    raw = bytearray(b'\xff'*(width*height*4))
    def ink(x,y):
        start = (y*width+x)*4
        raw[start:start+3] = bytes(3)
    for x in (4,110,234):
        for y in range(4,105):
            ink(x,y)
    for y in (4,36,70,104):
        for x in range(4,235):
            ink(x,y)
    # Real glyph-like contrast near the point for the standard 41x41 patch.
    for y in range(43,55):
        for x in range(12,49,2):
            ink(x,y)
    return bytes(raw)


class LocalResultAdapterTests(TestCase):
    def setUp(self):
        self.patches = self.enterContext(ExitStack())
        self.binding = Mock(hwnd=3, pid=9)
        self.tick = self.patches.enter_context(patch.object(adapter.win,'input_tick',return_value=100))
        self.box = (64, 89, 240, 110)
        self.frame = ((40,60,800,600), self.box, self.box, surface())
        self.capture = self.patches.enter_context(patch.object(adapter,'_capture',return_value=self.frame))
        self.ocr = self.patches.enter_context(patch.object(adapter,'_ocr',return_value=('en',[
            word('Equip',12,10), word('ID',65,10), word('Name',125,10),
            word('D124',12,42), word('Alpha',125,42), word('D124X',12,76), word('D124',125,76)])))

    def observe(self, expected='D124', total=2):
        return adapter.observe(self.binding, Mock(), Mock(), 'Equip ID', expected, lambda: None, row_count=total)

    def test_nonzero_screen_origin_links_exact_id_to_its_column_and_single_row(self):
        result = self.observe()
        proof = result['proof']
        self.assertEqual((94,137), proof.point)
        self.assertEqual('D124',result['value'])
        self.assertEqual({'status':'observed','source':'windows_ocr'},result['value_evidence'])
        self.assertEqual(1,result['result_observation']['matching_rows'])
        self.assertFalse(result['input_dispatched'])
        self.ocr.assert_called_once()

    def test_no_unrelated_or_partial_matches(self):
        for target in ('124', 'd124', 'D125', 'Alpha'):
            with self.subTest(target=target):
                result = self.observe(target)
                self.assertEqual('result_value_mismatch', result['diagnostic'])
                self.assertNotIn('proof',result)

    def test_zero_rows_skip_imaging_and_duplicates_do_not_choose_first(self):
        self.assertEqual('result_value_missing',self.observe(total=0)['diagnostic'])
        self.capture.assert_not_called()
        self.ocr.return_value[1][5]['text'] = 'D124'
        result = self.observe()
        self.assertEqual('result_value_ambiguous', result['diagnostic'])
        self.assertEqual(2, result['result_observation']['matching_rows'])

    def test_clipping_scroll_layout_or_unknown_total_cannot_prove_uniqueness(self):
        for expected_count in (True, None, -1, 129, 3, 1):
            self.assertEqual('blocked',self.observe(total=expected_count)['status'])
        self.ocr.return_value[1][1]['x'] = 110
        self.assertEqual('result_ocr_layout_unavailable',self.observe()['diagnostic'])

    def test_reordered_named_column_and_moved_row(self):
        xs, ys = [4,130,280], [4,36,70,104]
        evidence = {}
        words = [word('Category',12,10),word('Equip ID',145,10),word('D124X',145,42),
                 word('D124',12,42),word('D124',145,76)]
        point = adapter._match(xs,ys,words,'Equip ID','D124',2,evidence)
        self.assertEqual((163,82),point)
        self.assertEqual(1,evidence['column_index'])

    def test_confusable_spelling_can_only_veto_an_exact_match(self):
        other = self.ocr.return_value[1][5]
        for value in ('D1240', 'DI 240'):
            other['text'] = value
            self.assertEqual('verified', self.observe()['status'])
        for value in ('DI24', 'Dl 24', 'D 124'):
            other['text'] = value
            result = self.observe()
            self.assertEqual('result_ocr_identity_uncertain', result['diagnostic'])
            self.assertNotIn('proof', result)
        self.ocr.return_value[1][3]['text'] = 'DI24'
        self.assertEqual('result_value_mismatch', self.observe()['diagnostic'])

    def test_grid_dimensions_must_match_buffer(self):
        with self.assertRaisesRegex(win.BridgeError,'result_ocr_frame_invalid'):
            adapter._grid(surface(), 239,110)
        self.capture.return_value = (*self.frame[:3], bytes(240*110*4))
        self.assertEqual('result_ocr_layout_unavailable',self.observe()['diagnostic'])
        self.ocr.assert_not_called()

    def test_omitted_suffix_pixels_do_not_turn_a_prefix_into_an_id(self):
        raw = bytearray(self.frame[3])
        for y in range(42,54):
            i = (y*240 + 55)*4
            raw[i:i+3] = bytes(3)
        self.capture.return_value = (*self.frame[:3],bytes(raw))
        result = self.observe()
        self.assertEqual('result_ocr_cell_unreadable', result['diagnostic'])
        self.assertNotIn('proof', result)

    def test_high_dpi_grid_stroke_is_not_an_unread_glyph(self):
        raw = bytearray(self.frame[3])
        for x in range(6,109):
            i = (34*240+x)*4
            raw[i:i+3] = bytes([191])*3
        adapter._all_ink_read(raw,240,(4,4,110,36),[(12,10,36,12,'Code')])

    def test_changes_outside_clicked_cell_also_invalidate_exact_id_proof(self):
        proof = self.observe()['proof']
        changed = bytearray(self.frame[3])
        changed[-4] ^= 16
        self.capture.return_value = (*self.frame[:3],bytes(changed))
        def before_click(binding,spec,guard,*,before_input,reviewed_point):
            try:
                before_input(reviewed_point)
            except win.BridgeError as error:
                return dict(status='blocked',diagnostic=error.code,input_dispatched=False)
            self.fail('changed table accepted')
        with patch.object(adapter.visual_target,'execute',side_effect=before_click) as dispatch:
            result = adapter.execute(self.binding,Mock(),Mock(),proof,lambda:None)
        dispatch.assert_called_once()
        self.assertEqual('result_frame_changed',result['diagnostic'])
        self.assertFalse(result['input_dispatched'])
        self.ocr.assert_called_once()

    def test_partial_send_remains_uncertain(self):
        proof = self.observe()['proof']
        receipt = dict(status='uncertain', diagnostic='partial_input', input_dispatched=None)
        with patch.object(adapter.visual_target,'execute',return_value=receipt) as dispatch:
            self.assertIs(receipt,adapter.execute(self.binding,Mock(),Mock(),proof, lambda:None))
        dispatch.assert_called_once()

    def test_changed_miss_observes_same_surface_without_ocr(self):
        result = self.observe('D500')
        previous = result['_miss_snapshot']
        self.assertFalse(adapter.changed(self.binding,Mock(),Mock(),previous,lambda:None))
        raw = bytearray(self.frame[3])
        raw[400] ^= 64
        self.capture.return_value = (*self.frame[:3], bytes(raw))
        self.assertTrue(adapter.changed(self.binding,Mock(),Mock(),previous,lambda:None))
        self.ocr.assert_called_once()
        self.capture.return_value = ((0,0,800,600), *self.frame[1:])
        with self.assertRaisesRegex(win.BridgeError,'result_ocr_surface_changed'):
            adapter.changed(self.binding,Mock(),Mock(),previous,lambda:None)
        self.tick.return_value = 120
        with self.assertRaisesRegex(win.BridgeError,'foreground_or_input_changed'):
            adapter.changed(self.binding,Mock(),Mock(),previous,lambda:None)

    def test_input_during_ocr_or_before_foreground_blocks_double_click(self):
        self.tick.side_effect = (100,101)
        result = self.observe()
        self.assertEqual('foreground_or_input_changed', result['diagnostic'])
        self.assertFalse(result['input_dispatched'])
        self.tick.side_effect = None
        proof = self.observe()['proof']
        self.tick.return_value = 101
        with patch.object(adapter.visual_target,'observe') as locate:
            result = adapter.execute(self.binding, Mock(), Mock(), proof, lambda: None)
        self.assertEqual('foreground_or_input_changed', result['diagnostic'])
        self.assertFalse(result['input_dispatched'])
        locate.assert_not_called()
