"""A successful interpretation is reusable without freezing runtime parameters."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import confirmed_plan as confirmed
import intent_tasks as intent
import learned_tasks as tasks


class ConfirmedPlanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        step = intent.make_step(dict(role='combobox', automation_id='conditionFamily'),
                                '입력받은 {Family} 값을 이 항목에 넣어줘')
        self.plan = tasks.from_recording('Conditions', 'Example.exe', 'Business', [step])
        self.resolution = dict(schema=1, plan_digest=intent.plan_digest(self.plan), steps=[dict(number=1, actions=[
            dict(action='select', role='combobox', target='conditionFamily', parameter='Family')])])
        self.calls = []

    def invoke(self, action):
        self.calls.append(deepcopy(action))
        return dict(status='verified', effect_verification='matched', input_dispatched=True)

    def run_plan(self, value='Hybrid', **kwargs):
        return confirmed.replay(self.plan, 'Example.exe', 'Business', {'Family': value}, self.invoke,
                                directory=self.folder.name, **kwargs)

    def test_first_run_needs_interpretation_then_reuses_parameterized_success(self):
        self.assertEqual(self.run_plan()['status'], 'needs_interpretation')
        self.assertEqual(self.calls, [])
        result = self.run_plan(resolution=self.resolution)
        self.assertEqual(result['plan_reuse'], 'saved')
        saved = confirmed.load(self.plan, self.folder.name)
        self.assertEqual(saved, self.resolution)
        self.assertNotIn('Hybrid', json.dumps(saved))
        self.assertEqual(self.run_plan('DRAM')['resolution_source'], 'confirmed')
        self.assertEqual([a['operation']['value'] for a in self.calls], ['Hybrid', 'DRAM'])
        self.assertEqual(saved['steps'][0]['actions'][0]['parameter'], 'Family')

    def test_missing_runtime_value_still_stops_before_any_dispatch(self):
        confirmed.save(self.plan, self.resolution, self.folder.name)
        result = confirmed.replay(self.plan, 'Example.exe', 'Business', {}, self.invoke, directory=self.folder.name)
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['missing_parameters'], ['Family'])
        self.assertEqual(self.calls, [])

    def test_each_instruction_anchor_and_app_edit_invalidates_prior_plan(self):
        confirmed.save(self.plan, self.resolution, self.folder.name)
        for field in ('instruction', 'anchor', 'window'):
            changed = deepcopy(self.plan)
            if field == 'instruction':
                changed['steps'][0]['instruction']['text'] += ' 그리고 새 조건을 확인해줘'
            elif field == 'anchor':
                changed['steps'][0]['anchor']['automation_id'] = 'otherField'
            else:
                changed['window'] = 'Another business screen'
            with self.subTest(field=field):
                self.assertIsNone(confirmed.load(changed, self.folder.name))
        self.assertEqual(confirmed.load(self.plan, self.folder.name), self.resolution)

    def test_corrupt_or_wrong_digest_cache_requests_interpretation_without_input(self):
        for data in ('not json', json.dumps(dict(self.resolution, plan_digest='stale')),
                     '{"schema":1,"schema":2}'):
            confirmed.save(self.plan, self.resolution, self.folder.name)
            confirmed._path(self.plan, self.folder.name).write_text(data, encoding='utf8')
            result = self.run_plan()
            self.assertEqual(result['status'], 'needs_interpretation')
            self.assertEqual(result['plan_reuse'], 'confirmed_plan_unavailable')
            self.assertEqual(self.calls, [])

    def test_failed_uncertain_and_partial_execution_do_not_publish_confirmed_plan(self):
        for status, sent in (('blocked', False), ('uncertain', None), ('mismatch', True)):
            with patch.object(self, 'invoke', return_value=dict(status=status, input_dispatched=sent)):
                result = self.run_plan(resolution=self.resolution)
            self.assertEqual(result['status'], status)
            self.assertIsNone(confirmed.load(self.plan, self.folder.name))
        self.run_plan(resolution=self.resolution, only='1')
        self.assertIsNone(confirmed.load(self.plan, self.folder.name))

    def test_cache_io_failure_does_not_turn_success_into_retryable_failure(self):
        with patch.object(confirmed, 'save', side_effect=OSError('denied')):
            result = self.run_plan(resolution=self.resolution)
        self.assertEqual((result['status'], result['input_dispatched']), ('verified', True))
        self.assertEqual(result['plan_reuse'], 'save_unavailable')
        self.assertEqual(len(self.calls), 1)

    def test_reused_plan_still_invokes_live_resolution_and_stops_on_failure(self):
        self.run_plan(resolution=self.resolution)
        self.calls.clear()
        with patch.object(self, 'invoke', side_effect=RuntimeError('window disappeared')):
            result = self.run_plan('DRAM')
        self.assertEqual((result['status'], result['input_dispatched']), ('uncertain', None))
        self.assertEqual(result['resolution_source'], 'confirmed')

    def test_cached_task_does_not_authorize_another_business_window(self):
        confirmed.save(self.plan, self.resolution, self.folder.name)
        with self.assertRaises(tasks.TaskError):
            confirmed.replay(self.plan, 'Example.exe', 'Other', {'Family': 'DRAM'}, self.invoke,
                             directory=self.folder.name)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
