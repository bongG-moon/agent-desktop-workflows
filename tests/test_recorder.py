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
                     'secure': False, 'enabled': True, 'offscreen': False, 'bounds_valid': True,
                     'available_actions': ['SetValue'] if actions is None else actions,
                     'locator': {'automation_id': 'Family', 'role': role},
                     'bounds': {'x': 100, 'y': 150, 'width': 200, 'height': 100}}}


class FakeUIA:
    def __init__(self):
        self.values = {'field': 'Initial'}
        self.protected = set()
        self.focused_element = 'field'
        self.checked_value = True
        self.combo_parents = {}

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

    def combo_observation(self, item):
        if item['node']['role'] == 'combobox' and 'SetValue' in item['node']['available_actions']:
            return item
        return self.combo_parents.get(item['element'])


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

    def test_combo_arrow_and_detached_option_record_final_combo_value(self):
        combo = observation()
        arrow = observation(role='button', actions=['Click'], element='arrow')
        item = observation(role='listitem', actions=['Click', 'Select'], element='option')
        item['node']['name'] = 'N'
        self.worker.uia.combo_parents.update(arrow=combo, option=combo)
        self.worker.capture_action({'action': 'click'}, arrow)
        self.assertEqual(self.model.steps, [])
        self.worker.uia.values['field'] = 'N'
        self.worker.capture_action({'action': 'click'}, item)
        self.assertEqual([s['action'] for s in self.model.steps], ['set-value'])
        self.assertEqual(self.model.steps[0]['value'], 'N')
        self.assertEqual(self.model.steps[0]['target']['locator'], combo['node']['locator'])

    def test_combo_missing_or_empty_readback_never_records_empty_set_value(self):
        for value in (None, ''):
            self.worker.uia.values['field'] = value
            combo = observation()
            self.worker.watch(combo)
            self.assertIsNotNone(self.worker.watched)
            self.worker.finalize_watch(True)
            step = self.model.steps[-1]
            self.assertEqual(step['action'], 'manual')
            self.assertEqual(step['target'], {'kind': 'element', 'locator': combo['node']['locator']})
            self.assertEqual(step['recognition']['target'], 'identified')
            self.assertEqual(step['recognition']['reason'], 'combo_value_unobserved')
            self.assertNotIn('value', step)
            self.assertIn('Family', step['label'])
        self.model.finish()
        validate_workflow(json.loads(self.model.draft.read_text(encoding='utf-8')))

    def test_combo_option_intent_survives_missing_value_without_false_result(self):
        combo = observation()
        item = observation(role='listitem', actions=['Click', 'Select'], element='option')
        item['node']['name'] = 'N'
        self.worker.uia.values['field'] = None
        self.worker.uia.combo_parents['option'] = combo
        self.worker.capture_action({'action': 'click'}, item)
        self.assertEqual(len(self.model.steps), 1)
        self.assertEqual(self.model.steps[0]['action'], 'set-value')
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'combo_selection_unverified')
        self.assertEqual(self.model.steps[0]['value'], 'N')
        self.assertEqual(self.model.steps[0]['expect'], {'kind': 'value', 'value': 'N'})
        self.assertEqual(self.model.steps[0]['selection'],
                         {'source': 'option_click', 'value': 'N', 'result': 'unavailable'})
        self.assertEqual(self.model.snapshot()['warning_count'], 1)

    def option(self, value='Choice B', combo=None, element='option'):
        combo = combo or observation()
        item = observation(role='listitem', actions=['Select'], element=element)
        item['node']['name'] = value
        item['hit_window'] = 300
        item['combo'] = combo
        self.worker.uia.combo_parents[element] = combo
        return item

    def test_option_intent_and_actual_value_mismatch_are_distinct(self):
        self.worker.uia.values['field'] = 'Choice A'
        self.worker.capture_action({'action': 'click'}, self.option())
        step = self.model.steps[0]
        self.assertEqual(step['value'], 'Choice B')
        self.assertEqual(step['selection'], {'source': 'option_click', 'value': 'Choice B',
                                             'result': 'mismatch', 'observed_value': 'Choice A'})
        self.assertEqual(step['recognition']['reason'], 'combo_selection_mismatch')
        self.assertEqual(self.model.snapshot()['warning_count'], 1)

    def test_cached_clicked_option_records_payload_when_name_is_provider_metadata(self):
        combo = observation(actions=['SetValue', 'Expand'])
        item = self.option('[Provider] Option 2', combo)
        item['node']['value'] = 'Parcel'
        self.worker.uia.values['field'] = None
        self.worker.watch(combo)
        self.worker.option_history.append((10, [item]))
        self.worker.handle({'kind': 'down', 'action': 'click', 'at': 10.05, 'token': 4,
                            'x': 130, 'y': 170, 'hit_window': 300, 'window': 300})
        self.worker.handle({'kind': 'up', 'token': 4, 'inside': True, 'x': 131, 'y': 171})
        step, = self.model.steps
        self.assertEqual(step['value'], 'Parcel')
        self.assertEqual(step['expect'], {'kind': 'value', 'value': 'Parcel'})
        self.assertEqual(step['selection'], {'source': 'option_click', 'value': 'Parcel',
                                             'result': 'unavailable'})
        self.assertIs(step['headed'], True)
        self.assertEqual(step['combo_replay'], 'visible_option')
        self.assertNotIn('[Provider]', self.model.events_path.read_text(encoding='utf-8'))

    def test_hidden_or_invalid_option_payload_never_records_display_name(self):
        for extra in [{'enabled': False}, {'offscreen': True}, {'bounds_valid': False},
                      {'value': 'bad\0value'}, {'value': '\ud800'},
                      {'value': None, 'value_evidence': {'status': 'conflict'}}]:
            with self.subTest(extra=extra):
                item = self.option('Option 1')
                item['node'].update(extra)
                self.worker.capture_action({'action': 'click'}, item)
                self.assertEqual(self.model.steps[-1]['recognition']['reason'], 'combo_option_value_unavailable')
                self.assertEqual(self.model.steps[-1]['target']['locator'], item['combo']['node']['locator'])
                self.assertNotIn('value', self.model.steps[-1])

    def test_unwitnessed_point_keeps_known_field_once_without_replacing_recorded_value(self):
        combo = observation()
        self.worker.watch(combo)
        self.worker.capture_action({'action': 'click', 'x': 10, 'y': 20}, self.option(combo=combo))
        step, = self.model.steps
        self.assertEqual(step['recognition']['reason'], 'combo_option_value_unavailable')
        self.assertEqual(step['target']['locator'], combo['node']['locator'])
        self.assertNotIn('value', step)

    def test_writable_provider_without_opener_keeps_set_value_replay(self):
        self.worker.capture_action({'action': 'click'}, self.option())
        step, = self.model.steps
        self.assertEqual(step['value'], 'Choice B')
        self.assertNotIn('combo_replay', step)
        self.assertFalse(step['headed'])

    def test_expand_only_combo_requires_a_witnessed_choice_for_automatic_replay(self):
        combo = observation(actions=['Expand', 'Collapse'])
        self.worker.watch(combo)
        self.worker.finalize_watch(True)
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'combo_option_click_required')
        self.assertEqual(self.model.steps[0]['target']['locator'], combo['node']['locator'])
        self.worker.capture_action({'action': 'click'}, self.option(combo=combo))
        self.assertEqual(self.model.steps[1]['combo_replay'], 'visible_option')

    def test_delayed_selection_readback_revises_evidence_without_changing_intent_or_id(self):
        self.worker.uia.values['field'] = None
        self.worker.capture_action({'action': 'click'}, self.option())
        first = copy.deepcopy(self.model.steps[0])
        self.worker.uia.values['field'] = 'Choice B'
        self.worker.finalize_watch(False)
        last = self.model.steps[0]
        self.assertEqual((last['id'], last['value']), (first['id'], first['value']))
        self.assertEqual(last['selection']['result'], 'matched')
        self.assertEqual(self.model.snapshot()['warning_count'], 0)
        events_before = len(self.recorded_events())
        self.worker.finalize_watch(False)
        self.assertEqual(len(self.recorded_events()), events_before)

    def test_same_option_reselection_records_explicit_intent(self):
        self.worker.uia.values['field'] = 'Choice B'
        self.worker.capture_action({'action': 'click'}, self.option())
        self.assertEqual(len(self.model.steps), 1)
        self.assertEqual(self.model.steps[0]['value'], 'Choice B')
        self.assertEqual(self.model.steps[0]['selection']['result'], 'matched')

    def test_reopen_then_cancel_preserves_previous_explicit_selection_only(self):
        combo = observation()
        self.worker.uia.values['field'] = 'Choice B'
        self.worker.capture_action({'action': 'click'}, self.option(combo=combo))
        self.worker.capture_action({'action': 'click'}, combo)
        self.worker.handle({'kind': 'escape'})
        choices = [step for step in self.model.steps if step['action'] == 'set-value']
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]['value'], 'Choice B')
        self.assertEqual(choices[0]['selection']['result'], 'matched')

    def test_option_read_failure_keeps_recorded_intent(self):
        self.worker.uia.value = Mock(side_effect=RuntimeError('provider disappeared'))
        self.worker.capture_action({'action': 'click'}, self.option())
        self.assertEqual(self.model.steps[0]['value'], 'Choice B')
        self.assertEqual(self.model.steps[0]['selection']['result'], 'unavailable')
        self.assertNotIn('provider disappeared', self.model.events_path.read_text(encoding='utf-8'))

    def test_invalid_or_protected_option_name_never_becomes_action_value(self):
        for value, secure in [('Secret', True), ('Secret', None), ('', False), ('x\0y', False),
                              ('X' * (recorder.MAX_VALUE_BYTES + 1), False)]:
            item = self.option(value)
            item['node']['secure'] = secure
            self.worker.capture_action({'action': 'click'}, item)
            self.assertEqual(self.model.steps[-1]['action'], 'manual')
            self.assertNotIn('selection', self.model.steps[-1])
            self.assertNotIn('value', self.model.steps[-1])
        self.assertNotIn('Secret', self.model.events_path.read_text(encoding='utf-8'))

    def test_label_is_display_only_and_does_not_replace_actual_locator(self):
        combo = observation()
        combo['node']['semantic_label'] = 'Output format'
        self.worker.capture_action({'action': 'click'}, self.option(combo=combo))
        step = self.model.steps[0]
        self.assertEqual(step['label'], 'Output format → Choice B')
        self.assertEqual(step['target']['locator'], combo['node']['locator'])

    def test_option_inventory_alone_never_creates_selection_intent(self):
        combo = observation()
        item = self.option(combo=combo)
        self.worker.uia.combo_options = lambda *a, **kw: [item]
        self.worker.capture_action({'action': 'click'}, combo)
        self.assertTrue(self.worker.option_history)
        self.assertEqual(self.model.steps, [])
        self.worker.handle({'kind': 'escape'})
        self.assertFalse(any('selection' in step or step['action'] == 'set-value' for step in self.model.steps))
        self.assertFalse(self.worker.option_history)

    def test_option_cache_captures_fast_click_without_hover_and_survives_closed_popup(self):
        combo = observation()
        item = self.option(combo=combo)
        self.worker.uia.combo_options = lambda *a, **kw: [item]
        self.worker.watch(combo)
        with patch.object(recorder.time, 'monotonic', return_value=10):
            self.worker.cache_options()
        self.bridge.window_from_point.return_value = 200  # Popup has closed now.
        event = {'x': 150, 'y': 175, 'window': 300, 'hit_window': 300, 'at': 10.02}
        observed = self.worker.observe_down(event)
        self.assertEqual(observed['node']['name'], 'Choice B')
        self.assertEqual(observed['combo'], combo)
        self.bridge.window_from_point.assert_not_called()

    def test_option_cache_rejects_ambiguous_stale_and_wrong_window_points(self):
        item = self.option()
        duplicate = self.option(element='other-option')
        event = {'x': 150, 'y': 175, 'window': 300, 'hit_window': 300, 'at': 10.02}
        self.worker.option_history.append((10, [item, duplicate]))
        self.assertIsNone(self.worker.observe_down(event))
        self.worker.option_history.clear()
        self.worker.option_history.append((8, [item]))
        self.bridge.window_from_point.return_value = 200
        self.assertIsNone(self.worker.observe_down(event))
        self.worker.option_history.append((10, [item]))
        self.assertIsNone(self.worker.observe_down({**event, 'window': 301, 'hit_window': 301}))

    def test_new_empty_inventory_invalidates_stale_option_hover_for_later_click(self):
        item = self.option()
        self.worker.option_history.extend([(10, [item]), (10.1, [])])
        self.worker.hovered, self.worker.hover_at = item, 10
        self.bridge.window_from_point.return_value = 200
        event = {'x': 150, 'y': 175, 'window': 300, 'hit_window': 300, 'at': 10.2}
        self.assertIsNone(self.worker.observe_down(event))

    def test_selection_intent_does_not_take_next_action_post_value_as_evidence(self):
        with patch.object(recorder.time, 'monotonic', return_value=10):
            self.worker.uia.values['field'] = None
            self.worker.capture_action({'action': 'click', 'at': 10}, self.option())
        self.worker.uia.values['field'] = 'Choice B'
        with patch.object(recorder.time, 'monotonic', return_value=12.2):
            self.worker.finalize_watch(False)
        self.worker.freeze_watch(12)
        self.assertEqual(self.model.steps[0]['value'], 'Choice B')
        self.assertEqual(self.model.steps[0]['selection']['result'], 'unavailable')

    def test_delayed_same_combo_opener_freezes_prior_selection_at_down_timestamp(self):
        combo = observation()
        with patch.object(recorder.time, 'monotonic', return_value=10):
            self.worker.uia.values['field'] = None
            self.worker.capture_action({'action': 'click', 'at': 10}, self.option(combo=combo))
        original = self.model.steps[0]['id']
        self.worker.observe_down = lambda event: combo
        # By the time this queued down is handled, the provider has a value
        # from later application activity and the old settle deadline expired.
        self.worker.uia.values['field'] = 'Choice B'
        with patch.object(recorder.time, 'monotonic', return_value=11):
            self.worker.last_hover = self.worker.last_identity = 11
            self.worker.handle({'kind': 'down', 'token': 1, 'action': 'click',
                                'x': 150, 'y': 175, 'at': 10.2})
            self.worker.tick()
        prior = self.model.steps[0]
        self.assertEqual((prior['id'], prior['value']), (original, 'Choice B'))
        self.assertEqual(prior['selection']['result'], 'unavailable')
        self.assertIsNone(self.worker.watched)
        with patch.object(recorder.time, 'monotonic', return_value=11.1):
            self.worker.handle({'kind': 'up', 'token': 1, 'inside': True,
                                'x': 150, 'y': 175, 'at': 10.3})
        self.worker.handle({'kind': 'escape'})
        self.assertEqual(self.model.steps[0]['selection']['result'], 'unavailable')
        self.assertEqual(len([step for step in self.model.steps if 'selection' in step]), 1)

    def test_adjacent_combos_with_same_option_keep_their_own_identity(self):
        first, second = observation(), observation(element='other')
        second['node']['locator'] = {'automation_id': 'OtherField', 'role': 'combobox'}
        self.worker.uia.values.update(field=None, other=None)
        self.worker.capture_action({'action': 'click'}, self.option('Same', first, 'option1'))
        self.worker.freeze_watch()
        self.worker.capture_action({'action': 'click'}, self.option('Same', second, 'option2'))
        self.assertEqual([s['target']['locator']['automation_id'] for s in self.model.steps], ['Family', 'OtherField'])
        self.assertEqual([s['value'] for s in self.model.steps], ['Same', 'Same'])

    def test_combo_can_become_readable_after_opening_unreadable_peer(self):
        self.worker.uia.values['field'] = None
        self.worker.watch(observation())
        self.worker.uia.values['field'] = 'Hybrid'
        self.worker.finalize_watch(True)
        self.assertEqual(self.model.steps[0]['value'], 'Hybrid')
        self.assertEqual(self.model.steps[0]['expect'], {'kind': 'value', 'value': 'Hybrid'})

    def test_combo_lost_readback_replaces_provisional_value_with_review_same_id(self):
        self.worker.watch(observation())
        self.worker.uia.values['field'] = 'N'
        self.worker.finalize_watch(False)
        original = self.model.steps[0]['id']
        self.worker.uia.values['field'] = None
        self.worker.finalize_watch(False)
        self.worker.freeze_watch()
        self.assertEqual(len(self.model.steps), 1)
        self.assertEqual(self.model.steps[0]['id'], original)
        self.assertEqual(self.model.steps[0]['action'], 'manual')
        self.assertNotIn('value', self.model.steps[0])
        self.assertEqual(self.model.steps[0]['target']['locator'], observation()['node']['locator'])

    def test_textfield_intentional_clear_still_records_empty_value(self):
        self.worker.watch(observation(role='textfield'))
        self.worker.uia.values['field'] = ''
        self.worker.finalize_watch(True)
        self.assertEqual(self.model.steps[0]['action'], 'set-value')
        self.assertEqual(self.model.steps[0]['value'], '')
        self.assertEqual(self.model.steps[0]['expect'], {'kind': 'value', 'value': ''})

    def test_independent_listitem_after_edit_is_not_swallowed(self):
        self.worker.watch(observation(role='textfield'))
        self.worker.uia.values['field'] = 'Lot-42'
        self.worker.finalize_watch(False)
        row = observation(role='listitem', actions=['Select'], element='other-row')
        row['node']['name'] = 'Independent result'
        row['node']['locator'] = {'automation_id': 'ResultsRow', 'role': 'listitem'}
        self.worker.capture_action({'action': 'click'}, row)
        self.assertEqual([s['action'] for s in self.model.steps], ['set-value', 'select'])
        self.assertEqual(self.model.steps[1]['target']['locator']['automation_id'], 'ResultsRow')

    def _watched_input(self):
        with patch.object(recorder.time, 'monotonic', return_value=10):
            self.worker.watch(observation(role='textfield'))
        self.worker.uia.values['field'] = 'Lot-42'
        with patch.object(recorder.time, 'monotonic', return_value=11):
            self.worker.finalize_watch(False)
        button = observation(role='button', actions=['Click'], element='submit')
        button['node']['locator'] = {'automation_id': 'Submit', 'role': 'button'}
        self.worker.observe_down = lambda event: button
        return self.model.steps[0]['id']

    def test_submit_clear_does_not_replace_input_with_post_click_value(self):
        original = self._watched_input()
        self.worker.handle({'kind': 'down', 'token': 1, 'x': 150, 'y': 175, 'action': 'click', 'at': 12})
        self.worker.uia.values['field'] = ''  # Application consumed and cleared input.
        with patch.object(recorder.time, 'sleep'):
            self.worker.handle({'kind': 'up', 'token': 1, 'x': 150, 'y': 175, 'inside': True, 'at': 12.1})
        self.assertEqual([s['action'] for s in self.model.steps], ['set-value', 'click'])
        self.assertEqual((self.model.steps[0]['id'], self.model.steps[0]['value']), (original, 'Lot-42'))

    def test_delayed_down_uses_pre_event_sample_not_newer_poll(self):
        original = self._watched_input()
        self.worker.uia.values['field'] = ''
        with patch.object(recorder.time, 'monotonic', return_value=12.2):
            self.worker.finalize_watch(False)
        self.worker.handle({'kind': 'down', 'token': 1, 'x': 150, 'y': 175, 'action': 'click', 'at': 12})
        self.assertEqual((self.model.steps[0]['id'], self.model.steps[0]['value']), (original, 'Lot-42'))
        self.assertEqual([e['step'].get('value') for e in self.recorded_events()], ['Lot-42', '', 'Lot-42'])

    def test_unsampled_typing_before_transition_becomes_manual_not_stale_value(self):
        original = self._watched_input()
        self.worker.last_key = 11.95
        self.worker.handle({'kind': 'down', 'token': 1, 'x': 150, 'y': 175, 'action': 'click', 'at': 12})
        self.assertEqual(self.model.steps[0]['id'], original)
        self.assertEqual(self.model.steps[0]['action'], 'manual')
        self.assertNotIn('value', self.model.steps[0])
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'value_before_transition_unproven')

    def test_unrecognized_click_preserves_input_before_manual_gap(self):
        self._watched_input()
        self.worker.observe_down = lambda event: None
        self.worker.handle({'kind': 'down', 'token': 1, 'x': 150, 'y': 175, 'action': 'click', 'at': 12})
        with patch.object(recorder.time, 'sleep'):
            self.worker.handle({'kind': 'up', 'token': 1, 'x': 150, 'y': 175, 'inside': True})
        self.assertEqual([s['action'] for s in self.model.steps], ['set-value', 'manual'])
        self.assertEqual(self.model.steps[0]['value'], 'Lot-42')

    def test_unproven_detached_popup_is_a_gap_without_other_window_content(self):
        self.worker.observe_down = lambda event: None
        self.worker.handle({'kind': 'down', 'token': 1, 'x': 150, 'y': 175, 'action': 'click', 'at': 12,
                            'scope_proven': False})
        self.worker.handle({'kind': 'up', 'token': 1, 'x': 150, 'y': 175, 'inside': True})
        self.assertEqual(len(self.model.steps), 1)
        self.assertEqual(self.model.steps[0]['recognition']['reason'], 'popup_scope_unproven')

    def test_detached_combo_precapture_uses_native_hit_identity(self):
        cached = observation(role='listitem', actions=['Select'])
        cached['hit_window'] = 300
        self.worker.hovered, self.worker.hover_at = cached, 10
        observed = self.worker.observe_down({'window': 300, 'hit_window': 300, 'x': 150, 'y': 175, 'at': 10.2})
        self.assertIs(observed, cached)

    def test_preclick_hover_survives_newer_postclick_poll(self):
        option = observation(role='listitem', actions=['Select'])
        option['hit_window'] = 300
        self.worker.hover_history.append((10, option))
        self.worker.hovered, self.worker.hover_at = observation(role='window'), 10.3
        observed = self.worker.observe_down({'window': 300, 'hit_window': 300, 'x': 150, 'y': 175, 'at': 10.2})
        self.assertIs(observed, option)

    def test_invalid_initial_sample_is_manual_and_draft_remains_importable(self):
        for value in ('X' * (recorder.MAX_VALUE_BYTES + 1), 'prefix\0suffix'):
            self.worker.uia.values['field'] = value
            self.worker.watch(observation(role='textfield'))
            self.worker.freeze_watch()
            self.assertEqual(self.model.steps[-1]['action'], 'manual')
            self.assertEqual(self.model.steps[-1]['recognition']['reason'], 'value_readback_unavailable')
        self.model.finish()
        validate_workflow(json.loads(self.model.draft.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
