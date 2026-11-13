"""Literal registered workflows preserve parameters and stop after uncertain input."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow


def recipe():
    return dict(schema=1, name='Example workflow', exe='Example.exe', window='Example window', steps=[
        dict(action='set-value', selector=dict(automation_id='status', role='combobox'), value='N'),
        dict(action='set-value', selector=dict(automation_id='family', role='combobox'), parameter='Family'),
        dict(action='invoke', selector=dict(automation_id='search', role='button')),
        dict(action='wait', seconds=0),
        dict(action='invoke', selector=dict(automation_id='export', role='button'))])


class RecipeTests(unittest.TestCase):
    def test_required_parameter_has_no_recorded_default(self):
        result = flow.run(recipe(), {})
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['missing_parameters'], ['Family'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['steps'], [])

    def test_no_implicit_default_or_arbitrary_code(self):
        for update in ({'default': 'Hybrid'}, {'value': 'Hybrid'}, {'action': 'shell'}):
            data = recipe()
            data['steps'][1].update(update)
            with self.subTest(update=update), self.assertRaises((flow.WorkflowError, ValueError)):
                flow.validate(data)

    def test_invalid_wait_and_unknown_values(self):
        for seconds in (-1, True, float('nan'), 121):
            data = recipe()
            data['steps'][3]['seconds'] = seconds
            with self.subTest(seconds=seconds), self.assertRaises(flow.WorkflowError):
                flow.validate(data)
        with self.assertRaisesRegex(flow.WorkflowError, 'unknown_parameter'):
            flow.values_for(recipe(), {'Unrelated': 'N'})

    def test_case_insensitive_parameter_but_conflicting_keys_fail(self):
        self.assertEqual(flow.values_for(recipe(), {'family': 'Hybrid'}), ({'family': 'Hybrid'}, []))
        with self.assertRaisesRegex(flow.WorkflowError, 'invalid_override'):
            flow.values_for(recipe(), {'family': 'Hybrid', 'Family': 'DRAM'})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.uia = MagicMock()
        self.uia.__enter__.return_value = self.uia
        self.uia.same.return_value = True
        self.uia.actions.return_value = ['SetValue', 'Click']
        self.binding = MagicMock()
        self.element = MagicMock()
        self.element.property.return_value = ''
        for name, kwargs in (
            ('bind', dict(return_value=self.binding)),
            ('app.bridge.UIA', dict(return_value=self.uia)),
            ('app.guard', dict(return_value=None)),
            ('app.visibility.restore_for_action', dict(return_value=True)),
            ('lookup.resolve', dict(return_value=self.element)),
            ('wait', dict(return_value=None))):
            p = patch('python_workflow.' + name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def execute(self, *args, **kwargs):
        kwargs['still_valid']()
        if args[4] == 'select':
            return dict(status='verified', input_dispatched=True)
        return dict(status='delivered_unverified', diagnostic='business_result_not_verified', input_dispatched=True)

    def test_single_session_literal_values_and_explicit_wait(self):
        with patch.object(flow.live, 'execute', side_effect=self.execute) as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid'})
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual([c.args[5] for c in execute.call_args_list], ['N', 'Hybrid', None, None])
        self.assertEqual(flow.app.bridge.UIA.call_count, 1)
        self.assertEqual([s['receipt']['status'] for s in result['steps']],
                         ['verified', 'verified', 'delivered_unverified', 'waited', 'delivered_unverified'])

    def test_uncertain_combo_never_invokes_next_step_or_retries(self):
        with patch.object(flow.live, 'execute', return_value=dict(status='uncertain', input_dispatched=None)) as execute:
            result = flow.run(recipe(), {'Family': 'Hybrid'})
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(result['stopped_at'], 1)
        self.assertIsNone(result['input_dispatched'])
        execute.assert_called_once()

    def test_registered_command_caption_change_blocks_reused_id(self):
        data = recipe()
        data['steps'] = [dict(action='invoke', selector=dict(automation_id='search', role='button'),
                              command_name='Search')]
        self.element.property.return_value = 'Delete'
        with patch.object(flow.live, 'execute') as execute:
            result = flow.run(data)
        self.assertEqual(result['diagnostic'], 'command_name_changed')
        self.assertFalse(result['input_dispatched'])
        execute.assert_not_called()

    def test_failed_search_does_not_wait_or_export(self):
        receipts = [dict(status='verified', input_dispatched=True)] * 2
        receipts.append(dict(status='blocked', input_dispatched=False, diagnostic='target_disabled'))
        with patch.object(flow.live, 'execute', side_effect=receipts) as execute:
            result = flow.run(recipe(), {'Family': 'DRAM'})
        self.assertEqual(result['stopped_at'], 3)
        self.assertEqual(execute.call_count, 3)
        flow.wait.assert_not_called()

    def test_readonly_neither_restore_nor_dispatch(self):
        with patch.object(flow.live, 'execute') as execute, patch.object(flow.live, 'combo_readback', return_value={}):
            result = flow.run(recipe(), read_only=True)
        self.assertEqual(result['status'], 'observed')
        self.assertFalse(result['input_dispatched'])
        flow.app.visibility.restore_for_action.assert_not_called()
        execute.assert_not_called()
        flow.wait.assert_not_called()

    def test_register_creates_only_three_text_files_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'query-skill'
            with patch.object(flow.live, 'combo_readback', return_value={}):
                result = flow.register(recipe(), destination)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual(sorted(str(p.relative_to(destination)).replace('\\', '/')
                                    for p in destination.rglob('*') if p.is_file()),
                             ['SKILL.md', 'scripts/run.py', 'workflow.json'])
            self.assertEqual(flow.load(destination / 'workflow.json'), recipe())
            compile((destination / 'scripts/run.py').read_text(encoding='utf8'), '<runner>', 'exec')
            with self.assertRaisesRegex(flow.WorkflowError, 'workflow_destination_exists'):
                flow.register(recipe(), destination)


class BoundaryTests(unittest.TestCase):
    def test_worker_timeout_never_means_safe_to_retry(self):
        for read_only in (True, False):
            with (self.subTest(read_only=read_only), patch.object(flow, 'load', return_value=recipe()), patch.object(
                flow.subprocess, 'run', side_effect=subprocess.TimeoutExpired('workflow', 90)) as launched,
                redirect_stdout(StringIO()) as output):
                flow.main(['run', '--recipe', 'example.json', '--set', 'Family=Hybrid', *(['--read-only'] if read_only else [])])
                result = json.loads(output.getvalue())
            launched.assert_called_once()
            self.assertEqual(result['status'], 'blocked' if read_only else 'uncertain')
            self.assertIs(result['input_dispatched'], False if read_only else None)


if __name__ == '__main__':
    unittest.main()
