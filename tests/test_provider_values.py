"""Provider-value compatibility preserves selection proof and privacy boundaries."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'skills/desktop-select/scripts'), str(ROOT / 'scripts')]
import provider_values
import win_bridge as win


class Element:
    def __init__(self, role, parent=None, **props):
        self.props = {30002: 42, 30003: role, 30019: False, **{int(k): v for k, v in props.items()}}
        self.parent, self.children, self.reads = parent, [], []
        if parent:
            parent.children.append(self)

    def property(self, key):
        self.reads.append(key)
        return self.props.get(key)

    def element_property(self, key):
        return self.props.get(key)


class ProviderValueTests(unittest.TestCase):
    def setUp(self):
        self.combo = Element(50003, **{'30043': True, '30045': ''})
        self.uia = object.__new__(win.UIA)
        self.uia.children = lambda e: iter(e.children)
        self.uia.parent = lambda e: e.parent
        self.uia.same = lambda a, b: a is not None and a is b

    def edit(self, value, parent=None, **props):
        return Element(50004, parent or self.combo, **{'30043': True, '30045': value, **props})

    def option(self, value='wanted', **props):
        return Element(50007, self.combo, **{'30036': True, '30079': True,
                       '30043': True, '30045': value, **props})

    def observe(self):
        return provider_values.value_observation(self.uia, self.combo)

    def test_duplicate_corresponding_editor_values_are_corroborated(self):
        for _ in range(3):
            self.edit('Parcel')
        self.assertEqual(self.observe(), {'value': 'Parcel',
                         'value_evidence': {'status': 'observed', 'source': 'corroborated'}})
        # The pinned bridge retains its separate original contract.
        self.assertEqual(win.UIA.value_observation(self.uia, self.combo)['value_evidence']['status'], 'conflict')

    def test_empty_peers_are_not_selection_evidence(self):
        self.edit('')
        self.edit(None)
        self.assertEqual(self.observe()['value_evidence']['status'], 'unavailable')
        self.edit('N')
        self.assertEqual(self.observe()['value'], 'N')

    def test_conflicting_editor_parent_or_selected_values_remain_conflict(self):
        self.edit('N')
        other = self.edit('W')
        self.assertEqual(self.observe()['value_evidence']['status'], 'conflict')
        other.props[30045] = 'N'
        self.combo.props[30045] = 'P'
        self.assertEqual(self.observe()['value_evidence']['status'], 'conflict')
        self.combo.props[30045] = ''
        self.option('C')
        self.assertEqual(self.observe()['value_evidence']['status'], 'conflict')

    def test_raw_valuepattern_msaa_disagreement_is_never_overridden(self):
        self.edit('N')
        self.edit('N', **{'30090': True, '30093': 'P'})
        self.assertEqual(self.observe()['value_evidence']['status'], 'conflict')

    def test_two_distinct_selections_conflict_even_with_same_value(self):
        self.option('N')
        self.option('N')
        self.assertEqual(self.observe()['value_evidence']['status'], 'conflict')

    def test_same_selected_peer_enumerated_twice_is_one_selection(self):
        row = self.option('N')
        self.combo.props[30037] = True
        self.combo.selected_items = lambda: [row, row]
        self.assertEqual(self.observe(), {'value': 'N',
                         'value_evidence': {'status': 'observed', 'source': 'selection'}})

    def test_unrelated_selected_owner_is_rejected_before_value_read(self):
        other = Element(50003)
        row = self.option('secret', **{'30080': other})
        self.assertEqual(self.observe()['value_evidence']['status'], 'unavailable')
        self.assertNotIn(30045, row.reads)

    def test_detached_selected_popup_requires_explicit_matching_owner(self):
        desktop = Element(50033, **{'30002': 0, '30019': None})
        popup = Element(50032, desktop)
        row = Element(50007, popup, **{'30036': True, '30079': True, '30043': True,
                                      '30045': 'N', '30080': self.combo})
        self.combo.props[30037] = True
        self.combo.selected_items = lambda: [row]
        self.assertEqual(self.observe()['value'], 'N')
        row.props[30080] = None
        self.assertEqual(self.observe()['value_evidence']['status'], 'unavailable')

    def test_matching_explicit_owner_cannot_override_another_combo_ancestor(self):
        other = Element(50003)
        row = Element(50007, other, **{'30036': True, '30079': True, '30043': True,
                                      '30045': 'private', '30080': self.combo})
        self.combo.props[30037] = True
        self.combo.selected_items = lambda: [row]
        self.assertEqual(self.observe()['value_evidence']['status'], 'unavailable')
        self.assertNotIn(30045, row.reads)

    def test_protected_or_foreign_editor_values_are_not_read(self):
        for props in ({'30019': True}, {'30019': None}, {'30002': 99}):
            with self.subTest(props=props):
                self.combo.children.clear()
                self.edit('N')
                other = self.edit('secret', **props)
                self.assertEqual(self.observe()['value_evidence']['status'], 'unavailable')
                self.assertNotIn(30045, other.reads)

    def test_nested_combo_and_unselected_option_do_not_contribute_editor_values(self):
        self.edit('N')
        nested = Element(50003, self.combo)
        self.edit('wrong', nested)
        row = self.option('wrong', **{'30079': False})
        self.edit('wrong', row)
        self.assertEqual(self.observe()['value'], 'N')

    def test_partial_tree_does_not_turn_partial_agreement_into_success(self):
        for _ in range(257):
            self.edit('N')
        self.assertEqual(self.observe()['value_evidence']['status'], 'unavailable')

    def test_ordinary_edit_empty_value_retains_bridge_contract(self):
        editor = self.edit('')
        self.assertEqual(provider_values.value_observation(self.uia, editor), {'value': ''})


if __name__ == '__main__':
    unittest.main()
