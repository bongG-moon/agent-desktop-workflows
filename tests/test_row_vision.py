"""A painted row has no named child buttons. Prove identity and success independently."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # loads the pinned Windows bridge in a source checkout
import row_vision as vision


KEY = [2, 1, 18, 12]
BUTTON = [51, 1, 14, 12]
STATE = [24, 1, 20, 12]
W, H = 68, 15


def frame(rows, pos=(100, 200), background=24):
    # Horizontal/vertical glyphs make three identities, even on a changed hover background.
    raw = bytearray([background, background, background, 255] * W * H * len(rows))
    layout = []
    def dot(x, y):
        start = (W*y+x)*4
        raw[start:start+3] = bytes([220])*3
    for i, (key, status, button) in enumerate(rows):
        y = i*H
        layout.append(([pos[0], pos[1]+y, W, H], {'role': 'listitem', 'scope': ['Items']}))
        for t in range(8):
            for d in (0,1):
                dot(4+t, y+3+d) if key=='a' else dot(6+d, y+t+3) if key=='b' else dot(4+t,y+t+2+d)
                if status=='stopped':
                    dot(27+t,y+4+d)
                elif status=='running':
                    dot(29+d,y+3+t)
                elif status=='loading':
                    dot(28+t,y+2+t+d)
                if button=='start':
                    dot(55+t,y+3+d)
                else:
                    dot(57+d,y+2+t)
    return [*pos,W,H*len(rows)], layout, bytes(raw)


class RowRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.stopped = [('a','stopped','start'),('b','stopped','start')]
        self.image = frame(self.stopped)
        self.capture = patch.object(vision,'_snapshot',side_effect=lambda _:self.image)
        self.capture.start()
        self.addCleanup(self.capture.stop)
        self.first = vision.teach_before(None,[110,205],KEY,BUTTON,STATE)
        self.second = vision.teach_before(None,[110,219],KEY,BUTTON,STATE)

    def demonstrated(self):
        self.image = frame([('a','running','stop'),self.stopped[1]])
        return vision.teach_after(None,self.first)

    def test_before_after_and_transfer_identify_moved_row(self):
        task = self.demonstrated()
        other = vision.transfer_after(self.second,task)
        self.image = frame([self.stopped[1],('a','running','stop')],pos=(-90,153),background=61)
        self.assertEqual(vision.observe_row(None,task)['kind'],'after')
        ready = vision.observe_row(None,other)
        self.assertEqual(ready,dict(kind='before',row_bounds=[-90,153,W,H],point=[-32,160]))
        self.assertTrue(vision.template_preview(task,'after').startswith(b'P6\n20 12\n255\n'))

    def test_same_identity_expected_state_and_unchanged_button_is_not_success(self):
        task = self.demonstrated()
        self.image = frame([('a','loading','start'),('b','running','stop')])
        self.assertEqual(vision.observe_row(None,task)['kind'],'unknown')
        self.image = frame([('a','stopped','stop'),('b','running','stop')])
        self.assertEqual(vision.observe_row(None,task)['kind'],'unknown')

    def test_duplicate_identity_does_not_allow_input_or_teaching(self):
        task = self.demonstrated()
        self.image = frame([self.stopped[0]]*2)
        self.assertEqual(vision.observe_row(None,task),dict(kind='ambiguous',row_bounds=None,point=None))
        with self.assertRaisesRegex(vision.bridge.BridgeError,'row_key_not_unique'):
            vision.teach_before(None,[110,205],KEY,BUTTON,STATE)
        self.image = frame([self.stopped[1]])
        self.assertEqual(vision.observe_row(None,task)['kind'],'missing')

    def test_original_row_preview_checked_and_no_unchanged_result_is_teachable(self):
        preview = vision.row_preview(None,[110,205])
        self.image = frame([self.stopped[1],self.stopped[0]])
        with self.assertRaisesRegex(vision.bridge.BridgeError,'row_preview_changed'):
            vision.teach_before(None,[110,205],KEY,BUTTON,STATE,preview=preview)
        with self.assertRaisesRegex(vision.bridge.BridgeError,'row_state_change_unproven'):
            vision.teach_after(None,self.first)

    def test_sort_between_hit_test_and_input_rejects_stale_pixels(self):
        task = self.demonstrated()
        self.image = frame(self.stopped)
        observed = vision.observe_row(None, task)
        changed = frame(list(reversed(self.stopped)))
        with patch.object(vision, '_window_rect', return_value=changed[0]), \
                patch.object(vision, '_frame', return_value=changed[2]):
            with self.assertRaisesRegex(vision.bridge.BridgeError, 'row_pixels_changed'):
                vision.confirm_pixels(None, task, observed)

    def test_untrusted_geometry_masks_or_scope_cannot_be_reused(self):
        task = self.demonstrated()
        for change in ({'size':[10_000,10]}, {'boxes':{'button':BUTTON,'key':KEY,'state':KEY}},
                       {'key':'AAAA'}, {'after':task['before']}):
            with self.assertRaises(vision.bridge.BridgeError):
                vision.validate_spec(dict(task,**change))
        incompatible = copy.deepcopy(self.second)
        incompatible['scope']=['OtherList']
        with self.assertRaisesRegex(vision.bridge.BridgeError,'row_example_incompatible'):
            vision.transfer_after(incompatible,task)

    def test_a_featureless_painted_box_does_not_become_a_wildcard(self):
        with self.assertRaisesRegex(vision.bridge.BridgeError,'row_patch_lacks_detail'):
            vision.teach_before(None,[110,205],[16,2,6,10],BUTTON,STATE)

    def test_provider_hit_testing_only_the_list_still_requires_a_unique_visible_row(self):
        def element(role, rect, children=()):
            e = MagicMock()
            e.rect, e.descendants = rect, children
            e.property.side_effect = {30003:role, 30022:False, 30010:True}.get
            return e
        row = element(50007, [0,20,100,30])
        other = element(50007, [0,50,100,30])
        outer = element(50008, [0,0,100,100], [row,other])
        uia = MagicMock()
        uia.children.side_effect = lambda parent: parent.descendants
        uia.bounds.side_effect = lambda e: dict(zip(('x','y','width','height'),e.rect))
        uia.secure.return_value = False
        uia.pid.return_value = 100
        self.assertTrue(vision._list_hit_row(uia, outer,100,[80,30],row.rect))
        outer.descendants.append(element(50000, [60,25,30,15]))
        self.assertFalse(vision._list_hit_row(uia, outer,100,[80,30],row.rect))
        self.assertFalse(vision._list_hit_row(uia, outer.descendants[-1],100,[80,30],row.rect))

    def test_changed_suffix_cannot_hide_in_an_otherwise_identical_long_row_or_status(self):
        width, height = 288, 24
        old = bytearray(width * height)
        for x in range(8, 248, 8):
            for y in range(4, 18):
                old[y*width+x:y*width+x+3] = b'\x01'*3
        new = old.copy()
        for y in range(4, 18):
            new[y*width+278:y*width+281] = b'\x01'*3
        self.assertLess(vision._difference(old,new),.14)
        self.assertGreater(vision._match_distance(old,new,width),.14)
        self.assertEqual(vision._match_distance(old,old,width),0)

    def test_disabled_or_protected_child_over_an_enabled_row_cannot_receive_a_click(self):
        binding = MagicMock(hwnd=8,pid=25)
        row, child, root = MagicMock(), MagicMock(), MagicMock()
        rect = [0,0,100,30]
        row.property.side_effect = {30003:50007, 30010:True}.get
        child.property.side_effect = {30003:50000, 30010:False}.get
        uia = MagicMock()
        uia.from_handle.return_value, uia.from_point.return_value = root, child
        uia.pid.return_value, uia.secure.return_value, uia.within.return_value = 25,False,True
        uia.parent.return_value = row
        uia.bounds.return_value = dict(zip(('x','y','width','height'),rect))
        observed = {'kind':'before','row_bounds':rect,'point':[70,20]}
        with patch.object(vision,'_row_layout',return_value=(rect,[(rect,{})])), \
                patch.object(vision.bridge,'root_window',return_value=8), \
                patch.object(vision.bridge,'window_from_point',return_value=8), \
                patch.object(vision.bridge,'hwnd_pid',return_value=25), \
                patch.object(vision.bridge,'foreground',return_value=8), \
                patch.object(vision.bridge,'UIA') as factory:
            factory.return_value.__enter__.return_value = uia
            with self.assertRaisesRegex(vision.bridge.BridgeError,'row_hit_not_owned'):
                vision.confirm_point(binding, observed)
            child.property.side_effect = {30003:50000, 30010:True}.get
            uia.secure.side_effect = lambda e: e is child
            with self.assertRaisesRegex(vision.bridge.BridgeError,'row_hit_not_owned'):
                vision.confirm_point(binding, observed)
