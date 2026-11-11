"""Location capture never becomes a guessed choice or reject unparsed prose."""
import copy
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import intent_capture as capture
import intent_tasks


class IntentCaptureTests(unittest.TestCase):
    def test_location_reader_does_not_request_value_or_option_state(self):
        uia = capture.PositionUIA.__new__(capture.PositionUIA)
        uia._pattern_value = Mock(side_effect=AssertionError('value was read'))
        uia._accessible_value = Mock(side_effect=AssertionError('value was read'))
        self.assertIsNone(uia.value(object()))
        item = capture.draft({'role': 'combobox', 'automation_id': 'gcboFamily'}, 'Family')
        self.assertEqual(item['instruction']['text'], '')
        self.assertNotIn('value', item)

    def test_repeated_clicks_in_one_combo_capture_one_location_not_selected_value(self):
        notes = capture.PositionNotes()
        anchor = {'role': 'combobox', 'automation_id': 'gcboFamily'}
        notes.add(capture.draft(anchor, 'Family'))
        notes.add(capture.draft(anchor, 'Family'))
        notes.add(capture.draft({'role': 'button', 'automation_id': 'search'}, '조회'))
        notes.add(capture.draft(anchor, 'Family'))
        self.assertEqual(len(notes.snapshot()), 3)
        self.assertTrue(all(s['instruction']['text'] == '' for s in notes.snapshot()))

    def test_click_worker_ignores_typing_and_records_only_the_down_target(self):
        binding = Mock()
        notes = capture.PositionNotes()
        worker = capture.PositionWorker(binding, Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = Mock()
        worker.uia.value.side_effect = AssertionError('value read')
        observed = object()
        worker.observe_down = Mock(return_value=observed)
        picked = {'anchor': {'role': 'combobox', 'automation_id': 'gcboStatus'}, 'label': 'MES Setup'}
        with patch.object(capture, 'anchor_from_observation', return_value=picked):
            worker.handle({'kind': 'typing', 'at': 1})
            worker.handle({'kind': 'down', 'x': 20, 'y': 30, 'token': 1})
            worker.handle({'kind': 'up', 'x': 20, 'y': 30, 'token': 1, 'inside': True})
        item, = notes.snapshot()
        self.assertEqual(item['anchor'], picked['anchor'])
        self.assertEqual(item['instruction']['text'], '')
        self.assertEqual(item['action'], 'intent')

    def test_missing_anchor_keeps_unparsed_multiline_text_and_can_be_repaired(self):
        raw = capture.draft()
        text = 'MES SETUP 조건을 N으로 맞춘 다음\n옆의 FAMILY에는 입력받은 변수를 사용해줘.'
        saved = intent_tasks.make_step(raw, text, parameters=['FAMILY'])
        self.assertEqual(saved['instruction']['text'], text)
        self.assertIsNone(saved['anchor'])
        repaired = dict(saved, anchor={'role': 'combobox', 'automation_id': 'gcboStatus'})
        result = intent_tasks.make_step(repaired, text, parameters=['FAMILY'])
        self.assertEqual(result['instruction'], saved['instruction'])

    def test_open_existing_task_retains_explicit_fixed_and_required_semantics(self):
        old = {'action': 'select', 'role': 'combobox', 'match': 'automation_id', 'target': 'gcboStatus',
               'instruction': {'mode': 'fixed', 'text': 'MES Setup을 N으로 설정', 'value': 'N'}, 'alias': '상태'}
        opened = capture.editable_step(old)
        self.assertEqual(opened['anchor']['automation_id'], 'gcboStatus')
        self.assertEqual(opened['instruction']['text'], old['instruction']['text'])
        self.assertEqual(opened['alias'], '상태')
        old['instruction'] = {'mode': 'required', 'text': '', 'parameter': 'FAMILY'}
        opened = capture.editable_step(old)
        self.assertEqual(opened['instruction']['parameters'], ['FAMILY'])
        self.assertIn('{FAMILY}', opened['instruction']['text'])

    def test_specialized_steps_remain_intact_when_opened_for_editing(self):
        specialized = {'action': 'result-menu', 'label': '결과 저장', 'context': {'sample': 'unchanged'}}
        actual = capture.editable_step(specialized)
        actual.pop('id')
        self.assertEqual(actual, specialized)

    def test_new_raw_combo_does_not_inherit_observed_value_as_instruction(self):
        old_raw = {'id': 'capture', 'action': 'set-value', 'value': 'Hybrid', 'field_label': 'Family',
                   'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': 'gcboFamily'}}}
        self.assertEqual(capture.editable_step(old_raw)['instruction']['text'], '')
