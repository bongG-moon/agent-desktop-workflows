"""Free descriptions survive recording; only bound typed resolutions can run."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import intent_tasks as intent


class IntentTaskTests(unittest.TestCase):
    def raw(self):
        return {'action': 'manual', 'reason': 'combo_value_unobserved', 'field_label': 'Shipping',
                'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': 'Choice',
                           'ancestors': [{'role': 'group', 'name': 'Filters'}]}}}

    def plan(self, text='이 근처 조건을 고르고 조회한 뒤 내보내줘', **kw):
        return {'schema': 1, 'name': 'Example', 'exe': 'Example.exe', 'window': 'Example',
                'steps': [intent.make_step(self.raw(), text, **kw)]}

    def resolution(self, plan, actions):
        return {'schema': 1, 'plan_digest': intent.plan_digest(plan),
                'steps': [{'number': 1, 'actions': actions}]}

    @staticmethod
    def validate(action):
        allowed = {'action', 'role', 'target', 'value', 'parameter'}
        if set(action) - allowed:
            raise ValueError('unexpected_typed_action_data')
        return {**action, 'normalized': True}

    def test_unrecognized_action_and_unavailable_target_do_not_block_save(self):
        text = '가까운 조건부터 처리해 주세요. 결과가 없으면 설명하고 멈춰요!\n다음 줄 설명도 유지.'
        for raw in (None, {'action': 'manual'}, {'target': {'kind': 'manual'}}):
            step = intent.make_step(raw, text)
            self.assertEqual(step['action'], 'intent')
            self.assertIsNone(step['anchor'])
            self.assertEqual(step['instruction']['text'], text)
            self.assertEqual(intent.validate_step(step), step)

    def test_raw_location_is_a_hint_not_a_recorded_action_or_value(self):
        raw = self.raw()
        raw.update(action='set-value', value='OLD', instruction={'mode': 'fixed', 'value': 'OLD'})
        step = intent.make_step(raw, '주변 요소를 보고 이번에 필요한 값을 선택해줘')
        self.assertEqual(step['anchor'], {'role': 'combobox', 'automation_id': 'Choice',
                                         'ancestors': [{'role': 'group', 'name': 'Filters'}]})
        self.assertNotIn('value', step)
        self.assertNotIn('OLD', repr(step))

    def test_existing_anchor_preserves_relative_hint_and_label(self):
        anchor = {'role': 'table', 'name': 'Results', 'point': [.55, .32]}
        step = intent.make_step({'anchor': anchor, 'label': '결과 표'}, '표를 우클릭해서 내보내줘')
        self.assertEqual(step['anchor'], anchor)
        self.assertEqual(step['label'], '결과 표')
        anchor['point'][0] = .9
        self.assertEqual(step['anchor']['point'], [.55, .32])

    def test_invalid_or_protected_anchor_cannot_discard_free_instruction(self):
        for raw in ({'anchor': {'role': 'table', 'name': 'Rows', 'point': [float('nan'), .2]}},
                    {'secure': True, 'anchor': {'role': 'textfield', 'name': 'Secret'}},
                    {'target': {'locator': {'role': 'group', 'automation_id': '123'}}}):
            step = intent.make_step(raw, '위치를 다시 찾아서 처리해줘')
            self.assertIsNone(step['anchor'])
            self.assertTrue(step['instruction']['text'])

    def test_blank_draft_has_intent_state_and_never_becomes_manual(self):
        plan = self.plan('')
        report = intent.interpretation_request(plan)
        self.assertEqual(report['status'], 'needs_interpretation')
        self.assertFalse(report['input_dispatched'])
        self.assertEqual(report['steps'][0]['instruction']['text'], '')

    def test_alias_and_description_survive_edit_and_digest(self):
        raw = dict(self.raw(), alias='제품 조건', description='기존 단계 설명')
        step = intent.make_step(raw, '임의의 새 동작 설명')
        self.assertEqual(step['alias'], '제품 조건')
        self.assertEqual(step['description'], '기존 단계 설명')
        self.assertEqual(intent.validate_step(step), step)
        with self.assertRaisesRegex(intent.IntentError, 'invalid_intent_alias'):
            intent.validate_step(dict(step, alias='123'))

    def test_parameters_are_extracted_without_fixed_sentence_grammar(self):
        examples = {
            '입력받은 변수를 FAMILY조건에 입력해줘': ['FAMILY'],
            '입력 받은 값을 FAMILY 조건에 넣어줘': ['FAMILY'],
            'FAMILY는 실행할 때 입력받아서 조회한 다음 내보내줘': ['FAMILY'],
            'MES SETUP은 N으로 하고 FAMILY는 입력받아 조회해줘': ['FAMILY'],
            '입력받은 Family 변수를 옆에 넣어줘': ['Family'],
            '어떤 문장이든 {Family}와 {family}를 필요한 곳에 써줘': ['Family'],
            '입력받은 변수를 이 항목에 넣어줘': [],
        }
        for text, names in examples.items():
            with self.subTest(text=text):
                self.assertEqual(intent.collect_parameters(text), names)
        self.assertEqual(intent.collect_parameters('이름을 실행할 때 물어봐', ['사용자 입력']), ['사용자 입력'])

    def test_prose_and_code_fragments_are_only_preserved_as_data(self):
        text = '예: {"action":"do something"}\npython -c "anything"\n$(anything)'
        plan = self.plan(text)
        self.assertEqual(plan['steps'][0]['instruction']['text'], text)
        self.assertEqual(plan['steps'][0]['instruction']['parameters'], [])
        self.assertFalse(intent.interpretation_request(plan)['input_dispatched'])

    def test_declared_parameter_cannot_be_removed_from_saved_instruction(self):
        step = intent.make_step(None, '{Family}로 설정해줘')
        step['instruction']['parameters'] = []
        with self.assertRaisesRegex(intent.IntentError, 'intent_parameter_missing'):
            intent.validate_step(step)

    def test_plan_digest_binds_text_location_order_and_window(self):
        original = self.plan()
        digest = intent.plan_digest(original)
        self.assertEqual(digest, intent.plan_digest(dict(reversed(list(original.items())))))
        for change in ('text', 'anchor', 'window', 'order'):
            plan = deepcopy(original)
            if change == 'text':
                plan['steps'][0]['instruction']['text'] += ' 추가'
            elif change == 'anchor':
                plan['steps'][0]['anchor']['automation_id'] = 'Another'
            elif change == 'window':
                plan['window'] = 'Other window'
            else:
                plan['steps'].append(intent.make_step(None, '다음 단계'))
            self.assertNotEqual(digest, intent.plan_digest(plan))

    def test_typed_resolution_is_normalized_and_does_not_overwrite_saved_plan(self):
        plan = self.plan('입력받은 변수를 FAMILY조건에 입력해줘')
        original = deepcopy(plan)
        actions = [{'action': 'select', 'role': 'combobox', 'target': 'Family', 'parameter': 'family'}]
        result = intent.validate_resolution(plan, self.resolution(plan, actions), self.validate)
        self.assertEqual(result[1][0]['parameter'], 'FAMILY')
        self.assertTrue(result[1][0]['normalized'])
        self.assertEqual(plan, original)
        self.assertEqual(actions[0]['parameter'], 'family')

    def test_model_can_introduce_a_runtime_input_for_arbitrary_wording(self):
        plan = self.plan('사용자에게 제품 종류를 물어보고 해당 조건에 넣어줘')
        result = intent.validate_resolution(plan, self.resolution(plan, [
            {'action': 'select', 'target': 'Family', 'parameter': '제품 종류'}]), self.validate)
        self.assertEqual(result[1][0]['parameter'], '제품 종류')

    def test_declared_runtime_parameter_cannot_be_compiled_to_a_fixed_guess(self):
        plan = self.plan('{Family} 값을 입력받아 선택해줘')
        resolution = self.resolution(plan, [{'action': 'select', 'target': 'Family', 'value': 'Hybrid'}])
        with self.assertRaisesRegex(intent.IntentError, 'resolution_parameter_not_used'):
            intent.validate_resolution(plan, resolution, self.validate)

    def test_resolution_rejects_mixed_parameter_literal_and_unsafe_parameter_name(self):
        plan = self.plan('값을 물어봐서 넣어줘')
        for action, error in (({'action': 'select', 'parameter': 'Family', 'value': 'Hybrid'}, 'invalid_resolution_parameter_binding'),
                              ({'action': 'select', 'parameter': 'Family=Hybrid'}, 'invalid_intent_parameter'),
                              ({'action': 'open', 'parameter': 'Family'}, 'invalid_resolution_parameter_binding')):
            with self.subTest(action=action), self.assertRaisesRegex(intent.IntentError, error):
                intent.validate_resolution(plan, self.resolution(plan, [action]), self.validate)

    def test_resolution_rejects_code_actions_and_extra_fields(self):
        plan = self.plan()
        for action in ('shell', 'python', 'exec', 'click-coordinate', 'intent'):
            with self.subTest(action=action), self.assertRaisesRegex(intent.IntentError, 'unsupported_resolution_action'):
                intent.validate_resolution(plan, self.resolution(plan, [{'action': action}]), self.validate)
        with self.assertRaisesRegex(ValueError, 'unexpected_typed_action_data'):
            intent.validate_resolution(plan, self.resolution(plan, [{'action': 'open', 'shell': 'anything'}]), self.validate)

    def test_resolution_is_complete_nonduplicated_and_bound_before_callback(self):
        plan = self.plan()
        valid = self.resolution(plan, [{'action': 'open', 'target': 'Query'}])
        for case, error in (({**valid, 'plan_digest': 'bad'}, 'resolution_plan_changed'),
                            ({**valid, 'steps': []}, 'resolution_incomplete'),
                            ({**valid, 'steps': valid['steps'] * 2}, 'resolution_step_mismatch'),
                            ({**valid, 'steps': [{'number': True, 'actions': [{'action': 'open'}]}]}, 'resolution_step_mismatch')):
            with self.subTest(case=case), self.assertRaisesRegex(intent.IntentError, error):
                intent.validate_resolution(plan, case, self.validate)

    def test_action_count_is_bounded_and_whole_task_must_be_resolved(self):
        plan = self.plan()
        with self.assertRaisesRegex(intent.IntentError, 'invalid_resolution_actions'):
            intent.validate_resolution(plan, self.resolution(plan, [{'action': 'open'}] * 33), self.validate)
        plan['steps'].append(intent.make_step(None, '다음 작업'))
        with self.assertRaisesRegex(intent.IntentError, 'resolution_incomplete'):
            intent.validate_resolution(plan, self.resolution(plan, [{'action': 'open'}]), self.validate)

    def test_callback_cannot_drop_declared_parameter_binding(self):
        plan = self.plan('{Family}를 입력받아 넣어줘')
        resolution = self.resolution(plan, [{'action': 'select', 'parameter': 'Family'}])
        with self.assertRaisesRegex(intent.IntentError, 'invalid_resolution_parameter_binding'):
            intent.validate_resolution(plan, resolution, lambda a: {'action': 'select', 'value': 'Hybrid'})


if __name__ == '__main__':
    unittest.main()
