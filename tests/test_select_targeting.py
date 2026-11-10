import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import targeting


def node(path, role='group', *, name='', identifier='', rect=None, **attributes):
    bounds = dict(zip(('x', 'y', 'width', 'height'), rect)) if rect else {}
    return dict(path=path, parent_path=path.rpartition('/')[0], role=role, name=name,
                automation_id=identifier, bounds=bounds, secure=False, enabled=True,
                offscreen=attributes.pop('offscreen', False), **attributes)


def form():
    # Two label/field pairs on one row. The provider reports collapsed fields offscreen.
    return [node('0', 'window'), node('0/0', identifier='psView'),
            node('0/0/0', identifier='pnlConditionArea'),
            node('0/0/0/1', name='Mode', rect=(10, 100, 88, 19)),
            node('0/0/0/1/0', 'statictext', name='Mode', rect=(10, 100, 88, 19)),
            node('0/0/0/2', 'combobox', identifier='categoryCode',
                 rect=(145, 100, 170, 19), offscreen=True),
            node('0/0/0/3', 'statictext', name='Family', rect=(333, 101, 60, 19)),
            node('0/0/0/4', 'combobox', identifier='familyCode', rect=(430, 101, 170, 19))]


class TargetingTests(unittest.TestCase):
    def test_wrapped_label_offscreen_with_real_bounds_and_dense_form(self):
        nodes = form()
        self.assertEqual((5, 'sibling_geometry'), targeting.resolve(nodes, 'MODE', role='combobox'))
        self.assertEqual((7, 'sibling_geometry'), targeting.resolve(nodes, 'Family'))
        self.assertEqual(['conditions', 'content'], targeting.scopes(nodes, 5))
        self.assertEqual({5: {'semantic_label': 'Mode', 'label_source': 'sibling_geometry'},
                          7: {'semantic_label': 'Family', 'label_source': 'sibling_geometry'}},
                         targeting.field_labels(nodes))

    def test_explicit_labeled_by_with_no_bounds_and_conflicting_geometry(self):
        nodes = form()
        nodes[5].update(label_source='labeled_by', semantic_label='Plan')
        self.assertEqual((5, 'labeled_by'), targeting.resolve(nodes, 'Plan', role='combobox'))
        with self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
            targeting.resolve(nodes, 'Mode', role='combobox')
        nodes[5]['bounds'] = {}
        self.assertEqual((5, 'labeled_by'), targeting.resolve(nodes, 'Plan', role='combobox'))
        self.assertEqual({'semantic_label': 'Plan', 'label_source': 'labeled_by'},
                         targeting.field_labels(nodes)[5])

    def test_geometry_does_not_override_inaccessible_relation_or_redacted_intervening_peer(self):
        nodes = form()
        nodes[5]['label_source'] = 'invalid_labeled_by'
        with self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
            targeting.resolve(nodes, 'Mode')
        self.assertNotIn(5, targeting.field_labels(nodes))
        nodes = form() + [dict(path='0/0/0/9', parent_path='0/0/0', secure=True)]
        with self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
            targeting.resolve(nodes, 'Mode')

    def test_hidden_or_unknown_labels_are_not_shown_or_used_for_selection(self):
        for flag in (True, None):
            nodes = form()
            nodes[4]['offscreen'] = flag
            with self.subTest(flag=flag):
                self.assertNotIn(5, targeting.field_labels(nodes))
                with self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
                    targeting.resolve(nodes, 'Mode')

    def test_duplicate_explicit_labels_in_one_container_are_ambiguous(self):
        nodes = form()
        for index in (5, 7):
            nodes[index].update(semantic_label='Mode', label_source='labeled_by')
        self.assertEqual({}, targeting.field_labels(nodes))
        with self.assertRaisesRegex(targeting.SelectionError, 'target_ambiguous'):
            targeting.resolve(nodes, 'Mode')

    def test_id_on_other_field_conflicting_with_observed_label(self):
        nodes = form()
        nodes[7]['automation_id'] = 'MODE'
        with self.assertRaisesRegex(targeting.SelectionError, 'target_ambiguous'):
            targeting.resolve(nodes, 'Mode', role='combobox')

    def test_different_role_between_label_and_field_blocks_guess(self):
        for obstacle in [(101, 100, 30, 18), (145, 100, 170, 19)]:
            nodes = form() + [node('0/0/0/5', 'button', name='Help', rect=obstacle)]
            with self.subTest(obstacle=obstacle), self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
                targeting.resolve(nodes, 'Mode', role='combobox')

    def test_overlapping_fields_or_labels_block_geometry_guess(self):
        for extra in (node('0/0/0/8', 'combobox', rect=(145, 100, 170, 19)),
                      node('0/0/0/8', 'statictext', name='State', rect=(10, 100, 88, 19))):
            with self.subTest(extra=extra), self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
                targeting.resolve(form() + [extra], 'Mode', role='combobox')

    def test_no_cross_container_or_stale_provider_hint_or_blank_rectangle(self):
        nodes = form()
        nodes[5]['path'], nodes[5]['parent_path'] = '0/1', '0'
        nodes[5].update(label_source='sibling_geometry', semantic_label='Mode')
        with self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
            targeting.resolve(nodes, 'Mode', role='combobox')
        nodes = form()
        nodes[5]['bounds'] = dict(x=0, y=0, width=0, height=0)
        with self.assertRaisesRegex(targeting.SelectionError, 'target_missing'):
            targeting.resolve(nodes, 'Mode', role='combobox')

    def test_toolbar_content_and_condition_command_alias(self):
        nodes = form() + [node('0/1', 'toolbar', identifier='tsViewCommand'),
                          node('0/1/0', 'button', name='Search'),
                          node('0/0/1', identifier='pnlConditionButtonArea'),
                          node('0/0/1/0', 'button', name='Search')]
        with self.assertRaisesRegex(targeting.SelectionError, 'target_ambiguous'):
            targeting.resolve(nodes, 'Search')
        for area, target in [('commands', 9), ('content', 11), ('condition-commands', 11),
                             ('tsViewCommand', 9), ('psView', 11)]:
            self.assertEqual((target, 'name'), targeting.resolve(nodes, 'Search', within=area))

    def test_menu_path_and_tree_item_use_live_name(self):
        nodes = [node('0', 'window'), node('0/0', 'menuitem', name='Export'),
                 node('0/0/0', 'menuitem', name='Active sheet'), node('0/1', 'treeitem', name='Export')]
        self.assertEqual((3, 'name'), targeting.resolve(nodes, 'Export', role='treeitem'))
        with self.assertRaisesRegex(targeting.SelectionError, 'scope_ambiguous'):
            targeting.resolve(nodes, 'Active sheet', within='Export')
        nodes.pop()
        self.assertEqual((2, 'name'), targeting.resolve(nodes, 'Active sheet', within='Export'))

    def test_protected_disabled_and_malformed_hierarchy_fail_closed(self):
        nodes = form()
        nodes[2]['secure'] = True
        with self.assertRaisesRegex(targeting.SelectionError, 'target_protected'):
            targeting.resolve(nodes, 'Mode')
        nodes = form()
        nodes[5]['enabled'] = False
        with self.assertRaisesRegex(targeting.SelectionError, 'target_disabled'):
            targeting.resolve(nodes, 'Mode')
        nodes[5]['parent_path'] = nodes[5]['path']
        with self.assertRaisesRegex(targeting.SelectionError, 'invalid_tree'):
            targeting.resolve(nodes, 'Mode')


if __name__ == '__main__':
    unittest.main()
