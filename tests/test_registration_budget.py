"""Recorded identities avoid duplicate setup; unbound recipes still need discovery."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import recorded_plan
import test_python_workflow as runtime_fixture
from test_recorded_plan import recording
from test_semantic_runtime import task as query_recording


class RegistrationBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = recording()
        self.recipe = recorded_plan.compile_plan(self.source)['envelope']['recipe']

    def observed(self, recipe, *args, **kwargs):
        self.assertTrue(kwargs['read_only'])
        return dict(status='observed', input_dispatched=False, steps=[
            dict(number=n, receipt=dict(status='observed', actions=['SetValue', 'Click']))
            for n, _ in enumerate(recipe['steps'], 1)])

    def test_compiled_concrete_source_registers_with_zero_provider_passes(self):
        before = deepcopy(self.source)
        with patch.object(flow, 'run', side_effect=AssertionError('duplicate provider pass')) as provider:
            result = flow.register(self.recipe, self.folder / 'recorded', source_task=self.source)
        self.assertEqual(result['target_validation'], 'recorded_evidence')
        self.assertEqual(result['required_parameters'], ['Family'])
        self.assertFalse(result['input_dispatched'])
        saved = flow.load(self.folder / 'recorded/workflow.json')
        self.assertEqual([s['selector'] for s in saved['steps']], [s['selector'] for s in self.recipe['steps']])
        self.assertEqual(saved['steps'][0]['value'], 'N')
        self.assertEqual(saved['steps'][1]['parameter'], 'Family')
        self.assertEqual(json.loads((self.folder / 'recorded/source-task.json').read_text(encoding='utf8')), before)
        self.assertEqual(self.source, before)
        provider.assert_not_called()

    def test_recorded_caption_or_name_selector_keeps_zero_provider_registration(self):
        for name, step in [
                ('caption', dict(action='open', role='button', target='search', match='automation_id',
                                 expand=False, command_name='Search')),
                ('name', dict(action='open', role='button', target='Search', match='name', expand=False))]:
            source = dict(self.source, steps=[step])
            recipe = recorded_plan.compile_plan(source)['envelope']['recipe']
            destination = self.folder / name
            with self.subTest(name=name), patch.object(
                    flow, 'run', side_effect=AssertionError('duplicate provider pass')) as provider:
                result = flow.register(recipe, destination, source_task=source)
            self.assertEqual(result['target_validation'], 'recorded_evidence')
            self.assertEqual(flow.load(destination / 'workflow.json')['steps'][0]['command_name'], 'Search')
            provider.assert_not_called()

    def test_id_only_command_keeps_preflight_caption_and_blocks_renamed_button(self):
        source = dict(self.source, steps=[dict(action='open', role='button', target='search',
            match='automation_id', expand=False, label='Search')])
        recipe = recorded_plan.compile_plan(source)['envelope']['recipe']
        fixture = runtime_fixture.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.element.property.return_value = 'Search'
        destination = self.folder / 'id-only'
        with patch.object(flow, 'run', wraps=flow.run) as provider:
            result = flow.register(recipe, destination, source_task=source)
        self.assertEqual(result['status'], 'registered')
        provider.assert_called_once()
        self.assertTrue(provider.call_args.kwargs['read_only'])
        saved = flow.load(destination / 'workflow.json')
        self.assertEqual(saved['steps'][0]['command_name'], 'Search')
        fixture.element.property.return_value = 'Delete'
        with patch.object(flow.live, 'execute') as execute:
            result = flow.run(saved)
        self.assertEqual(result['diagnostic'], 'command_name_changed')
        self.assertFalse(result['input_dispatched'])
        execute.assert_not_called()

    def test_semantic_query_source_and_recipe_without_source_keep_one_preflight(self):
        query = query_recording()
        query_recipe = recorded_plan.compile_plan(query)['envelope']['recipe']
        for name, recipe, source in [('query', query_recipe, query), ('unproven', self.recipe, None)]:
            with self.subTest(name=name), patch.object(flow, 'run', side_effect=self.observed) as provider:
                result = flow.register(recipe, self.folder / name, source_task=source)
            self.assertEqual(result['status'], 'registered')
            provider.assert_called_once()
            self.assertTrue(provider.call_args.kwargs['read_only'])

    def test_different_action_value_selector_or_scope_never_uses_source_as_evidence(self):
        variants = []
        for key, value in [('value', 'other'), ('parameter', 'Other')]:
            changed = deepcopy(self.recipe)
            changed['steps'][0].pop('value', None)
            changed['steps'][0][key] = value
            variants.append((key, changed))
        changed = deepcopy(self.recipe)
        changed['steps'][0]['action'] = 'read-value'
        changed['steps'][0].pop('value')
        variants.append(('action', changed))
        changed = deepcopy(self.recipe)
        changed['steps'][0]['selector']['automation_id'] = 'other'
        variants.append(('target', changed))
        changed = deepcopy(self.recipe)
        changed['steps'][0]['selector']['ancestors'][0]['automation_id'] = 'other-panel'
        variants.append(('ancestor', changed))
        changed = deepcopy(self.recipe)
        changed['window'] = 'Other business window'
        variants.append(('window', changed))
        changed = deepcopy(self.recipe)
        changed['exe'] = 'Other.exe'
        variants.append(('exe', changed))
        for name, recipe in variants:
            with self.subTest(name=name), patch.object(flow, 'run') as provider:
                with self.assertRaisesRegex(flow.WorkflowError, '^recorded_workflow_mismatch$'):
                    flow.register(recipe, self.folder / name, source_task=self.source)
            provider.assert_not_called()
            self.assertFalse((self.folder / name).exists())

    def test_generic_result_and_pending_grounding_do_not_gain_new_offline_path(self):
        recipe = deepcopy(self.recipe)
        recipe['steps'].append(dict(action='double-click-result', selector={'role': 'table'}, result_row='single'))
        self.assertFalse(flow._literal_recorded_targets(recipe))
        recipe = deepcopy(self.recipe)
        recipe['steps'][0]['grounding_text'] = 'Find the live field'
        self.assertFalse(flow._literal_recorded_targets(recipe))


if __name__ == '__main__':
    unittest.main()
