"""Recorded positions produce editable intent even without a usable UIA target."""
import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/desktop-select/scripts'))
import desktop_select
import learned_tasks as tasks
import teach_capture as capture
from test_teach_capture import combo


class RecoveryDraftTests(unittest.TestCase):
    def test_legacy_alias_recovery_prefers_actual_instruction_and_keeps_original(self):
        first = '첫번째 요소 실행버튼 누르기'
        second = '두번째 요소 실행버튼 누르기'
        unsupported = '지금 열린 목록을 살펴보고 결정해줘'
        base = {'action': 'manual', 'reason': 'row_capture_requires_review', 'alias': first}
        plan = {'schema': 1, 'name': 'MuMu', 'exe': 'App.exe', 'window': 'App', 'steps': [
            dict(base, instruction_draft=tasks.LEGACY_INSTRUCTION_HINT),
            dict(base, instruction_draft=second), dict(base, instruction_draft=unsupported),
            dict(base, alias='목록 확인'), dict(base, reason='target_protected')]}
        original = copy.deepcopy(plan)
        resolved = tasks.resolve_saved_intents(plan)
        self.assertEqual(plan, original)
        a, b, *rest = resolved['steps']
        self.assertEqual((a['label'], a['ordinal']), (first, 1))
        self.assertEqual((b['label'], b['ordinal'], b['alias']), (second, 2, first))
        self.assertEqual(rest, original['steps'][2:])
        self.assertEqual(tasks.resolve_saved_intents(resolved), resolved)
        broken = copy.deepcopy(resolved)
        broken['steps'][1]['ordinal'] = 1
        with self.assertRaises(tasks.TaskError):
            tasks._validate(broken)

    def test_explicit_instruction_overrides_a_taught_gesture(self):
        import row_teach
        row_spec = {'version': 1, 'role': 'listitem', 'scope': [], 'size': [20, 8],
                    'boxes': {'key': [0, 0, 4, 4], 'button': [7, 0, 4, 4], 'state': [14, 0, 4, 4]},
                    'key': 'qlU=', 'button': 'Vao=', 'before': 'qlU=', 'after': 'Vao='}
        row = row_teach.raw_row_step('1번째 행 버튼 누르기', row_spec)
        edits = capture.StepEdits()
        self.assertEqual(tasks.recorded_step(edits.apply([row])[0])['action'], 'row-action')
        edits.store(row, '', '두번째 요소 실행버튼 누르기')
        command = tasks.recorded_step(edits.apply([row])[0])
        self.assertEqual((command['action'], command['ordinal']), ('row-launch', 2))
        edits.store(row, '', '이번에 필요한 항목을 검토해줘')
        draft = tasks.recorded_step(edits.apply([row])[0])
        self.assertEqual(draft['action'], 'manual')
        self.assertEqual(draft['instruction_draft'], '이번에 필요한 항목을 검토해줘')

    def test_known_value_draft_names_the_actual_field(self):
        step = capture.capture_step(combo('Family', 'Hybrid'))
        self.assertEqual(capture.default_text(step), 'Family 조건을 Hybrid로 설정해줘')

    def test_unobserved_combo_draft_requires_value_at_run(self):
        raw = combo('Family')
        raw.pop('value')
        raw.update(action='manual', recognition={'target': 'identified', 'action': 'manual',
                   'result': 'manual', 'reason': 'combo_value_unobserved'})
        step = capture.capture_step(raw)
        final = tasks.recorded_step(capture.StepEdits().apply([step])[0])
        self.assertEqual(final['instruction']['parameter'], 'Family')
        self.assertNotIn('value', final['instruction'])
        self.assertEqual(final['recorded_effect'], 'unobserved')

    def test_button_draft_uses_visible_name_and_persists_without_edit(self):
        raw = {'id': 'query', 'action': 'click', 'target': {'kind': 'element',
               'locator': {'role': 'button', 'automation_id': 'btnSearch', 'name': '조회'}},
               'recognition': {'target': 'identified', 'action': 'supported', 'result': 'manual'}}
        step = capture.capture_step(raw)
        self.assertEqual(capture.default_text(step), '조회 버튼을 눌러줘')
        final = tasks.recorded_step(capture.StepEdits().apply([step])[0])
        self.assertEqual(final['instruction']['text'], '조회 버튼을 눌러줘')
        self.assertEqual(final['target'], 'btnSearch')

    def test_untouched_generated_literal_remains_fixed_in_saved_task(self):
        step = capture.capture_step(combo('Family', 'Hybrid'))
        plan = tasks.from_recording('example', 'Example.exe', 'Example', capture.StepEdits().apply([step]))
        tasks._validate(plan)
        self.assertEqual(plan['steps'][0]['instruction']['mode'], 'fixed')
        self.assertEqual(plan['steps'][0]['instruction']['value'], 'Hybrid')

    def test_recorded_literal_outside_parser_still_saves_as_literal(self):
        step = capture.capture_step(combo('Family', 'A&B'))
        saved = capture.StepEdits().apply([step])[0]
        self.assertEqual(saved['instruction']['value'], 'A&B')

    def test_unknown_target_does_not_save_placeholder_as_instruction(self):
        step = capture.manual('capture_target_unavailable', 'lost')
        draft = capture.default_text(step)
        self.assertEqual('', draft)
        final = tasks.recorded_step(capture.StepEdits().apply([step])[0])
        self.assertEqual(final['action'], 'manual')
        self.assertNotIn('instruction_draft', final)
        self.assertNotIn('target', final)

    def test_unknown_target_named_instruction_becomes_a_live_query(self):
        step = capture.manual('capture_target_unavailable', 'lost')
        edits = capture.StepEdits()
        edits.store(step, '', 'MES Setup 조건을 N으로 설정해줘')
        raw = edits.apply([step])[0]
        final = tasks.recorded_step(raw)
        self.assertEqual((final['match'], final['target'], final['role']), ('query', 'MES Setup', 'combobox'))
        self.assertEqual(final['instruction']['value'], 'N')
        self.assertEqual(capture.review_interpretation(step, edits.get(step)), ('고정값 N', 'fixed', 'N', True))

    def test_unknown_target_can_describe_a_runtime_variable(self):
        step = capture.manual('stable_locator_unavailable', 'lost')
        edits = capture.StepEdits()
        edits.store(step, '', '입력받은 변수를 Family 조건에 입력해줘')
        final = tasks.recorded_step(edits.apply([step])[0])
        self.assertEqual((final['match'], final['instruction']['parameter']), ('query', 'Family'))

    def test_unparsed_unknown_description_is_saved_for_host_interpretation(self):
        step = capture.manual('capture_target_unavailable', 'lost')
        sentence = '이번에 요청한 제품군에 맞춰 가운데 조건을 바꿔줘'
        edits = capture.StepEdits()
        edits.store(step, '', sentence)
        final = tasks.recorded_step(edits.apply([step])[0])
        self.assertEqual(final['instruction_draft'], sentence)
        self.assertEqual(capture.review_interpretation(step, edits.get(step)), ('실행 전 설명 확인', None, '', False))

    def test_known_target_freeform_preserves_identity_for_later_interpretation(self):
        step = capture.capture_step(combo('Family', 'Hybrid'))
        edits = capture.StepEdits()
        sentence = '이번에 요청한 제품군에 맞춰 선택해줘'
        edits.store(step, '', sentence)
        final = tasks.recorded_step(edits.apply([step])[0])
        self.assertEqual(final['instruction_draft'], sentence)
        self.assertEqual((final['target'], final['match']), ('comboFamily', 'automation_id'))

    def test_temporary_unknown_diagnostic_keeps_user_text_and_variable(self):
        step = capture.manual('capture_target_unavailable', 'same-event')
        edits = capture.StepEdits()
        edits.store(step, '', '입력받은 변수를 Family 조건에 입력해줘')
        changed = copy.deepcopy(step)
        changed['recognition']['reason'] = 'stable_locator_unavailable'
        final = tasks.recorded_step(edits.apply([changed])[0])
        self.assertEqual(final['instruction']['parameter'], 'Family')

    def test_protected_and_special_menu_steps_get_no_generated_action(self):
        for reason in ('protected_input_not_recorded', 'right_click_requires_menu_teaching',
                       'combo_selection_mismatch'):
            with self.subTest(reason=reason):
                step = capture.manual(reason)
                self.assertEqual(capture.default_text(step), '')
                self.assertNotIn('instruction', capture.StepEdits().apply([step])[0])
                with self.assertRaises(tasks.TaskError):
                    capture.StepEdits().store(step, '', 'Family 조건을 Hybrid로 설정해줘')


if __name__ == '__main__':
    unittest.main()
