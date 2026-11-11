"""Missed capture identities can be replaced by an explicit live semantic query."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import learned_tasks as tasks


def unknown(reason='capture_target_unavailable', **extra):
    return {'id': 'recorded-step-id', 'action': 'manual',
            'target': {'kind': 'manual', 'locator': {}},
            'recognition': {'action': 'manual', 'result': 'manual', 'reason': reason}, **extra}


def plan(*steps):
    return {'schema': 1, 'name': '설명으로 찾기', 'exe': 'Demo.exe', 'window': 'Conditions', 'steps': list(steps)}


def assigned(raw, text, **options):
    return tasks.instructed_step(raw, tasks.compile_instruction(raw, text, **options))


class SemanticRecordingTests(unittest.TestCase):
    def test_missing_combo_gets_named_query_and_fixed_instruction(self):
        raw = unknown(description='위치 녹화만 수행')
        before = deepcopy(raw)
        step = assigned(raw, 'MES SETUP 조건을 N으로 해줘')
        self.assertEqual(step, {'action': 'select', 'target': 'MES SETUP', 'match': 'query',
                               'role': 'combobox', 'recorded_effect': 'unobserved',
                               'description': '위치 녹화만 수행',
                               'instruction': {'mode': 'fixed', 'text': 'MES SETUP 조건을 N으로 해줘', 'value': 'N'}})
        self.assertEqual(raw, before)
        tasks._validate(plan(step))

    def test_unknown_combo_required_parameter_is_not_a_recorded_default(self):
        step = assigned(unknown(), '입력받은 변수를 FAMILY 조건에 입력해줘')
        self.assertEqual(step['instruction']['mode'], 'required')
        self.assertEqual(step['instruction']['parameter'], 'FAMILY')
        self.assertNotIn('value', step)
        result = tasks.replay(plan(step), 'Demo.exe', 'Conditions', {}, lambda _: self.fail('unexpected input'))
        self.assertEqual((result['status'], result['missing_parameters'], result['input_dispatched']),
                         ('needs_input', ['FAMILY'], False))
        received = []
        result = tasks.replay(plan(step), 'Demo.exe', 'Conditions', {'family': 'Hybrid'},
                              lambda actual: received.append(actual) or {
                                  'status': 'verified', 'input_dispatched': True, 'effect_verification': 'matched'})
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(received[0]['value'], 'Hybrid')
        self.assertEqual(received[0]['match'], 'query')

    def test_explicit_named_variable(self):
        step = assigned(unknown(), '입력받은 {제품군} 변수를 Family 조건에 입력해줘')
        self.assertEqual((step['target'], step['instruction']['parameter']), ('Family', '제품군'))

    def test_button_and_menu_commands_are_live_queries(self):
        for sentence, role, target in [('조회 버튼을 눌러줘', 'button', '조회'),
                                       ('Export 메뉴를 실행해줘', 'menuitem', 'Export')]:
            with self.subTest(sentence=sentence):
                step = assigned(unknown(), sentence)
                self.assertEqual((step['action'], step['role'], step['target'], step['expand']),
                                 ('open', role, target, False))
                self.assertEqual(step['instruction'], {'mode': 'recorded', 'text': sentence})
                tasks._validate(plan(step))

    def test_recorded_step_canonicalizes_provisional_instruction(self):
        raw = unknown(alias='MES')
        spec = tasks.compile_instruction(raw, 'MES Setup을 N으로 설정')
        recorded = tasks.from_recording('설명으로 찾기', 'Demo.exe', 'Conditions', [dict(raw, instruction=spec)])
        self.assertEqual(recorded['steps'], [tasks.instructed_step(raw, spec)])
        self.assertEqual(set(recorded['steps'][0]['instruction']), {'mode', 'text', 'value'})
        tasks._validate(recorded)

    def test_pronoun_without_a_named_target_is_preserved_not_guessed(self):
        step = assigned(unknown(), '이 항목을 N으로 설정')
        self.assertEqual(step, {'action': 'manual', 'reason': 'capture_target_unavailable',
                                'instruction_draft': '이 항목을 N으로 설정'})
        result = tasks.replay(plan(step), 'Demo.exe', 'Conditions', {}, lambda _: self.fail('unexpected input'))
        self.assertEqual((result['status'], result['input_dispatched']), ('needs_review', False))

    def test_explicit_alias_supplies_a_named_target_for_pronoun(self):
        step = assigned(unknown(alias='MES Setup'), '이 항목을 N으로 설정')
        self.assertEqual((step['target'], step['instruction']['value']), ('MES Setup', 'N'))

    def test_free_prose_roundtrips_then_host_can_bind_without_rerecording(self):
        sentence = '공정 조건을 이번 시험에 맞는 상태로 변경해줘'
        step = assigned(unknown(), sentence)
        self.assertEqual(step['instruction_draft'], sentence)
        p = plan(step)
        with tempfile.TemporaryDirectory() as temp:
            tasks.save_task(p, temp)
            stored = tasks.load_task(p['name'], temp)['steps'][0]
        rebound = assigned(stored, sentence, target='MES Setup', role='combobox', mode='fixed', argument='N')
        self.assertEqual((rebound['match'], rebound['target'], rebound['instruction']['value']),
                         ('query', 'MES Setup', 'N'))
        self.assertNotIn('instruction_draft', rebound)
        tasks._validate(plan(rebound))

    def test_scope_is_optional_query_text_not_a_recorded_ancestor(self):
        step = assigned(unknown(), 'MES Setup을 N으로 설정', within='Conditions')
        self.assertEqual(step['within'], 'Conditions')
        self.assertNotIn('ancestors', step)
        tasks._validate(plan(step))
        for value in ('', '  ', 123, None):
            corrupted = {**step, 'within': value}
            with self.subTest(value=value), self.assertRaises(tasks.TaskError):
                tasks._validate(plan(corrupted))

    def test_protected_and_nonrecognition_failures_cannot_promote(self):
        reasons = ('protected_input_not_recorded', 'target_protected', 'password', 'delivery_uncertain',
                   'combo_selection_mismatch', 'right_click_requires_menu_teaching',
                   'popup_needs_live_binding', 'popup_scope_unproven', 'recording_queue_full', 'hook_stopped')
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertFalse(tasks.is_recoverable(unknown(reason)))
                with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_unavailable'):
                    assigned(unknown(reason), 'MES Setup을 N으로 설정', target='MES Setup',
                             role='combobox', mode='fixed', argument='N')
        for source in (unknown(secure=True), unknown(target={'kind': 'element', 'locator': {'is_password': True}}),
                       unknown(target={'kind': 'element', 'locator': {}, 'window': {'hwnd': 100}})):
            self.assertFalse(tasks.is_recoverable(source))
            with self.assertRaises(tasks.TaskError):
                assigned(source, 'MES Setup을 N으로 설정')

    def test_recovery_does_not_copy_coordinates_or_runtime_refs(self):
        raw = unknown(target={'kind': 'region', 'locator': {}, 'point': {'x': 999, 'y': 555}, 'ref': 'stale'})
        step = assigned(raw, '조회 버튼을 눌러줘')
        self.assertNotIn('point', str(step))
        self.assertNotIn('stale', str(step))
        self.assertEqual(step['match'], 'query')

    def test_corrupted_provisional_query_or_binding_is_rejected(self):
        raw = unknown()
        spec = tasks.compile_instruction(raw, 'MES Setup을 N으로 설정')
        for changed in ({**spec, 'value': 'Y'}, {**spec, 'query': 'Family'}, {**spec, 'role': 'button'},
                        {**spec, 'coordinate': [200, 300]}):
            with self.subTest(changed=changed), self.assertRaises(tasks.TaskError):
                tasks.instructed_step(raw, changed)

    def test_query_source_requires_bound_instruction_and_supported_action(self):
        step = assigned(unknown(), 'MES Setup을 N으로 설정')
        for change in ({'instruction': None}, {'ancestors': []}, {'recorded_effect': 'matched'}, {'role': []}):
            corrupt = {**step, **change}
            with self.subTest(change=change), self.assertRaises(tasks.TaskError):
                tasks._validate(plan(corrupt))
        missing = dict(step)
        del missing['instruction']
        with self.assertRaises(tasks.TaskError):
            tasks._validate(plan(missing))

    def test_existing_identified_target_and_capability_remain_authoritative(self):
        raw = {'action': 'click', 'recognition': {'target': 'identified', 'action': 'supported'},
               'target': {'kind': 'element', 'locator': {'role': 'button', 'automation_id': 'btnSearch', 'name': '조회'}}}
        step = assigned(raw, '조회 버튼을 눌러줘')
        self.assertEqual((step['match'], step['target'], step['command_name']), ('automation_id', 'btnSearch', '조회'))
        tasks._validate(plan(step))
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_mismatch'):
            tasks.compile_instruction(raw, '엑셀 버튼을 눌러줘', target='엑셀', role='button', mode='recorded')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_action_mismatch'):
            tasks.compile_instruction(raw, '조회 버튼을 펼쳐줘')

    def test_known_free_prose_preserves_identity_but_preflights_before_any_input(self):
        raw = {'action': 'set-value', 'value': 'Old', 'field_label': 'Family',
               'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'},
               'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': 'gcboFamily',
                          'ancestors': [{'role': 'group', 'name': 'Conditions'}]}}}
        original = tasks.recorded_step(raw)
        sentence = '전에 쓰던 값으로 알아서'
        step = assigned(raw, sentence)
        self.assertEqual({key: value for key, value in step.items() if key != 'instruction_draft'}, original)
        tasks._validate(plan(step))
        first = assigned(unknown(), 'MES Setup을 N으로 설정')
        result = tasks.replay(plan(first, step), 'Demo.exe', 'Conditions', {}, lambda _: self.fail('unexpected input'))
        self.assertEqual((result['status'], result['input_dispatched'], result['stopped_at']), ('needs_review', False, 2))
        repaired = assigned(step, sentence, mode='required', argument='Family')
        self.assertEqual(repaired['target'], 'gcboFamily')
        self.assertNotIn('instruction_draft', repaired)
        self.assertNotIn('value', repaired)
        tasks._validate(plan(repaired))

    def test_known_point_draft_keeps_target_without_requiring_a_demonstrated_value(self):
        raw = {'action': 'teach-target', 'field_label': 'Family',
               'recognition': {'target': 'identified', 'action': 'requires_instruction',
                               'result': 'manual', 'reason': 'combo_choice_unproven'},
               'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': 'gcboFamily'}}}
        step = assigned(raw, '이전 설정을 참고해서 맞춰줘')
        self.assertEqual((step['target'], step['recorded_effect']), ('gcboFamily', 'unobserved'))
        tasks._validate(plan(step))

    def test_editing_recovered_query_to_free_prose_keeps_its_saved_identity(self):
        previous = assigned(unknown(), 'MES Setup을 N으로 설정')
        step = assigned(previous, '이전 시험의 설정을 참고해서 맞춰줘')
        self.assertEqual((step['target'], step['match']), ('MES Setup', 'query'))
        tasks._validate(plan(step))
        result = tasks.replay(plan(step), 'Demo.exe', 'Conditions', {}, lambda _: self.fail('unexpected input'))
        self.assertEqual((result['status'], result['input_dispatched']), ('needs_review', False))

    def test_unknown_capture_with_unsupported_action_keeps_editable_prose(self):
        step = assigned(unknown(), 'Export 메뉴를 펼쳐줘')
        self.assertEqual(step['instruction_draft'], 'Export 메뉴를 펼쳐줘')
        tasks._validate(plan(step))

    def test_host_query_mapping_preserves_prose_label_but_not_a_false_display_claim(self):
        sentence = '공정 조건을 이번 시험에 맞는 상태로 변경해줘'
        raw = unknown()
        step = assigned(raw, sentence, target='MES Setup', role='combobox', mode='fixed', argument='N')
        self.assertEqual((step['label'], step['target'], step['instruction']['text']), ('공정', 'MES Setup', sentence))
        tasks._validate(plan(step))
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
            assigned(raw, '공정 조건을 N으로 설정', target='MES Setup', role='combobox', mode='fixed', argument='P')

    def test_legacy_stored_tasks_still_validate_without_instructions(self):
        tasks._validate(plan({'action': 'select', 'role': 'combobox', 'match': 'automation_id',
                              'target': 'gcboStatus', 'value': 'N', 'parameter': 'MES Setup', 'recorded_effect': 'matched'},
                             {'action': 'open', 'role': 'button', 'match': 'name', 'target': '조회', 'expand': False},
                             {'action': 'manual', 'reason': 'capture_target_unavailable'}))


if __name__ == '__main__':
    unittest.main()
