"""Recorder data/security checks; no desktop hooks or input dispatch."""
import copy
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import recorder
from storage import validate_workflow


def observation(role='combobox', actions=None, element='field', window=200):
    return {'element': element, 'root': 'root', 'window': window,
            'node': {'name': 'Family', 'automation_id': 'Family', 'role': role,
                     'available_actions': ['SetValue'] if actions is None else actions,
                     'locator': {'automation_id': 'Family', 'role': role},
                     'bounds': {'x': 100, 'y': 150, 'width': 200, 'height': 100}}}


class FakeUIA:
    def __init__(self):
        self.values = {'field': 'Initial'}
        self.protected = set()
        self.focused_element = 'field'
        self.checked_value = True

    def secure(self, element):
        return element in self.protected

    def same(self, first, second):
        return first == second

    def value(self, element):
        if self.secure(element):
            raise AssertionError('protected value must not be read')
        return self.values.get(element)

    def checked(self, element):
        return self.checked_value

    def focused(self):
        return self.focused_element

    def from_handle(self, hwnd):
        return 'root' if hwnd == 200 else 'child'

    def within(self, element, root):
        return element in {'field', 'child'} and root == 'root'

    def node(self, element, root):
        return observation(element=element)['node']


class RecorderTests(unittest.TestCase):
    def setUp(self):
        directory = ROOT / '.build'
        directory.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.request = {'output_dir': str(self.output), 'progress_path': str(self.output / 'progress.json'),
                        'draft_path': str(self.output / 'draft.json'),
                        'stop_path': str(self.output / 'stop'), 'name': 'Synthetic recording'}
        self.info = {'exe_name': 'Fixture.exe', 'title': 'Synthetic fixture', 'class_name': 'FixtureWindow'}
        self.model = recorder.RecordingModel(self.request, self.info)
        self.binding = Mock(pid=100, hwnd=200)
        self.binding.contains_window.return_value = True
        self.bridge = Mock()
        self.bridge.root_window.return_value = 200
        self.bridge.foreground.return_value = 200
        self.bridge.window_text.return_value = 'Popup'
        self.bridge.window_class.return_value = 'PopupWindow'
        self.worker = recorder.CaptureWorker(self.binding, self.bridge, self.model, queue.Queue(), queue.Queue())
        self.worker.uia = FakeUIA()

    def recorded_events(self):
        return [json.loads(line) for line in self.model.events_path.read_text(encoding='utf-8').splitlines()]

    def test_value_revisions_keep_original_events_and_one_stable_step(self):
        self.worker.watch(observation())
        self.worker.uia.values['field'] = 'Hybrid'
        self.worker.finalize_watch(False)
        first_id = self.model.steps[0]['id']
        self.worker.uia.values['field'] = 'Final'
        self.worker.finalize_watch(True)
        self.assertEqual(len(self.model.steps), 1)
        self.assertEqual(self.model.steps[0]['id'], first_id)
        self.assertEqual(self.model.steps[0]['expect'], {'kind': 'value', 'value': 'Final'})
        events = self.recorded_events()
        self.assertEqual([event['kind'] for event in events], ['step_recorded', 'step_revision'])
        self.assertEqual([event['step']['value'] for event in events], ['Hybrid', 'Final'])
        before = self.model.events_path.read_bytes()
        result = self.model.finish()
        draft = json.loads(self.model.draft.read_text(encoding='utf-8'))
        validate_workflow(draft)
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(draft['revision'], 1)
        self.assertEqual(draft['steps'][0]['id'], first_id)
        self.assertEqual(self.model.events_path.read_bytes(), before)

    def test_protected_input_never_reads_or_records_value(self):
        self.worker.uia.values['field'] = 'do-not-persist-secret'
        self.worker.uia.protected.add('field')
        self.worker.watch(observation())
        self.model.finish()
        all_data = self.model.events_path.read_text(encoding='utf-8') + self.model.draft.read_text(encoding='utf-8')
        self.assertNotIn('do-not-persist-secret', all_data)
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'protected_input_not_recorded')

    def test_value_becoming_protected_stops_watcher_before_read(self):
        self.worker.watch(observation())
        self.worker.uia.values['field'] = 'new-secret'
        self.worker.uia.protected.add('field')
        self.worker.finalize_watch(True)
        self.assertIsNone(self.worker.watched)
        self.assertNotIn('new-secret', self.model.events_path.read_text(encoding='utf-8'))

    def test_region_requires_real_bounds_and_stable_locator(self):
        obs = observation(role='group', actions=[])
        self.worker.capture_action({'action': 'right-click', 'x': 150, 'y': 175}, obs)
        step = self.model.steps[0]
        self.assertEqual(step['target']['point'], {'x_ratio': .25, 'y_ratio': .25})
        self.assertTrue(step['headed'])
        self.assertEqual(step['recognition']['action'], 'region_review_required')
        self.assertNotIn('x', step['target'])
        self.model.finish()
        validate_workflow(json.loads(self.model.draft.read_text(encoding='utf-8')))
        for point in ((100, 150), (350, 175), (float('nan'), 175)):
            with self.assertRaises(recorder.RecorderError):
                self.worker.target(obs, 'region', point)
        obs['node']['locator'] = {'role': 'group'}
        with self.assertRaises(recorder.RecorderError):
            self.worker.target(obs, 'region', (150, 175))

    def test_window_root_is_manual_even_when_bounds_include_point(self):
        self.worker.capture_action({'action': 'right-click', 'x': 150, 'y': 175},
                                   observation(role='window', actions=[]))
        self.assertEqual(self.model.steps[0]['action'], 'manual')
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'window_root_not_replay_target')

    def test_cached_preclick_target_survives_context_popup(self):
        obs = observation(role='group', actions=[])
        self.worker.hovered, self.worker.hover_at = obs, 10
        self.bridge.window_from_point.return_value = 400
        event = {'x': 150, 'y': 175, 'window': 200, 'hit_window': 201, 'at': 10.2}
        self.assertIs(self.worker.observe_down(event), obs)
        self.bridge.window_from_point.assert_not_called()
        self.worker.hover_at = 8
        event['hit_window'] = 200
        self.assertIsNone(self.worker.observe_down(event))

    def test_drag_never_becomes_semantic_click(self):
        self.worker.captures[1] = ({'x': 150, 'y': 175, 'action': 'click'}, observation(actions=['Click']))
        self.worker.handle({'kind': 'up', 'token': 1, 'x': 180, 'y': 175, 'inside': True})
        self.assertEqual(self.model.steps[0]['action'], 'manual')
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'drag_needs_review')

    def test_wheel_shortcuts_and_unreadable_typing_are_explicit_gaps(self):
        self.worker.handle({'kind': 'scroll'})
        self.worker.handle({'kind': 'shortcut'})
        self.worker.uia.focused_element = None
        self.worker.handle({'kind': 'typing', 'at': 10})
        self.worker.handle({'kind': 'typing', 'at': 10.1})
        self.assertEqual([step['recognition']['reason'] for step in self.model.steps],
                         ['scroll_needs_review', 'shortcut_needs_review', 'input_value_unavailable'])

    def test_checked_action_uses_current_value_not_assumed_toggle(self):
        self.worker.uia.checked_value = False
        self.worker.capture_action({'action': 'click'}, observation(role='checkbox', actions=['Check']))
        self.assertEqual(self.model.steps[0]['action'], 'uncheck')
        self.assertEqual(self.model.steps[0]['expect'], {'kind': 'checked', 'value': False})

    def test_changed_process_invalidates_pending_trusted_capture(self):
        self.worker.watch(observation())
        self.worker.hovered = observation()
        self.worker.captures[1] = ({'x': 150, 'y': 175, 'action': 'click'}, observation(actions=['Click']))
        self.binding.check.side_effect = RuntimeError('lifetime changed')
        self.worker.handle({'kind': 'up', 'token': 1, 'x': 150, 'y': 175, 'inside': True})
        self.assertTrue(self.worker.invalidated)
        self.assertIsNone(self.worker.watched)
        self.assertIsNone(self.worker.hovered)
        self.assertEqual(self.model.steps, [])
        self.assertEqual(self.worker.notices.get_nowait(), ('fatal', 'target_lifetime_changed'))
        self.worker.handle({'kind': 'typing', 'at': 11})
        self.assertTrue(self.worker.notices.empty())

    def test_output_paths_cannot_escape_or_use_alternate_stream(self):
        for path in (self.output.parent / 'elsewhere.json', self.output / 'x:stream', Path('relative.json')):
            with self.assertRaises(recorder.RecorderError):
                self.model._path(path)
        self.model.progress.mkdir()
        with self.assertRaises(recorder.RecorderError):
            self.model.write(self.model.progress, {'state': 'ready'})

    def test_empty_recording_requires_review_and_cancel_does_not_create_draft(self):
        self.model.finish()
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'empty_recording_requires_review')
        validate_workflow(json.loads(self.model.draft.read_text(encoding='utf-8')))
        second = copy.deepcopy(self.request)
        second['draft_path'] = str(self.output / 'cancelled.json')
        other = recorder.RecordingModel(second, self.info)
        self.assertEqual(other.finish(cancelled=True)['status'], 'cancelled')
        self.assertFalse(other.draft.exists())

    def test_failed_write_does_not_mutate_draft_and_removes_temporary_file(self):
        self.model.write(self.model.draft, {'old': True})
        with patch.object(recorder.os, 'replace', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.model.write(self.model.draft, {'new': True})
        self.assertEqual(json.loads(self.model.draft.read_text(encoding='utf-8')), {'old': True})
        self.assertEqual(list(self.output.glob('.recording-*.tmp')), [])

    def test_double_click_revises_first_click_instead_of_replaying_it(self):
        first = {'token': 1, 'x': 150, 'y': 175, 'action': 'click'}
        obs = observation(role='button', actions=['Click'])
        self.worker.captures[1] = (first, obs)
        with patch.object(recorder.time, 'sleep'):
            self.worker.handle({'kind': 'up', 'token': 1, 'x': 150, 'y': 175, 'inside': True})
            original_id = self.model.steps[0]['id']
            self.worker.captures[2] = ({**first, 'token': 2, 'double': True, 'previous_token': 1}, obs)
            self.worker.handle({'kind': 'up', 'token': 2, 'x': 150, 'y': 175, 'inside': True})
        self.assertEqual(len(self.model.steps), 1)
        self.assertEqual(self.model.steps[0]['id'], original_id)
        self.assertEqual(self.model.steps[0]['action'], 'manual')
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'double_click_needs_review')
        events = self.recorded_events()
        self.assertEqual([event['step']['action'] for event in events], ['click', 'manual'])
        self.assertEqual([event['kind'] for event in events], ['step_recorded', 'step_revision'])

    def test_double_click_on_editable_field_preserves_future_final_value(self):
        first = {'token': 1, 'x': 150, 'y': 175, 'action': 'click'}
        obs = observation()
        self.worker.captures[1] = (first, obs)
        with patch.object(recorder.time, 'sleep'):
            self.worker.handle({'kind': 'up', 'token': 1, 'x': 150, 'y': 175, 'inside': True})
            self.worker.captures[2] = ({**first, 'token': 2, 'double': True, 'previous_token': 1}, obs)
            self.worker.handle({'kind': 'up', 'token': 2, 'x': 150, 'y': 175, 'inside': True})
        self.assertEqual(self.model.steps[0]['action'], 'manual')
        self.assertIsNone(self.worker.watched)
        self.worker.handle({'kind': 'typing', 'at': 20})
        self.worker.uia.values['field'] = 'Final typed value'
        self.worker.finalize_watch(True)
        self.assertEqual(self.model.steps[1]['value'], 'Final typed value')

    def test_double_pair_uses_windows_timing_geometry_and_target_window(self):
        previous = {'at': 10, 'x': 150, 'y': 175, 'action': 'click', 'window': 200, 'hit_window': 201}
        current = {**previous, 'at': 10.2, 'x': 151}
        self.assertTrue(recorder._double_pair(previous, current, .5, 4, 4))
        for change in ({'at': 10.6}, {'x': 154}, {'window': 300}, {'hit_window': 202}, {'action': 'right-click'}):
            self.assertFalse(recorder._double_pair(previous, {**current, **change}, .5, 4, 4))
        self.assertFalse(recorder._double_pair({**previous, 'double': True}, current, .5, 4, 4))

    def test_ui_failure_stops_hook_and_worker_without_raw_exception(self):
        self.worker.enabled.set()
        pump = Mock()
        pump.stop.side_effect = RuntimeError('private target data')
        result = recorder._abort_recording(pump, self.worker)
        pump.stop.assert_called_once_with()
        self.assertFalse(self.worker.enabled.is_set())
        self.assertTrue(self.worker.abort.is_set())
        self.assertEqual(result['diagnostic'], 'recording_ui_failed')
        self.assertNotIn('private target data', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
