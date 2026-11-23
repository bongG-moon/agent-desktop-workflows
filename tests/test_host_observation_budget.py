"""Host replies stay small without losing task intent or reporting unproved success."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import learned_tasks as tasks
import python_workflow as flow


class HostObservationBudgetTests(unittest.TestCase):
    def test_nested_visual_payloads_stay_local_without_mutating_the_saved_task(self):
        visual = dict(size=[41, 41], offset=[20, 20], rgb='A' * 6724)
        source = dict(schema=1, name='Example', exe='Example.exe', window='Example', steps=[
            dict(action='conditional-open', label='Close notice then open item', seconds=1,
                 when_present=dict(name='Notice', visual=visual),
                 dismiss=dict(name='Close', visual=visual), always=dict(name='Item', visual=visual)),
            dict(action='row-open', label='Open named row', row_target=dict(
                version=1, role='listitem', row_id='row-2', scope=['Results'], size=[300, 50],
                boxes=dict(key=[4, 4, 40, 20], button=[200, 4, 30, 20]), key='B' * 4096)),
            dict(action='visual-click', label='Open item', visual=visual),
            dict(action='fill', target='Search', role='textfield', instruction=dict(
                mode='required', text='Use requested ID', parameter='Equipment'))])
        before = deepcopy(source)
        result = tasks.summary(source)
        encoded = json.dumps(result)
        self.assertNotIn('A' * 64, encoded)
        self.assertNotIn('B' * 64, encoded)
        self.assertNotIn('"rgb"', encoded)
        for key in ('when_present', 'dismiss', 'always'):
            self.assertEqual(result['steps'][0][key], dict(name=source['steps'][0][key]['name'], has_visual_hint=True))
        row = result['steps'][1]['row_target']
        self.assertEqual(row['row_id'], 'row-2')
        self.assertEqual(row['boxes'], source['steps'][1]['row_target']['boxes'])
        self.assertTrue(row['has_visual_hint'])
        self.assertNotIn('key', row)
        self.assertTrue(result['steps'][2]['has_visual_hint'])
        self.assertEqual(result['steps'][3]['instruction'], source['steps'][3]['instruction'])
        self.assertLess(len(encoded), len(json.dumps(source)) * .06)
        self.assertEqual(source, before)

    def test_finished_summary_requests_reporting_without_promoting_delivery_to_verified(self):
        entries = [(1, dict(action='invoke', label='Open')),
                   (2, dict(action='read-value', label='Comment'))]
        report = dict(status='completed_unverified', input_dispatched=True, steps=[
            dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True,
                 diagnostic='business_result_not_verified')),
            dict(number=2, receipt=dict(status='observed', input_dispatched=False, value='Current comment',
                 value_evidence=dict(status='observed')))])
        before = deepcopy(report)
        summary = flow.summarize_execution(report, entries)
        self.assertEqual(summary['host_action'], 'report_execution_receipt')
        self.assertEqual(summary['status'], 'completed_unverified')
        self.assertEqual(summary['steps'][0]['status'], 'delivered_unverified')
        self.assertEqual(summary['steps'][1]['value'], 'Current comment')
        self.assertEqual(report, before)
        for state in ('blocked', 'uncertain', 'needs_scene'):
            interrupted = dict(report, status=state, stopped_at=2, host_action='inspect_current_result',
                               image='current.png', frame='current-frame', resume_args=['resume'])
            self.assertIs(flow.summarize_execution(interrupted, entries), interrupted)
        self.assertEqual(flow.summarize_execution(dict(report, host_action='specific_protocol'), entries)['host_action'],
                         'specific_protocol')


if __name__ == '__main__':
    unittest.main()
