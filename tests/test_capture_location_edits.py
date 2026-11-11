"""0.4 recording locations remain editable without an observed combo value."""
import copy
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/desktop-select/scripts'))
import desktop_select as app
import learned_tasks as tasks
import teach_capture as capture
from test_teach_capture import combo


class LocationEditsTests(unittest.TestCase):
    def test_known_unreadable_combo_keeps_location_for_user_instruction(self):
        raw = combo()
        raw.pop('value')
        raw.update(action='manual', recognition={'target': 'identified', 'action': 'manual',
                   'result': 'manual', 'reason': 'combo_value_unobserved'})
        step = capture.capture_step(raw)
        self.assertEqual((step['id'], step['action']), ('one', 'teach-target'))
        edits = capture.StepEdits()
        edits.store(step, '', '이 항목을 N으로 설정')
        result = tasks.recorded_step(edits.apply([step])[0])
        self.assertEqual((result['target'], result['instruction']['value']), ('comboMode', 'N'))

    def test_peer_metadata_and_point_to_value_revision_keep_explicit_variable(self):
        raw = combo()
        raw.pop('value')
        raw.update(action='teach-target', recognition={'target': 'identified', 'action': 'requires_instruction',
                   'result': 'manual', 'reason': 'combo_choice_unproven'})
        step = capture.capture_step(raw)
        edits = capture.StepEdits()
        edits.store(step, '제품군', '이 항목을 {Family}로 설정')
        changed = capture.capture_step(combo(value='Hybrid'))
        changed['target']['locator'].update(name='temporary provider caption', runtime_id=[1, 20],
                                            bounds={'x': 300, 'y': 400})
        final = tasks.recorded_step(edits.apply([changed])[0])
        self.assertEqual(final['instruction']['parameter'], 'Family')
        self.assertNotIn('value', final)
        self.assertEqual(edits.form(changed)[0], '이 항목을 {Family}로 설정')

    def test_invalid_draft_survives_switch_and_can_be_completed_structurally(self):
        step = capture.capture_step(combo())
        edits = capture.StepEdits()
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_requires_structure'):
            edits.store(step, '제품군', '실행할 때 요청한 제품군으로')
        self.assertEqual(edits.form(step)[0], '실행할 때 요청한 제품군으로')
        self.assertEqual(edits.alias(step), '제품군')
        with self.assertRaises(tasks.TaskError):
            edits.apply([step])
        edits.store(step, '제품군', '실행할 때 요청한 제품군으로', 'required', 'Family')
        self.assertEqual(edits.apply([step])[0]['instruction']['parameter'], 'Family')
        edits.forget(step['id'])
        self.assertEqual(edits.get(step), {})
        self.assertEqual(edits.alias(step), '')

    def test_explicit_alias_link_binds_screen_name_to_recorded_control(self):
        step = capture.capture_step(combo())
        sentence = 'MES Setup조건을 N으로 설정해줘'
        subject = tasks.instruction_subject(sentence)
        self.assertEqual(subject, 'MES Setup')
        edits = capture.StepEdits()
        edits.store(step, subject, sentence)
        result = tasks.recorded_step(edits.apply([step])[0])
        self.assertEqual(result['target'], 'comboMode')
        self.assertEqual(result['instruction']['value'], 'N')
        self.assertEqual(tasks.instruction_subject('입력받은 변수를 FAMILY조건에 입력해줘'), 'FAMILY')

    def test_drafts_are_valid_sentences_and_do_not_invent_unknown_value(self):
        for raw in (combo(), dict(combo(), action='click', target={'kind': 'element',
                     'locator': {'role': 'button', 'automation_id': 'search'}})):
            step = capture.capture_step(raw)
            draft = capture.default_text(step)
            self.assertIsNotNone(tasks.compile_instruction(step, draft))
        step = combo()
        step.pop('value')
        step.update(action='teach-target', recognition={'target': 'identified', 'action': 'requires_instruction',
                    'result': 'manual', 'reason': 'combo_choice_unproven'})
        self.assertEqual(capture.default_text(capture.capture_step(step)), '')

    def test_clicked_combo_is_immediately_recorded_before_value_readback(self):
        binding, uia = Mock(hwnd=5), Mock()
        notes = capture.CaptureNotes()
        worker = capture._Worker(binding, Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = uia
        uia.same.return_value = True
        uia.secure.return_value = False
        observed = {'element': object(), 'window': 5, 'node': {'secure': False, 'enabled': True,
                    'role': 'combobox', 'semantic_label': 'Mode', 'locator': combo()['target']['locator']}}
        worker.combo_for = Mock(return_value=observed)

        def watch(*args):
            worker.watched = observed

        with patch.object(capture.recorder.CaptureWorker, 'capture_action', side_effect=watch):
            step_id = worker.capture_action({'action': 'click'}, observed)
        step, = notes.snapshot()
        self.assertEqual((step['id'], step['action']), (step_id, 'teach-target'))
        with patch.object(capture.bridge, '_current_combo', side_effect=AssertionError('no new full tree')):
            self.assertTrue(worker._emit_point())
        self.assertEqual(len(notes.snapshot()), 1)
        self.assertEqual(notes.snapshot()[0]['id'], step_id)

    def test_different_ancestor_does_not_inherit_draft_or_alias(self):
        step = capture.capture_step(combo())
        edits = capture.StepEdits()
        edits.store(step, 'alias', '이 항목을 {Family}로 설정')
        changed = copy.deepcopy(step)
        changed['target']['locator']['ancestors'] = [{'role': 'group', 'automation_id': 'different'}]
        self.assertEqual(edits.alias(changed), '')
        self.assertNotIn('instruction', edits.apply([changed])[0])

    def test_recorder_adapter_uses_shared_local_label_resolution(self):
        uia = capture._CaptureUIA.__new__(capture._CaptureUIA)
        uia.binding = Mock()
        with patch.object(app, 'field_label', return_value={'semantic_label': 'Family', 'label_source': 'sibling_geometry'}) as read:
            result = uia.semantic_label('combo', 'root')
        read.assert_called_once_with(uia.binding, uia, 'combo', 'root')
        self.assertEqual(result['semantic_label'], 'Family')


if __name__ == '__main__':
    unittest.main()
