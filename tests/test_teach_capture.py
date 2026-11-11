"""Regression cases for human demonstrations and their executable instructions."""
import copy
import json
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/desktop-select/scripts'))
import desktop_select  # Loads the pinned bridge for the source checkout.
import teach_capture as capture
import learned_tasks


def combo(name='Mode', value='N'):
    return {'id': 'one', 'action': 'set-value',
            'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': 'combo' + name}},
            'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'},
            'field_label': name, 'value': value}


class CaptureTests(unittest.TestCase):
    def test_unrelated_inputs_and_coordinates_do_not_survive_review(self):
        notes = capture.CaptureNotes()
        for i, (role, action) in enumerate((('textfield', 'set-value'), ('table', 'right-click'))):
            step = combo()
            step.update(id=str(i), action=action, value='PRIVATE', field_label='PRIVATE')
            step['target']['locator'].update(role=role, name='PRIVATE')
            step['target']['point'] = {'x_ratio': .12345}
            notes.add(step)
        self.assertEqual([s['action'] for s in notes.snapshot()], ['manual', 'manual'])
        self.assertNotIn('PRIVATE', json.dumps(notes.snapshot()))
        self.assertNotIn('point', json.dumps(notes.snapshot()))

    def test_revised_option_keeps_intent_only_not_unsolicited_observed_value(self):
        notes = capture.CaptureNotes()
        step = combo()
        step['selection'] = {'source': 'option_click', 'value': 'N', 'result': 'unavailable'}
        notes.add(step)
        step['selection'].update(result='mismatch', observed_value='unwanted')
        step['recognition']['result'] = 'mismatch'
        notes.add(step)
        items = notes.snapshot()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['selection'], {'value': 'N', 'source': 'option_click', 'result': 'mismatch'})
        self.assertNotIn('unwanted', json.dumps(items))

    def test_limit_marks_interruption_and_keeps_steps_before_limit(self):
        notes = capture.CaptureNotes()
        for _ in range(capture.recorder.MAX_STEPS + 2):
            notes.manual('', 'review')
        self.assertEqual(len(notes.steps), capture.recorder.MAX_STEPS)
        self.assertEqual(notes.snapshot()[-1]['recognition']['reason'], 'recording_step_limit')

    def test_explicit_directive_equal_to_demonstrated_value_is_still_fixed(self):
        edits, step = capture.StepEdits(), capture.capture_step(combo())
        self.assertIn('N', capture.default_text(step))
        edits.store(step, '상태', 'Mode를 N으로 선택')
        self.assertEqual(edits.apply([step])[0]['instruction'], {'mode': 'fixed', 'value': 'N', 'text': 'Mode를 N으로 선택'})

    def test_replaced_target_cannot_inherit_another_steps_instruction(self):
        edits, step = capture.StepEdits(), capture.capture_step(combo())
        edits.store(step, '모드', 'Mode를 {condition}로 선택')
        changed = copy.deepcopy(step)
        changed['target']['locator']['automation_id'] = 'different'
        self.assertEqual(edits.apply([changed])[0]['instruction']['mode'], 'fixed')
        self.assertNotIn('parameter', edits.apply([changed])[0]['instruction'])
        self.assertEqual(edits.apply([step])[0]['instruction']['parameter'], 'condition')
        with self.assertRaises(learned_tasks.TaskError):
            edits.store(step, '', '다른 필드를 N으로 선택')
        self.assertEqual(edits.get(step)['alias'], '모드')

    def test_only_a_witnessed_bound_combo_can_be_promoted_to_point(self):
        binding, uia = Mock(hwnd=5), Mock()
        notes = capture.CaptureNotes()
        worker = capture._Worker(binding, Mock(), notes, queue.Queue(), queue.Queue())
        worker.uia = uia
        uia.secure.return_value = False
        step = combo()
        observed = {'element': object(), 'window': 5, 'node': {'secure': False, 'enabled': True, 'role': 'combobox',
                    'semantic_label': 'Mode', 'locator': step['target']['locator']}}
        worker.watched = observed
        self.assertFalse(worker._emit_point())
        worker.pointed = observed
        uia.secure.return_value = True
        self.assertFalse(worker._emit_point())
        uia.secure.return_value = False
        with patch.object(capture.bridge, '_current_combo', side_effect=capture.bridge.BridgeError('ambiguous_target')):
            self.assertFalse(worker._emit_point())
        with patch.object(capture.bridge, '_current_combo', return_value={'element': observed['element']}):
            self.assertTrue(worker._emit_point())
        row, = notes.snapshot()
        self.assertEqual((row['action'], row['field_label']), ('teach-target', 'Mode'))
        self.assertEqual(learned_tasks.recorded_step(row)['action'], 'manual')
        edits = capture.StepEdits()
        edits.store(row, '', '이 항목을 N으로 설정')
        value = learned_tasks.recorded_step(edits.apply([row])[0])
        self.assertEqual((value['target'], value['instruction']['value']), ('comboMode', 'N'))

    def test_unrecognized_or_protected_action_cannot_become_executable(self):
        for reason in ('protected_input_not_recorded', 'combo_selection_mismatch'):
            row = capture.manual(reason)
            with self.assertRaises(learned_tasks.TaskError):
                capture.StepEdits().store(row, '', '이 항목을 N으로 설정', 'fixed', 'N')
