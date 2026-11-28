"""Selected re-recording keeps surrounding work and reconnects observed windows."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import learned_tasks as tasks
import review_drafts
import teach_capture as capture
from capture_trail import finalize_contexts


def row(key, target='Run', *, role='button', value=None, scope=None):
    action = 'click' if value is None else 'set-value'
    step = dict(id=key, action=action, label=target, field_label=target,
                target=dict(kind='element', locator=dict(role=role, automation_id=target)),
                recognition=dict(target='identified', action='supported', result='checkable'))
    if value is not None:
        step['value'] = value
    if scope is not None:
        step['_capture_window'] = deepcopy(scope)
    return capture.StepEdits().review_rows([step])[0]


class RerecordReplacementTests(unittest.TestCase):
    def test_one_to_many_and_many_to_one_keep_prefix_suffix_and_input_documents(self):
        source = [row('before'), row('a'), row('b'), row('after')]
        original = deepcopy(source)
        fresh = [row('x', 'View'), row('y', 'Open')]
        fresh_before = deepcopy(fresh)
        merged = review_drafts.replace_rows(source, ['a'], fresh)
        self.assertEqual([r['step']['id'] for r in merged], ['before', 'x', 'y', 'b', 'after'])
        self.assertEqual(merged[0], source[0])
        self.assertEqual(merged[-1], source[-1])
        merged[0]['form']['text'] = 'changed'
        self.assertEqual(source, original)
        self.assertEqual(fresh, fresh_before)
        merged = review_drafts.replace_rows(source, ['b', 'a'], [row('x')])
        self.assertEqual([r['step']['id'] for r in merged], ['before', 'x', 'after'])

    def test_empty_noncontiguous_missing_selection_and_step_limit_reject_without_edits(self):
        source = [row('a'), row('b'), row('c')]
        before = deepcopy(source)
        for selection, replacement in (([], [row('x')]), (['a', 'c'], [row('x')]),
                                       (['absent'], [row('x')]), (['b'], [])):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                review_drafts.replace_rows(source, selection, replacement)
        with patch.object(tasks, 'MAX_STEPS', 3), self.assertRaises(ValueError):
            review_drafts.replace_rows(source, ['b'], [row('x'), row('y')])
        with self.assertRaises(ValueError):
            review_drafts.replace_rows(source, ['b'], [row('a')])
        self.assertEqual(source, before)

    def test_required_variable_moves_only_to_unique_same_field(self):
        source = row('field', 'Equip ID', role='textfield', value='OLD')
        source['form'] = dict(alias='', text='Equip ID 입력란에 입력받은 {장비} 변수를 입력해줘',
                              mode='required', argument='장비')
        fresh = [row('view', 'View'), row('input', 'Equip ID', role='textfield', value='NEW')]
        merged = review_drafts.replace_rows([source], ['field'], fresh)
        self.assertEqual(merged[0]['form'], fresh[0]['form'])
        self.assertEqual(merged[1]['form'], source['form'])
        steps = capture.StepEdits()
        compiled = steps.apply(steps.restore_rows(merged))
        self.assertEqual(tasks.recorded_step(compiled[1])['instruction']['parameter'], '장비')

    def test_newly_edited_form_and_new_recorded_literal_take_precedence(self):
        source = row('old', 'Equip ID', role='textfield', value='OLD')
        fresh = row('new', 'Equip ID', role='textfield', value='NEW')
        merged = review_drafts.replace_rows([source], ['old'], [fresh])
        self.assertEqual(merged[0]['form'], fresh['form'])
        source['form'].update(mode='required', argument='장비', text='Equip ID 입력란에 {장비} 변수를 입력해줘')
        fresh['form'].update(mode='fixed', argument='EDITED', text='Equip ID 입력란을 EDITED로 설정해줘')
        merged = review_drafts.replace_rows([source], ['old'], [fresh])
        self.assertEqual(merged[0]['form'], fresh['form'])

    def test_duplicate_target_or_different_owner_does_not_copy_variable(self):
        source = row('old', 'Equip ID', role='textfield', value='OLD')
        source['form'].update(mode='required', argument='장비', text='Equip ID 입력란에 {장비} 변수를 입력해줘')
        fresh = [row('a', 'Equip ID', role='textfield', value='A'),
                 row('b', 'Equip ID', role='textfield', value='B')]
        self.assertEqual(review_drafts.replace_rows([source], ['old'], fresh), fresh)
        child = row('child', 'Equip ID', role='textfield', value='NEW',
                    scope=dict(exe='Other.exe', title='Detail', mode='existing'))
        self.assertEqual(review_drafts.replace_rows([source], ['old'], [child]), [child])

    def test_same_field_id_in_different_ancestor_does_not_copy_instruction(self):
        source = row('old', 'Comment', role='textfield', value='OLD')
        source['step']['target']['locator']['ancestors'] = [dict(role='group', automation_id='left')]
        source['form'].update(mode='required', argument='Memo', text='Comment 입력란에 {Memo} 변수를 입력해줘')
        fresh = row('new', 'Comment', role='textfield', value='NEW')
        fresh['step']['target']['locator']['ancestors'] = [dict(role='group', automation_id='right')]
        self.assertEqual(review_drafts.replace_rows([source], ['old'], [fresh]), [fresh])

    def test_saved_legacy_value_and_parameter_reach_matching_new_field(self):
        source = row('old', 'Equip ID', role='textfield', value='OLD')
        compiled = tasks.recorded_step(source['step'])
        compiled['parameter'] = 'Equipment'
        plan = dict(schema=1, name='Example', exe='Demo.exe', window='Main', steps=[compiled])
        saved = review_drafts.rows_from_task(plan)
        fresh = row('new', 'Equip ID', role='textfield', value='NEW')
        merged = review_drafts.replace_rows(saved, ['saved-1'], [fresh])
        result = tasks.recorded_step(merged[0]['step'])
        self.assertEqual((result['value'], result['parameter']), ('OLD', 'Equipment'))
        self.assertNotEqual(result['recorded_effect'], 'matched')

    def test_single_corrected_control_preserves_explicit_alias_and_parameter(self):
        source = row('old', 'OldMode', role='combobox', value='OLD')
        source['form'] = dict(alias='옵션', text='옵션 조건에 {Mode} 변수를 입력해줘', mode='required', argument='Mode')
        fresh = row('new', 'CorrectedMode', role='combobox', value='NEW')
        detail = dict(exe='Demo.exe', title='Detail', related=True, after_id='old')
        merged = review_drafts.replace_rows([source, row('read', 'Read', scope=detail)], ['old'], [fresh])
        self.assertEqual(merged[0]['form'], source['form'])
        self.assertEqual(merged[-1]['step']['_capture_window']['after_id'], 'new')

    def test_read_instruction_survives_reteaching_the_same_unedited_field(self):
        scope = dict(exe='Demo.exe', title='Detail', related=True, after_id='open')
        source = row('old', 'Comment', role='textfield', value='Example', scope=scope)
        source['form']['text'] = 'Comment 내용 읽어줘'
        fresh = row('new', 'Comment', role='textfield')
        fresh['step'].update(action='teach-target', recognition=dict(
            target='identified', action='requires_instruction', result='manual', reason='text_value_unobserved'))
        fresh['form'] = capture.StepEdits().review_rows([fresh['step']])[0]['form']
        merged = review_drafts.replace_rows([row('open', 'Open'), source], ['old'], [fresh])
        self.assertEqual(merged[-1]['form'], source['form'])

    def test_optional_target_inspection_failure_preserves_new_take(self):
        source, fresh = row('old'), row('new')
        with patch.object(capture.StepEdits, 'compile', side_effect=capture.bridge.BridgeError('unavailable')):
            merged = review_drafts.replace_rows([source], ['old'], [fresh])
        self.assertEqual(merged, [fresh])

    def test_invalid_point_draft_can_be_replaced_when_its_conversion_fails(self):
        source = row('old', 'Comment', role='textfield')
        source['step'].update(action='teach-target', recognition=dict(
            target='identified', action='requires_instruction', result='manual', reason='text_value_unobserved'))
        fresh = row('new', 'Read')
        with patch.object(tasks, '_convert', side_effect=tasks.TaskError('invalid_point_target')):
            merged = review_drafts.replace_rows([source], ['old'], [fresh])
        self.assertEqual(merged, [fresh])

    def test_replacement_inside_existing_detail_inherits_original_window_route(self):
        detail = dict(exe='Demo.exe', title='Detail', related=True, after_id='open')
        source = [row('open', 'Open'), row('old', 'Comment', scope=detail), row('close', 'Exit', scope=detail)]
        fresh = [row('new1', 'Comment'), row('new2', 'Read')]
        merged = review_drafts.replace_rows(source, ['old'], fresh)
        for entry in merged[1:3]:
            self.assertEqual(entry['step']['_capture_window'], detail)
        self.assertEqual(merged[-1], source[-1])

    def test_matching_opener_relinks_all_following_detail_steps_and_final_numbers(self):
        detail = dict(exe='Demo.exe', title='Detail', related=True, after_id='open')
        source = [row('before', 'Before'), row('open', 'Open'),
                  row('read', 'Comment', scope=detail), row('close', 'Exit', scope=detail)]
        fresh = [row('view', 'View'), row('new-open', 'Open')]
        merged = review_drafts.replace_rows(source, ['open'], fresh)
        self.assertEqual(merged[3]['step']['_capture_window']['after_id'], 'new-open')
        self.assertEqual(merged[4]['step']['_capture_window']['after_id'], 'new-open')
        final = finalize_contexts([r['step'] for r in merged])
        self.assertEqual(final[3]['window_context']['after_step'], 3)

    def test_captured_child_window_proves_new_opener_without_old_target_match(self):
        old_detail = dict(exe='Demo.exe', title='Detail', related=True, after_id='old-open')
        new_detail = dict(old_detail, after_id='new-open')
        source = [row('old-open', 'Old target'), row('close', 'Exit', scope=old_detail)]
        fresh = [row('view', 'View'), row('new-open', 'New target'), row('read', 'Comment', scope=new_detail)]
        merged = review_drafts.replace_rows(source, ['old-open'], fresh)
        self.assertEqual(merged[-1]['step']['_capture_window']['after_id'], 'new-open')
        self.assertEqual(merged[2]['step']['_capture_window'], new_detail)

    def test_uncertain_opener_stays_reviewable_without_blind_last_action_choice(self):
        scope = dict(exe='Demo.exe', title='Detail', related=True, after_id='old-open')
        source = [row('old-open', 'Missing'), row('read', 'Comment', scope=scope)]
        fresh = [row('query', 'View'), row('open1', 'Open'), row('open2', 'Open')]
        merged = review_drafts.replace_rows(source, ['old-open'], fresh)
        current = merged[-1]['step']['_capture_window']
        self.assertNotIn('after_id', current)
        self.assertNotIn('related', current)
        self.assertEqual(current['_openers'], ('query', 'open1', 'open2'))
        with self.assertRaises(ValueError):
            finalize_contexts([r['step'] for r in merged])

    def test_two_new_origins_for_same_detail_do_not_create_false_proof(self):
        scope = dict(exe='Demo.exe', title='Detail', related=True, after_id='old-open')
        source = [row('old-open', 'Missing'), row('close', 'Exit', scope=scope)]
        fresh = [row('open1', 'Open'), row('read1', 'Comment', scope=dict(scope, after_id='open1')),
                 row('open2', 'Open'), row('read2', 'Comment', scope=dict(scope, after_id='open2'))]
        merged = review_drafts.replace_rows(source, ['old-open'], fresh)
        self.assertIn('_openers', merged[-1]['step']['_capture_window'])


if __name__ == '__main__':
    unittest.main()
