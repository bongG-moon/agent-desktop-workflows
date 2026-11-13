"""Recorded ordinary text can be fixed, cleared or supplied per run without recapturing."""
import json
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Load pinned bridge for the source checkout.
import learned_tasks as tasks
import teach_capture as capture


def textfield(value='00042'):
    return dict(id='search-field', action='set-value', field_label='검색어', value=value,
                target=dict(kind='element', locator=dict(role='textfield', automation_id='searchText')),
                recognition=dict(target='identified', action='supported', result='checkable'))


class TextfieldCaptureTests(unittest.TestCase):
    def test_observed_text_drafts_preserve_literal_data_including_empty_clear(self):
        for value in ('00042', '한글 "따옴표" & A=B', ''):
            with self.subTest(value=value):
                step = capture.capture_step(textfield(value))
                self.assertIn(json.dumps(value, ensure_ascii=False), capture.default_text(step))
                saved = tasks.from_recording('검색', 'Example.exe', 'Example', capture.StepEdits().apply([step]))
                first = saved['steps'][0]
                self.assertEqual((first['action'], first['role']), ('fill', 'textfield'))
                self.assertEqual(first['instruction']['value'], value)
                self.assertEqual(first['instruction']['mode'], 'fixed')

    def test_unreadable_text_keeps_location_and_requires_parameter(self):
        raw = textfield()
        raw.pop('value')
        raw.update(action='manual', recognition=dict(target='identified', action='manual',
                   result='manual', reason='value_readback_unavailable'))
        step = capture.capture_step(raw)
        self.assertEqual(step['action'], 'teach-target')
        self.assertEqual(capture.default_text(step), '입력받은 변수를 검색어 입력란에 입력해줘')
        result = tasks.recorded_step(capture.StepEdits().apply([step])[0])
        self.assertEqual(result['instruction']['parameter'], '검색어')
        self.assertEqual(result['recorded_effect'], 'unobserved')

    def test_changed_value_on_same_field_keeps_explicit_runtime_variable(self):
        step = capture.capture_step(textfield())
        edits = capture.StepEdits()
        edits.store(step, '', '입력받은 {Search} 변수를 검색어 입력란에 입력해줘')
        changed = capture.capture_step(textfield('different demonstration'))
        changed['target']['locator'].update(name='volatile peer caption', runtime_id=[99],
                                            bounds=dict(x=100, y=200))
        saved = tasks.recorded_step(edits.apply([changed])[0])
        self.assertEqual(saved['instruction']['parameter'], 'Search')
        self.assertNotIn('value', saved)
        self.assertNotIn('value', saved['instruction'])

    def test_explicit_empty_fixed_value_has_clear_preview_and_is_not_missing_argument(self):
        step = capture.capture_step(textfield())
        edits = capture.StepEdits()
        edits.store(step, '', '검색어 입력란을 비워줘', 'fixed', '')
        instruction = edits.get(step)['instruction']
        self.assertEqual(instruction['value'], '')
        self.assertIn('비우기', capture.instruction_view(step, instruction))
        tasks.recorded_step(edits.apply([step])[0])

    def test_password_and_combo_editor_values_do_not_survive_capture(self):
        for update in ({'password': True}, {'secure': True},
                       {'ancestors': [dict(role='combobox', automation_id='ownerCombo')]}):
            with self.subTest(update=update):
                raw = textfield('PRIVATE')
                raw['field_label'] = 'PRIVATE'
                raw['target']['locator'].update(update)
                step = capture.capture_step(raw)
                self.assertEqual(step['action'], 'manual')
                self.assertNotIn('PRIVATE', json.dumps(step))
                self.assertFalse(tasks.is_recoverable(step))

    def test_text_point_to_value_refinement_preserves_step_edit(self):
        raw = textfield()
        raw.pop('value')
        raw.update(action='teach-target', recognition=dict(target='identified', action='requires_instruction',
                   result='manual', reason='text_value_unobserved'))
        first = capture.capture_step(raw)
        edits = capture.StepEdits()
        edits.store(first, '', '실행마다 받은 검색어를 사용해줘', 'required', 'Search')
        latest = capture.capture_step(textfield('new'))
        final = tasks.recorded_step(edits.apply([latest])[0])
        self.assertEqual((final['action'], final['instruction']['parameter']), ('fill', 'Search'))
        edits.forget(first['id'])
        self.assertEqual(edits.get(latest), {})


class TextfieldWorkerTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.uia = Mock(hwnd=5), Mock()
        self.notes = capture.CaptureNotes()
        self.worker = capture._Worker(self.binding, Mock(), self.notes, queue.Queue(), queue.Queue())
        self.worker.uia = self.uia
        self.uia.secure.return_value = False
        self.uia.same.side_effect = lambda first, second: first is second
        self.uia.combo_observation.return_value = None
        self.uia.value.return_value = 'old'
        self.element, self.root = object(), object()
        self.observed = dict(element=self.element, root=self.root, window=5,
            node=dict(secure=False, enabled=True, role='textfield', semantic_label='검색어',
                      available_actions=['SetValue'], locator=textfield()['target']['locator']))
        self.uia.locator.return_value = self.observed['node']['locator']
        for method, result in (('textfield_scope', self.root), ('text_readback',
                               dict(value='old', value_evidence=dict(status='observed', source='value_pattern')))):
            patched = patch.object(capture.live_action, method, return_value=result)
            patched.start()
            self.addCleanup(patched.stop)

    def test_click_then_text_change_uses_generic_recorder_with_same_step_id(self):
        first_id = self.worker.capture_action(dict(action='click'), self.observed)
        first, = self.notes.snapshot()
        self.assertEqual(first['action'], 'teach-target')
        self.worker._commit_value('00042')
        final, = self.notes.snapshot()
        self.assertEqual(final['id'], first_id)
        self.assertEqual(final['value'], '00042')
        saved = tasks.recorded_step(capture.StepEdits().apply([final])[0])
        self.assertEqual((saved['action'], saved['instruction']['value']), ('fill', '00042'))

    def test_location_only_click_does_not_fix_the_existing_text(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker.finalize_watch(True)
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'teach-target')
        saved = tasks.recorded_step(capture.StepEdits().apply([final])[0])
        self.assertEqual(saved['instruction']['mode'], 'required')
        self.assertNotIn('old', json.dumps(saved))

    def test_late_protection_discards_previously_watched_value(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        capture.live_action.textfield_scope.side_effect = capture.bridge.BridgeError('target_protected')
        self.worker._commit_value('PRIVATE')
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'manual')
        self.assertEqual(final['recognition']['reason'], 'protected_input_not_recorded')
        self.assertNotIn('PRIVATE', json.dumps(final))

    def test_unreadable_value_preserves_known_location_without_guessing(self):
        capture.live_action.text_readback.return_value = dict(value=None, value_evidence=dict(status='unavailable'))
        self.uia.value.return_value = None
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker.finalize_watch(True)
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'teach-target')
        self.assertNotIn('value', final)

    def test_combo_editor_owner_error_prevents_independent_watch(self):
        capture.live_action.textfield_scope.side_effect = capture.bridge.BridgeError('textfield_requires_combo')
        self.worker.capture_action(dict(action='click'), self.observed)
        final, = self.notes.snapshot()
        self.assertEqual(final['recognition']['reason'], 'textfield_requires_combo')
        capture.live_action.text_readback.assert_not_called()
        self.assertIsNone(self.worker.watched)

    def test_known_combo_owner_is_retained_when_its_inner_editor_is_clicked(self):
        owner = dict(element=object(), root=self.root, window=5,
                     node=dict(self.observed['node'], role='combobox', semantic_label='Family',
                               locator=dict(role='combobox', automation_id='family')))
        self.uia.combo_observation.return_value = owner
        self.worker.cache_options = Mock()
        self.worker.capture_action(dict(action='click'), self.observed)
        step, = self.notes.snapshot()
        self.assertEqual(step['target']['locator'], owner['node']['locator'])
        self.assertEqual(step['recognition']['reason'], 'combo_choice_unproven')
        self.assertEqual(capture.default_text(step), '입력받은 변수를 Family 조건에 입력해줘')
        capture.live_action.text_readback.assert_not_called()
        capture.live_action.textfield_scope.assert_not_called()


if __name__ == '__main__':
    unittest.main()
