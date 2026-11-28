import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import request_timing as timing


def event(kind, **fields):
    return {'timestamp': '1970-01-01T00:00:00.001Z', 'type': 'event_msg',
            'payload': dict(type=kind, turn_id='t', **fields)}


def item(kind, start, end, **fields):
    return event('item_completed', started_at_ms=start, completed_at_ms=end, item=dict(type=kind, **fields))


def host_call(kind, ms, **fields):
    return dict(timestamp=f'1970-01-01T00:00:00.{ms:03d}Z', type='response_item',
                payload=dict(type=kind, call_id='call_1', **fields))


class RequestTiming(unittest.TestCase):
    def test_nested_intervals_reconcile_to_user_message_and_answer_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'cmd-1').mkdir()
            (root/'cmd-1'/'worker.json').write_text(json.dumps(dict(phase='runner', start_ms=25, end_ms=70,
                    spans=[['step_3', 34, 41]], run_id='a'*32)), encoding='ascii')
            events = [event('task_started'), item('UserMessage', 10, 10),
                      host_call('custom_tool_call', 19), item('CommandExecution', 20, 80, id='cmd-1'),
                      host_call('custom_tool_call_output', 83, output=json.dumps(dict(run_id='a'*32))),
                      item('AgentMessage', 94, 100, phase='final_answer'),
                      event('task_complete')]
            path = root/'events'
            path.write_text('\n'.join(map(json.dumps, events)), encoding='ascii')
            report = timing.turn_report(path, root)
            self.assertEqual('measured', report['status'])
            self.assertEqual('host_user_message', report['start_source'])
            self.assertEqual(90, sum(row['elapsed_ms'] for row in report['intervals']))
            self.assertEqual((10, 100, None), (report['request_received_ms'], report['answer_ready_ms'],
                                               report['response_delivered_ms']))
            self.assertEqual('step_3/aaaaaaaa', report['intervals'][3]['phase'])
            self.assertEqual(80, report['runtime_calls'][0]['tool_result_available_ms'])
            self.assertEqual(19, report['runtime_calls'][0]['skill_invoked_ms'])
            self.assertEqual(83, report['runtime_calls'][0]['host_received_result_ms'])
            self.assertEqual(['a'*32], report['runtime_calls'][0]['run_ids'])
            self.assertEqual('partial', timing.turn_report(path, root/'missing')['status'])

    def test_overlapping_calls_count_each_millisecond_once(self):
        rows = timing.partition(10, 60, [(1, 'tool', 1, 100), (5, 'step_1/a', 20, 50),
                                         (5, 'step_1/b', 40, 58)])
        self.assertEqual(50, sum(i['elapsed_ms'] for i in rows))
        self.assertIn('step_1/a + step_1/b', [i['phase'] for i in rows])

    def test_timing_write_failure_does_not_modify_execution_and_no_values_are_stored(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'DESKTOP_SELECT_TASK_DIR': str(Path(tmp)/'tasks'), 'CODEX_TOOL_CALL_ID': 'cmd-1',
                'CODEX_THREAD_ID': '00000000-0000-0000-0000-000000000001'}):
            def run():
                timing.receipt(dict(status='verified', run_id='b'*32, value='private business value'))
                return 3
            self.assertEqual(3, timing.invoke(run, ['run', '--set', 'ID=secret']))
            raw = next(Path(tmp).rglob('*.json')).read_text(encoding='ascii')
            self.assertNotIn('secret', raw)
            self.assertNotIn('private', raw)
            self.assertEqual('b'*32, json.loads(raw)['run_id'])
            with patch.object(timing, 'trace_dir', side_effect=OSError):
                self.assertEqual(3, timing.invoke(run, ['run']))
