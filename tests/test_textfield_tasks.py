"""Recorded text inputs and combo selections keep their own literal variable rules."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import learned_tasks as tasks
import recorded_plan as plans


def raw_text(value='old search', *, point=False):
    raw = dict(action='set-value', field_label='Search', value=value,
               target=dict(kind='element', locator=dict(role='textfield', automation_id='queryInput',
                           ancestors=[dict(role='group', automation_id='conditions')])),
               recognition=dict(target='identified', action='supported', result='checkable'))
    if point:
        raw.pop('value')
        raw.update(action='teach-target', recognition=dict(target='identified', action='requires_instruction',
                                                          result='manual', reason='text_value_unobserved'))
    return raw


def raw_combo():
    raw = raw_text('W')
    raw['field_label'] = 'Status'
    raw['target']['locator'].update(role='combobox', automation_id='statusField')
    return raw


def bound(raw, sentence, **options):
    return dict(raw, instruction=tasks.compile_instruction(raw, sentence, **options))


def task(*raw_steps):
    result = tasks.from_recording('Variable inputs', 'Example.exe', 'Query window', list(raw_steps))
    tasks._validate(result)
    return result


def matched(step):
    return dict(status='verified', input_dispatched=True, effect_verification='matched')


class TextfieldTaskTests(unittest.TestCase):
    def replay(self, source, values=None, invoke=matched):
        return tasks.replay(source, 'Example.exe', 'Query window', values or {}, invoke)

    def test_recorded_text_preserves_quotes_unicode_and_leading_zero(self):
        value = '00123 "한글" = $value'
        source = task(raw_text(value))
        step = source['steps'][0]
        self.assertEqual((step['action'], step['role'], step['value']), ('fill', 'textfield', value))
        self.assertEqual(step['parameter'], 'Search')
        with tempfile.TemporaryDirectory() as directory:
            tasks.save_task(source, directory)
            self.assertEqual(tasks.load_task(source['name'], directory), source)

    def test_location_only_input_requires_instruction(self):
        raw = raw_text(point=True)
        self.assertEqual(task(raw)['steps'][0]['action'], 'manual')
        source = task(bound(raw, '입력받은 변수를 Search 입력란에 입력해줘'))
        step = source['steps'][0]
        self.assertEqual((step['action'], step['recorded_effect']), ('fill', 'unobserved'))
        self.assertEqual(step['instruction']['parameter'], 'Search')

    def test_generic_required_draft_can_rename_parameter_without_rewriting_sentence(self):
        for raw, sentence in (
                (raw_text(), '입력받은 변수를 Search 입력란에 입력해줘'),
                (raw_text(point=True), '입력받은 변수를 Search 입력란에 입력해줘'),
                (raw_combo(), '입력받은 변수를 Status 조건에 입력해줘'),
                (dict(action='manual', reason='capture_target_unavailable'),
                 '입력받은 변수를 Search 입력란에 입력해줘')):
            with self.subTest(raw=raw):
                spec = tasks.compile_instruction(raw, sentence, mode='required', argument='SearchText')
                step = tasks.instructed_step(raw, spec)
                self.assertEqual(step['instruction'], dict(mode='required', text=sentence, parameter='SearchText'))
                plans.compile_plan(dict(schema=1, name='Renamed', exe='Example.exe', window='Query window', steps=[step]))

    def test_explicit_parameter_name_and_fixed_value_conflicts_still_rejected(self):
        for raw in (raw_text(), dict(action='manual', reason='capture_target_unavailable')):
            for sentence, mode, argument in (
                    ('입력받은 {Search} 변수를 Search 입력란에 입력해줘', 'required', 'Other'),
                    ('{Search}를 Search 입력란에 입력해줘', 'required', 'Other'),
                    ('Search 입력란을 {Search}로 설정해줘', 'required', 'Other'),
                    ('Search 입력란을 N으로 설정해줘', 'fixed', 'Other')):
                with self.subTest(raw=raw, sentence=sentence):
                    with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
                        tasks.compile_instruction(raw, sentence, mode=mode, argument=argument)

    def test_missing_input_variable_blocks_even_earlier_fixed_combo(self):
        source = task(bound(raw_combo(), 'Status를 N으로 설정해줘'),
                      bound(raw_text(), '입력받은 변수를 Search 입력란에 입력해줘'))
        result = self.replay(source, invoke=lambda _: self.fail('no partial input'))
        self.assertEqual((result['status'], result['input_dispatched'], result['missing_parameters']),
                         ('needs_input', False, ['Search']))

    def test_subsequent_runs_take_new_values_without_changing_original(self):
        source = task(bound(raw_combo(), '입력받은 변수를 Status 조건에 입력해줘'),
                      bound(raw_text(), '입력받은 변수를 Search 입력란에 입력해줘'))
        before = deepcopy(source)
        for status, search in [('N', '0012 "첫 검색"'), ('P', '한글 = 0007')]:
            seen = []
            result = self.replay(source, dict(Status=status, Search=search),
                                 lambda step: seen.append((step['action'], step['value'])) or matched(step))
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(seen, [('select', status), ('fill', search)])
        self.assertEqual(source, before)
        self.assertNotIn('old search', json.dumps(source))

    def test_empty_text_is_explicit_clear_and_not_missing(self):
        source = task(bound(raw_text(), '입력받은 변수를 Search 입력란에 입력해줘'))
        seen = []
        parsed = tasks.parse_overrides(['Search='])
        self.assertEqual(parsed, {'Search': ''})
        self.assertEqual(self.replay(source, parsed, lambda step: seen.append(step['value']) or matched(step))['status'], 'verified')
        self.assertEqual(seen, [''])
        fixed = task(bound(raw_text(), 'Search 입력란을 비워줘'))
        self.assertEqual(fixed['steps'][0]['instruction']['value'], '')
        self.assertEqual(task(raw_text(''))['steps'][0]['value'], '')

    def test_shared_text_and_combo_parameter_empty_fails_before_any_input(self):
        source = task(bound(raw_text(), '입력받은 {Value} 변수를 Search 입력란에 입력해줘'),
                      bound(raw_combo(), '입력받은 {Value} 변수를 Status 조건에 입력해줘'))
        with self.assertRaisesRegex(tasks.TaskError, 'invalid_override'):
            self.replay(source, {'Value': ''}, lambda _: self.fail('no partial input'))

    def test_quoted_literal_draft_keeps_exact_text_and_empty(self):
        for value in ('', '0012', '  한글 "검색"  ', '$x; command()'):
            with self.subTest(value=value):
                sentence = 'Search 입력란을 ' + json.dumps(value, ensure_ascii=False) + '로 설정해줘'
                source = task(bound(raw_text(), sentence))
                self.assertEqual(source['steps'][0]['instruction']['value'], value)

    def test_text_value_rejects_control_characters_and_invalid_codepoints(self):
        for value in ('line\nbreak', 'tab\ttext', '\x00', '\ud800', '가' * 4000):
            with self.subTest(value=repr(value)):
                self.assertFalse(tasks.valid_value(value, 'textfield'))
                with self.assertRaises(tasks.TaskError):
                    tasks.compile_instruction(raw_text(), '', mode='fixed', argument=value)
        self.assertFalse(tasks.valid_value('', 'combobox'))
        self.assertFalse(tasks.valid_value('N', 'password'))

    def test_unavailable_provider_value_is_not_recorded_as_empty_clear(self):
        raw = raw_text('')
        raw['recognition']['result'] = 'manual'
        step = task(raw)['steps'][0]
        self.assertEqual((step['action'], step['reason']), ('manual', 'text_value_unobserved'))
        self.assertNotIn('value', step)

    def test_protected_text_is_never_stored_or_made_executable(self):
        for location in ('raw', 'target', 'locator', 'ancestor'):
            raw = raw_text('private test value')
            nodes = dict(raw=raw, target=raw['target'], locator=raw['target']['locator'],
                         ancestor=raw['target']['locator']['ancestors'][0])
            nodes[location]['is_password'] = True
            with self.subTest(location=location):
                step = task(raw)['steps'][0]
                self.assertEqual(step, dict(action='manual', reason='target_protected'))
                with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_unavailable'):
                    tasks.compile_instruction(raw, 'Search 입력란을 N으로 설정해줘')

    def test_combo_child_editor_is_not_reclassified_as_an_ordinary_textfield(self):
        raw = raw_text()
        raw['target']['locator']['ancestors'].insert(0, dict(role='combobox', automation_id='parentCombo'))
        self.assertEqual(task(raw)['steps'][0]['action'], 'manual')
        source = task(raw_text())
        source['steps'][0]['ancestors'] = raw['target']['locator']['ancestors']
        source['steps'][0]['within'] = 'parentCombo'
        with self.assertRaisesRegex(tasks.TaskError, 'text_input_owned_by_combo'):
            tasks._validate(source)

    def test_unrecognized_named_input_uses_fill_query_but_unspecified_field_keeps_combo_default(self):
        raw = dict(action='manual', reason='capture_target_unavailable')
        spec = tasks.compile_instruction(raw, '입력받은 변수를 Search 입력란에 입력해줘')
        source = dict(schema=1, name='Recovered text', exe='Example.exe', window='Query window',
                      steps=[tasks.instructed_step(raw, spec)])
        tasks._validate(source)
        self.assertEqual((source['steps'][0]['action'], source['steps'][0]['match']), ('fill', 'query'))
        plain = tasks.compile_instruction(raw, 'Search 조건을 N으로 설정해줘')
        self.assertEqual(plain['role'], 'combobox')
        explicit = tasks.compile_instruction(raw, '실행할 때 받은 검색어를 사용',
                                              target='Search', role='textfield', mode='required', argument='Search')
        self.assertEqual(tasks.instructed_step(raw, explicit)['action'], 'fill')

    def test_known_text_draft_retains_identity_and_can_be_bound_later(self):
        raw = raw_text()
        sentence = '다음에 전달할 검색어를 그대로 사용해줘'
        step = tasks.instructed_step(raw, tasks.compile_instruction(raw, sentence))
        self.assertEqual((step['action'], step['target'], step['instruction_draft']), ('fill', 'queryInput', sentence))
        restored = tasks.instructed_step(step, tasks.compile_instruction(step, sentence, mode='required', argument='Search'))
        self.assertNotIn('instruction_draft', restored)
        self.assertEqual(restored['instruction']['parameter'], 'Search')

    def test_recipe_compiles_text_and_combo_values_with_roles_preserved(self):
        source = task(bound(raw_combo(), 'Status를 N으로 설정해줘'),
                      bound(raw_text(), '입력받은 변수를 Search 입력란에 입력해줘'),
                      bound(raw_text(), 'Search 입력란을 비워줘'))
        result = plans.compile_plan(source)
        self.assertEqual(result['status'], 'compiled')
        first, second, third = result['envelope']['recipe']['steps']
        self.assertEqual((first['selector']['role'], first['value']), ('combobox', 'N'))
        self.assertEqual((second['action'], second['selector']['role'], second['parameter']), ('set-value', 'textfield', 'Search'))
        self.assertNotIn('value', second)
        self.assertEqual(third['value'], '')


if __name__ == '__main__':
    unittest.main()
