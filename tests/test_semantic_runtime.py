"""Unidentified locations resolve current descriptions with direct-action identity guards."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import python_workflow as flow
import recorded_plan


def task():
    raw = dict(action='manual', target=dict(kind='manual', locator={}),
               recognition=dict(action='manual', result='manual', reason='capture_target_unavailable'))
    raw['instruction'] = app.tasks.compile_instruction(raw, 'MES Setup 조건을 N으로 해줘')
    return app.tasks.from_recording('Recovered condition', 'Example.exe', 'Example window', [raw])


class QueryContractTests(unittest.TestCase):
    def test_compilation_preserves_description_without_claiming_observed_label(self):
        source = task()
        compiled = recorded_plan.compile_plan(source)['envelope']['recipe']
        step = compiled['steps'][0]
        self.assertEqual(step['selector'], dict(query='MES Setup', role='combobox'))
        self.assertEqual(step['value'], 'N')
        self.assertNotIn('field_label', step)
        self.assertEqual(source['steps'][0]['recorded_effect'], 'unobserved')

    def test_query_cannot_smuggle_old_identity_or_coordinates(self):
        good = dict(query='MES Setup', role='combobox')
        flow.validate_selector(good)
        for update in ({'automation_id': 'old-id'}, {'x': 12}, {'query': ''},
                       {'within': ''}, {'role': 'invented'}, {'ancestors': []}):
            with self.subTest(update=update), self.assertRaises(flow.WorkflowError):
                flow.validate_selector(dict(good, **update))


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.uia, self.root, self.element = (MagicMock() for _ in range(4))
        self.uia.same.return_value = True
        self.nodes = [dict(role='window'), dict(role='combobox', name='', automation_id='current-id')]
        self.selector = dict(query='MES Setup', role='combobox')
        self.stable = dict(automation_id='current-id', role='combobox')
        for target, kwargs in (
            ('app.observe', dict(return_value=(self.nodes, [self.root, self.element]))),
            ('app.resolve', dict(return_value=(1, 'label'))),
            ('app.field_labels', dict(return_value={1: dict(semantic_label='MES Setup', label_source='labeled_by')})),
            ('app._stable_selector', dict(return_value=self.stable)),
            ('lookup.ancestry', dict(return_value=[])),
            ('lookup.resolve', dict(return_value=self.element))):
            p = patch('python_workflow.' + target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_same_direct_resolver_and_current_identity_only(self):
        element, stable, proof = flow.semantic_match(self.binding, self.uia, self.root, self.selector)
        self.assertIs(element, self.element)
        self.assertEqual(stable, self.stable)
        self.assertEqual(proof['automation_id'], 'current-id')
        flow.app.resolve.assert_called_once_with(self.nodes, 'MES Setup', role='combobox', within=None)
        flow.lookup.ancestry.assert_called_once_with(self.uia, self.element, self.root, pid=self.binding.pid)

    def test_revalidation_uses_current_native_identity_without_scanning_again(self):
        proof = dict(semantic_label='MES Setup')
        with patch.object(flow.app, 'field_label', return_value=proof):
            found = flow.revalidate_step(self.binding, self.uia, self.root, self.selector,
                                         self.stable, proof, self.element)
        self.assertIs(found, self.element)
        flow.app.observe.assert_not_called()

    def test_no_id_reobserves_and_requires_same_element(self):
        self.uia.same.side_effect = [True, True, False]
        with self.assertRaisesRegex(flow.WorkflowError, 'target_changed'):
            flow.revalidate_step(self.binding, self.uia, self.root, self.selector, None, {}, self.element)
        flow.app.observe.assert_called_once()

    def test_command_caption_change_stops_before_input(self):
        self.element.property.return_value = 'Delete'
        with self.assertRaisesRegex(flow.WorkflowError, 'command_name_changed'):
            flow.revalidate_step(self.binding, self.uia, self.root, dict(query='Search', role='button'),
                                 dict(automation_id='current-id', role='button'), dict(name='Search'), self.element)

    def test_registration_promotes_live_id_and_keeps_original_description(self):
        source = task()
        recipe = recorded_plan.compile_plan(source)['envelope']['recipe']
        observed = dict(status='observed', input_dispatched=False, steps=[dict(number=1, receipt=dict(status='observed',
            actions=['SetValue'], resolved_selector=self.stable,
            target_resolution=dict(semantic_label='MES Setup')))])
        with tempfile.TemporaryDirectory() as folder, patch.object(flow, 'run', return_value=observed):
            destination = Path(folder) / 'registered-query'
            result = flow.register(recipe, destination, source_task=source)
            self.assertEqual(result['status'], 'registered')
            registered = flow.load(destination / 'workflow.json')
            self.assertEqual(registered['steps'][0]['selector'], self.stable)
            self.assertEqual(registered['steps'][0]['field_label'], 'MES Setup')
            self.assertEqual(recipe['steps'][0]['selector'], self.selector)
            self.assertIn('MES Setup 조건을 N으로 해줘', (destination / 'source-task.json').read_text(encoding='utf8'))


if __name__ == '__main__':
    unittest.main()
