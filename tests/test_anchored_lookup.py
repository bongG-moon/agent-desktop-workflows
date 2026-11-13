"""An observed relative point resolves a live, scoped UIA control, never a click."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from test_control_lookup import Binding, FakeUIA, Node, native, win


class AnchoredLookupTests(unittest.TestCase):
    def setUp(self):
        self.uia, self.binding = FakeUIA(), Binding()
        self.root = self.uia.root
        self.anchor = Node(self.uia, '', 50026, parent=self.root, name='Device form')
        self.field = Node(self.uia, '83489', 50004, parent=self.anchor)
        self.field.values[30012] = 'RichEdit20W'
        self.boxes = {
            self.root: dict(x=0, y=0, width=1000, height=800),
            self.anchor: dict(x=100, y=100, width=500, height=400),
            self.field: dict(x=200, y=200, width=300, height=40),
        }
        for node in self.boxes:
            node.values[30022] = False
        self.uia.bounds = Mock(side_effect=lambda node: dict(self.boxes[node]))
        self.uia.from_point = Mock(return_value=self.field)
        self.uia.point_descendant = Mock(return_value=self.field)
        def within(node, parent):
            while node is not None:
                if node is parent:
                    return True
                node = node.parent_node
            return False
        self.uia.within = within
        self.selector = dict(anchor=dict(name='Device form', role='group'),
                             point=[.5, .3], role='textfield', class_name='RichEdit20W')
        for target, replacement in (
            ('_Com', lambda pointer, uia: uia.handles[pointer.value]),
            ('Element', lambda pointer, uia: uia.nodes[pointer.value]),
            ('_sig', self.uia.allocation),
            ('window_from_point', lambda x, y: 101),
            ('root_window', lambda hwnd: 123),
            ('hwnd_pid', lambda hwnd: 7),
        ):
            p = patch.object(win, target, replacement, create=True)
            p.start()
            self.addCleanup(p.stop)

    def resolve(self):
        return native.resolve(self.binding, self.uia, self.selector)

    def test_fresh_anchor_geometry_resolves_unnamed_editor_without_persisted_handle(self):
        self.assertIs(self.resolve(), self.field)
        self.uia.from_point.assert_called_once_with(350, 220)
        self.uia.point_descendant.assert_not_called()
        self.assertNotIn('83489', str(self.selector))
        # A moved but stable form changes the current point, not the recipe.
        self.boxes[self.anchor]['x'] += 60
        self.boxes[self.field]['x'] += 60
        self.assertIs(self.resolve(), self.field)
        self.uia.from_point.assert_called_with(410, 220)

    def test_wrong_role_or_class_is_not_same_target(self):
        for key, value in ((30003, 50000), (30012, 'OtherEditor')):
            with self.subTest(key=key):
                old = self.field.values[key]
                self.field.values[key] = value
                with self.assertRaisesRegex(win.BridgeError, 'anchored_target_type_changed'):
                    self.resolve()
                self.field.values[key] = old

    def test_protected_and_foreign_target_fail_before_type_reads(self):
        for key, value, error in ((30019, True, 'target_protected'), (30002, 9, 'target_owner_mismatch')):
            with self.subTest(key=key):
                old = self.field.values[key]
                self.field.values[key] = value
                self.field.reads.clear()
                with self.assertRaisesRegex(win.BridgeError, error):
                    self.resolve()
                self.assertNotIn(30012, self.field.reads)
                self.field.values[key] = old

    def test_other_window_over_point_stops_before_uia_hit(self):
        with patch.object(win, 'root_window', return_value=999), \
             self.assertRaisesRegex(win.BridgeError, 'anchored_target_occluded'):
            self.resolve()
        self.uia.from_point.assert_not_called()

    def test_same_process_field_outside_demonstrated_anchor_is_rejected(self):
        self.field.parent_node = self.root
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_outside_anchor'):
            self.resolve()

    def test_duplicate_anchor_is_ambiguous_before_point_is_used(self):
        Node(self.uia, '', 50026, parent=self.root, name='Device form')
        with self.assertRaisesRegex(win.BridgeError, 'target_ambiguous'):
            self.resolve()
        self.uia.from_point.assert_not_called()

    def test_anchor_movement_during_resolution_is_rejected(self):
        calls = 0
        def bounds(node):
            nonlocal calls
            current = dict(self.boxes[node])
            if node is self.anchor:
                calls += 1
                if calls > 2:
                    current['x'] += 1
            return current
        self.uia.bounds.side_effect = bounds
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_changed'):
            self.resolve()

    def test_parent_hit_is_refined_then_full_ancestry_checked(self):
        self.uia.from_point.return_value = self.anchor
        self.assertIs(self.resolve(), self.field)
        self.uia.point_descendant.assert_called_once_with(self.anchor, 350, 220, 7)
        self.field.values[30019] = True
        with self.assertRaisesRegex(win.BridgeError, 'target_protected'):
            self.resolve()

    def test_explicit_offscreen_or_point_outside_control_stops(self):
        self.field.values[30022] = True
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_not_visible'):
            self.resolve()
        self.field.values[30022] = False
        self.boxes[self.field]['y'] = 260
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_not_visible'):
            self.resolve()

    def test_selector_rejects_nested_anchors_handles_edges_and_nonfinite_points(self):
        for update in ({'point': [0, .5]}, {'point': [True, .5]}, {'point': [.5, float('nan')]},
                       {'class_name': ''}, {'hwnd': 101}, {'anchor': deepcopy(self.selector)}):
            with self.subTest(update=update), self.assertRaises(win.BridgeError):
                native._selector(dict(self.selector, **update))


class BoundAnchorTests(unittest.TestCase):
    setUp = AnchoredLookupTests.setUp

    def bind(self):
        return native.bind_anchored(self.binding, self.uia, self.selector)

    def test_one_primitive_revalidates_same_target_without_repeating_findall(self):
        proof = self.bind()
        self.assertIs(proof.element, self.field)
        self.assertEqual(len(self.uia.queries), 2)
        for _ in range(4):
            self.assertIs(proof.revalidate(), self.field)
        self.assertEqual(len(self.uia.queries), 2)
        proof.close()
        with self.assertRaisesRegex(win.BridgeError, 'anchored_binding_expired'):
            proof.revalidate()

    def test_new_same_class_field_at_the_point_never_retargets_bound_action(self):
        proof = self.bind()
        replacement = Node(self.uia, '', 50004, parent=self.anchor)
        replacement.values[30012] = self.field.values[30012]
        self.uia.from_point.return_value = replacement
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_changed'):
            proof.revalidate()

    def test_duplicate_added_elsewhere_does_not_retarget_but_new_step_must_resolve_again(self):
        proof = self.bind()
        Node(self.uia, '', 50026, parent=self.root, name='Device form')
        self.assertIs(proof.revalidate(), self.field)
        proof.close()
        with self.assertRaisesRegex(win.BridgeError, 'target_ambiguous'):
            self.bind()

    def test_anchor_and_target_cannot_move_during_bound_primitive(self):
        for target in (self.anchor, self.field):
            with self.subTest(target=target):
                proof = self.bind()
                self.boxes[target]['x'] += 1
                with self.assertRaisesRegex(win.BridgeError, 'anchored_target_changed'):
                    proof.revalidate()
                self.boxes[target]['x'] -= 1

    def test_anchor_identity_and_target_metadata_changes_are_rejected(self):
        for target, key, new in ((self.anchor, 30005, 'Different form'),
                                 (self.anchor, 30003, 50009),
                                 (self.field, 30003, 50000),
                                 (self.field, 30012, 'DifferentEditor')):
            with self.subTest(key=key, target=target):
                proof = self.bind()
                old = target.values[key]
                target.values[key] = new
                with self.assertRaises(win.BridgeError):
                    proof.revalidate()
                target.values[key] = old

    def test_protection_and_pid_are_read_again_on_every_revalidation(self):
        for target in (self.anchor, self.field):
            for key, new, code in ((30019, True, 'target_protected'), (30002, 8, 'target_owner_mismatch')):
                with self.subTest(target=target, key=key):
                    proof = self.bind()
                    old = target.values[key]
                    target.values[key] = new
                    with self.assertRaisesRegex(win.BridgeError, code):
                        proof.revalidate()
                    target.values[key] = old

    def test_reparented_target_and_replaced_root_are_rejected(self):
        proof = self.bind()
        self.field.parent_node = self.root
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_outside_anchor'):
            proof.revalidate()
        self.field.parent_node = self.anchor
        other = Node(self.uia, 'Business', 50032)
        self.uia.from_handle = lambda hwnd: other
        with self.assertRaisesRegex(win.BridgeError, 'root_changed'):
            proof.revalidate()

    def test_occlusion_hidden_state_and_closed_session_are_rejected(self):
        proof = self.bind()
        with patch.object(win, 'window_from_point', return_value=222), \
             self.assertRaisesRegex(win.BridgeError, 'anchored_target_occluded'):
            proof.revalidate()
        self.field.values[30022] = True
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_not_visible'):
            proof.revalidate()
        self.field.values[30022] = False
        self.uia.closed = True
        with self.assertRaisesRegex(win.BridgeError, 'anchored_binding_expired'):
            proof.revalidate()

    def test_recorded_selector_is_copied_and_parent_hit_still_proves_same_field(self):
        proof = self.bind()
        self.selector['anchor']['name'] = 'Changed recipe'
        self.uia.from_point.return_value = self.anchor
        self.assertIs(proof.revalidate(), self.field)
        replacement = Node(self.uia, '', 50004, parent=self.anchor)
        self.uia.point_descendant.return_value = replacement
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_changed'):
            proof.revalidate()

    def test_recorded_anchor_ancestor_identity_must_remain_current(self):
        self.selector['anchor']['ancestors'] = [dict(role='window', automation_id='Business')]
        proof = self.bind()
        self.root.values[30011] = 'Another business view'
        with self.assertRaisesRegex(win.BridgeError, 'anchored_target_changed'):
            proof.revalidate()


if __name__ == '__main__':
    unittest.main()
