"""Semantic registration never persists native handle-shaped AutomationIds."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import python_workflow as flow


def nodes(identifier='68754', name='Filter'):
    return [
        dict(path='0', parent_path='', secure=False, role='window', automation_id='', name='Demo'),
        dict(path='0/0', parent_path='0', secure=False, role='group', automation_id='70001', name='devices'),
        dict(path='0/0/0', parent_path='0/0', secure=False, role='textfield', automation_id=identifier, name=name),
    ]


class StableRuntimeIdentityTests(unittest.TestCase):
    def test_decimal_target_and_ancestor_ids_become_observed_names(self):
        observed = nodes()
        before = deepcopy(observed)
        selector = app._stable_selector(observed, 2)
        self.assertEqual(selector, dict(name='Filter', role='textfield', ancestors=[
            dict(role='group', name='devices'), dict(role='window', name='Demo')]))
        self.assertEqual(observed, before)
        changed = nodes('90001')
        changed[1]['automation_id'] = '90002'
        self.assertEqual(app._stable_selector(changed, 2), selector)

    def test_descriptive_ids_and_ancestor_scope_are_retained(self):
        observed = nodes('gcboStatus', '')
        observed[2]['role'] = 'combobox'
        observed[1]['automation_id'] = 'conditionPanel'
        self.assertEqual(app._stable_selector(observed, 2), dict(automation_id='gcboStatus', role='combobox',
            ancestors=[dict(role='group', automation_id='conditionPanel', name='devices'),
                       dict(role='window', name='Demo')]))

    def test_unnamed_numeric_target_retains_semantic_query(self):
        for identifier in ('68754', '', None):
            with self.subTest(identifier=identifier):
                self.assertIsNone(app._stable_selector(nodes(identifier, ''), 2))

    def test_duplicate_caption_does_not_get_promoted(self):
        observed = nodes()
        observed.append(dict(observed[2], path='0/0/1', automation_id='68755'))
        self.assertIsNone(app._stable_selector(observed, 2))

    def test_handle_only_ancestor_is_omitted_but_protected_ancestor_blocks(self):
        observed = nodes()
        observed[1]['name'] = ''
        self.assertEqual(app._stable_selector(observed, 2)['ancestors'], [dict(role='window', name='Demo')])
        observed[1]['secure'] = True
        self.assertIsNone(app._stable_selector(observed, 2))

    def test_name_promotion_is_checked_against_the_live_element(self):
        binding, uia, root, element = Mock(), Mock(), Mock(), Mock()
        uia.same.side_effect = lambda left, right: left is right
        with patch.object(app, 'observe', return_value=(nodes(), [root, Mock(), element])), \
             patch.object(app, 'resolve', return_value=(2, 'name')), \
             patch.object(app, 'field_labels', return_value={}), \
             patch.object(flow.lookup, 'ancestry'), \
             patch.object(flow.lookup, 'resolve', return_value=element) as resolve:
            found, selector, _ = flow.semantic_match(binding, uia, root, dict(query='Filter', role='textfield'))
            self.assertIs(found, element)
            self.assertEqual(selector['name'], 'Filter')
            self.assertNotIn('automation_id', selector)
            resolve.assert_called_once_with(binding, uia, selector, root=root)
            resolve.return_value = Mock()
            with self.assertRaisesRegex(flow.WorkflowError, 'target_changed'):
                flow.semantic_match(binding, uia, root, dict(query='Filter', role='textfield'))


if __name__ == '__main__':
    unittest.main()
