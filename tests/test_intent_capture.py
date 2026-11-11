"""Action drafts remain editable and never copy an unchosen existing value."""
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
        combo = Mock()
        combo.property.return_value = 50003
        self.assertIsNone(uia.value(combo))
        item = capture.draft({'role': 'combobox', 'automation_id': 'gcboFamily'}, 'Family')
        self.assertEqual(item['instruction']['parameters'], ['Family'])
        self.assertIn('{Family}', item['instruction']['text'])
        self.assertNotIn('Hybrid', item['instruction']['text'])
        self.assertNotIn('value', item)

    def test_repeated_clicks_in_one_combo_capture_one_location_not_selected_value(self):
        notes = capture.PositionNotes()
        anchor = {'role': 'combobox', 'automation_id': 'gcboFamily'}
        notes.add(capture.draft(anchor, 'Family'))
        notes.add(capture.draft(anchor, 'Family'))
        notes.add(capture.draft({'role': 'button', 'automation_id': 'search'}, '조회'))
        notes.add(capture.draft(anchor, 'Family'))
        self.assertEqual(len(notes.snapshot()), 3)
        self.assertEqual(notes.snapshot()[0]['instruction']['parameters'], ['Family'])
        self.assertEqual(notes.snapshot()[1]['instruction']['text'], '조회 버튼을 눌러 줘')

    def test_click_worker_ignores_typing_and_records_only_the_down_target(self):
        binding = Mock()
        notes = capture.PositionNotes()
        worker = capture.PositionWorker(binding, Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = Mock()
        worker.uia.value.side_effect = AssertionError('value read')
        observed = {'node': {'role': 'combobox', 'secure': False}}
        worker.uia.combo_observation.return_value = observed
        worker.observe_down = Mock(return_value=observed)
        picked = {'anchor': {'role': 'combobox', 'automation_id': 'gcboStatus'}, 'label': 'MES Setup'}
        with patch.object(capture, 'anchor_from_observation', return_value=picked):
            worker.handle({'kind': 'typing', 'at': 1})
            worker.handle({'kind': 'down', 'x': 20, 'y': 30, 'token': 1})
            worker.handle({'kind': 'up', 'x': 20, 'y': 30, 'token': 1, 'inside': True})
        item, = notes.snapshot()
        self.assertEqual(item['anchor'], picked['anchor'])
        self.assertEqual(item['instruction']['parameters'], ['MES Setup'])
        self.assertNotIn('"N"', item['instruction']['text'])
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

    def test_witnessed_option_click_replaces_generic_draft_on_same_step(self):
        notes = capture.PositionNotes()
        notes.max_steps = 1
        anchor = {'role': 'combobox', 'automation_id': 'conditionState'}
        first = capture.draft(anchor, 'MES Setup')
        notes.add(first)
        chosen = capture.draft(anchor, 'MES Setup', action='select-option', value='N')
        notes.add(chosen, strength=2)
        notes.add(capture.draft(anchor, 'MES Setup'))
        saved, = notes.snapshot()
        self.assertEqual(saved['id'], first['id'])
        self.assertIn('"N"', saved['instruction']['text'])
        self.assertEqual(saved['instruction']['parameters'], [])
        self.assertFalse(notes.limit)
        changed = capture.draft(anchor, 'MES Setup', action='select-option', value='P')
        notes.add(changed, strength=2)
        self.assertIn('"P"', notes.snapshot()[0]['instruction']['text'])

    def test_option_draft_requires_exact_click_and_proved_owner_not_selected_state(self):
        notes = capture.PositionNotes()
        worker = capture.PositionWorker(Mock(), Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = Mock()
        combo = {'node': {'role': 'combobox', 'secure': False}}
        worker.uia.combo_observation.return_value = combo
        observed = {'node': {'role': 'listitem', 'secure': False, 'enabled': True, 'offscreen': False,
                             'bounds_valid': True, 'bounds': {'x': 10, 'y': 20, 'width': 50, 'height': 20},
                             'name': '[Editor] item 1', 'value': 'N'}}
        event = {'kind': 'down', 'x': 20, 'y': 30, 'token': 1, 'action': 'click'}
        anchor = {'role': 'combobox', 'automation_id': 'conditionState'}
        self.assertEqual(worker.selected_option(observed, event, anchor), 'N')
        for updates in ({'offscreen': True}, {'bounds_valid': False}, {'secure': None},
                        {'value_evidence': {'status': 'conflict'}}):
            bad = {'node': dict(observed['node'], **updates)}
            self.assertIsNone(worker.selected_option(bad, event, anchor))
        self.assertIsNone(worker.selected_option(observed, dict(event, x=100), anchor))
        self.assertIsNone(worker.selected_option(observed, dict(event, action='right-click'), anchor))
        worker.uia.combo_observation.return_value = None
        self.assertIsNone(worker.selected_option(observed, event, anchor))

    def test_real_down_up_produces_value_draft_without_reading_combo_value(self):
        notes = capture.PositionNotes()
        worker = capture.PositionWorker(Mock(), Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = Mock()
        worker.uia.value.side_effect = AssertionError('combo value read')
        combo = {'node': {'role': 'combobox', 'secure': False}}
        worker.uia.combo_observation.return_value = combo
        option = {'node': {'role': 'listitem', 'secure': False, 'enabled': True, 'offscreen': False,
                          'bounds_valid': True, 'bounds': {'x': 10, 'y': 20, 'width': 50, 'height': 20},
                          'value': 'N'}}
        worker.observe_down = Mock(return_value=option)
        picked = {'anchor': {'role': 'combobox', 'automation_id': 'conditionState'}, 'label': 'MES Setup'}
        with patch.object(capture, 'anchor_from_observation', return_value=picked):
            worker.handle({'kind': 'down', 'x': 20, 'y': 30, 'token': 1, 'action': 'click'})
            self.assertEqual(notes.snapshot(), [])
            worker.handle({'kind': 'up', 'x': 20, 'y': 30, 'token': 1, 'inside': True})
        step, = notes.snapshot()
        self.assertIn('"N"', step['instruction']['text'])
        self.assertEqual(step['instruction']['parameters'], [])
        stored = intent_tasks.make_step(step, step['instruction']['text'])
        intent_tasks.validate_step(stored)

    def test_option_observation_reads_only_option_not_parent_or_editors(self):
        uia = capture.PositionUIA.__new__(capture.PositionUIA)
        item = Mock()
        item.property.return_value = 50007
        uia._selected_option_observation = Mock(return_value={'value': 'N'})
        uia._pattern_value = Mock(side_effect=AssertionError('parent value read'))
        self.assertEqual(uia.value_observation(item), {'value': 'N'})
        uia._selected_option_observation.assert_called_once_with(item)

    def test_button_and_right_click_suggestions_do_not_invent_menu_path(self):
        button = capture.draft({'role': 'button', 'automation_id': 'QueryCommand'}, '조회', action='click')
        table = capture.draft({'role': 'group', 'automation_id': 'resultGrid', 'point': [.4, .3]},
                              '결과 표', action='right-click')
        self.assertEqual(button['instruction']['text'], '조회 버튼을 눌러 줘')
        self.assertIn('우클릭', table['instruction']['text'])
        self.assertNotIn('EXPORT', table['instruction']['text'])
        notes = capture.PositionNotes()
        notes.add(button)
        notes.add(capture.draft(button['anchor'], '조회', action='click'))
        self.assertEqual(len(notes.snapshot()), 2)

    def test_literal_braces_in_ui_caption_do_not_require_an_input_variable(self):
        item = capture.draft({'role': 'button', 'name': 'Run {mode}'}, 'Run {mode}', action='click')
        self.assertEqual(item['instruction']['parameters'], [])
        self.assertEqual(item['anchor']['name'], 'Run {mode}')
        self.assertIn('Run ［mode］', item['instruction']['text'])

    def test_double_click_yields_two_single_click_drafts_without_extra_click(self):
        notes = capture.PositionNotes()
        worker = capture.PositionWorker(Mock(), Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = Mock()
        worker.uia.combo_observation.return_value = None
        worker.observe_down = Mock(return_value={'node': {'role': 'button', 'secure': False}})
        picked = {'anchor': {'role': 'button', 'automation_id': 'run'}, 'label': 'Run'}
        with patch.object(capture, 'anchor_from_observation', return_value=picked):
            for token in (1, 2):
                worker.handle({'kind': 'down', 'x': 20, 'y': 30, 'token': token, 'action': 'click',
                               'double': token == 2, 'previous_token': 1})
                worker.handle({'kind': 'up', 'x': 20, 'y': 30, 'token': token, 'inside': True})
        self.assertEqual([item['instruction']['text'] for item in notes.snapshot()],
                         ['Run 버튼을 눌러 줘', 'Run 버튼을 눌러 줘'])

    def test_delayed_draft_update_preserves_user_text_variables_and_intentionally_empty_text(self):
        anchor = {'role': 'combobox', 'automation_id': 'family'}
        for text, parameters in [('다른 문장으로 직접 실행해 줘', ['Family']),
                                 ('', []), (None, []), (None, ['제품군'])]:
            with self.subTest(text=text, parameters=parameters):
                item = capture.draft(anchor, 'Family')
                drafts = capture.DraftEdits()
                drafts.remember(item)
                # Editing may still only be in the form, before its save/flush.
                actual_text = item['instruction']['text'] if text is None else text
                drafts.note_edit(item, actual_text, parameters)
                selected = capture.draft(anchor, 'Family', action='select-option', value='Hybrid')
                before = copy.deepcopy(item)
                self.assertFalse(drafts.merge(item, selected))
                self.assertEqual(item, before)
                # Once edited, returning to the old text does not re-enable overwrites.
                original = drafts.generated[item['id']]
                drafts.note_edit(item, original['text'], original['parameters'])
                self.assertFalse(drafts.merge(item, selected))

    def test_untouched_auto_draft_updates_but_reopened_and_retargeted_steps_do_not(self):
        anchor = {'role': 'combobox', 'automation_id': 'state'}
        item = capture.draft(anchor, 'MES Setup')
        drafts = capture.DraftEdits()
        drafts.remember(item)
        chosen = capture.draft(anchor, 'MES Setup', action='select-option', value='N')
        drafts.note_edit(item, item['instruction']['text'], item['instruction']['parameters'])
        self.assertTrue(drafts.merge(item, chosen))
        self.assertEqual(item['instruction']['parameters'], [])
        reopened = capture.editable_step(item)
        self.assertFalse(capture.DraftEdits().merge(reopened, chosen))
        item['anchor'] = {'role': 'combobox', 'automation_id': 'another'}
        self.assertFalse(drafts.merge(item, chosen))
