"""Host interpretation uses common typed actions, independent of application vocabulary."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import learned_tasks as tasks
import recorded_plan


def recording(steps, exe='Inventory.exe'):
    return dict(schema=1, name='Generic workflow', exe=exe, window='Records', steps=steps)


def raw_field():
    return dict(action='manual', reason='unsupported_recorded_action',
                target=dict(kind='element', locator=dict(role='textfield', automation_id='memo',
                       ancestors=[dict(role='group', automation_id='DetailsArea')])),
                window_context=dict(exe='Inventory.exe', title='Record details', related=True, after_step=1))


class GenericInterpretedIntents(unittest.TestCase):
    def test_host_can_interpret_unmatched_prose_without_rewriting_it(self):
        raw = raw_field()
        selector = raw['target']['locator']
        for sentence in ('여기에 뭐라고 적혀 있는지 보여줘', '이 칸의 메모만 전달해줘',
                         'Dime el contenido de este campo', 'この欄の内容を教えて'):
            with self.subTest(sentence=sentence):
                self.assertEqual(tasks.compile_instruction(raw, sentence)['mode'], 'draft')
                step = tasks.interpret_step(raw, sentence, 'read-value', selector, label='Notes')
                self.assertEqual(step['selector'], selector)
                self.assertEqual(step['description'], sentence)
                self.assertEqual(step['window_context'], raw['window_context'])
                self.assertEqual(step['action'], 'read-value')
                self.assertNotIn('instruction_draft', step)
                self.assertNotIn('value', step)

    def test_explicit_first_is_a_policy_not_a_language_pattern(self):
        raw = dict(action='manual', reason='double_click_needs_review')
        selector = dict(role='group', automation_id='PaintedRecords')
        for text in ('결과 1행을 더블클릭해줘', '첫 번째 항목 열어줘', 'abre la primera fila'):
            step = tasks.interpret_step(raw, text, 'double-click-result', selector, result_row='first')
            plan = recorded_plan.compile_plan(recording([step]))
            self.assertEqual(plan['status'], 'compiled', plan)
            self.assertEqual(plan['envelope']['recipe']['steps'][0]['result_row'], 'first')
            self.assertNotIn('visual', step)
            self.assertEqual(step['description'], text)

    def test_long_multibyte_description_keeps_full_text_with_bounded_label(self):
        text = '이 필드에 표시한 내용을 현재 작업의 결과로 읽어서 전달해 주세요. ' * 5
        step = tasks.interpret_step(dict(action='manual', reason='capture_target_unavailable'), text,
                                    'read-value', dict(role='textfield', automation_id='memo'))
        self.assertEqual(step['description'], text.strip())
        self.assertLessEqual(len(step['label'].encode('utf8')), 200)
        self.assertEqual(recorded_plan.compile_plan(recording([step]))['status'], 'compiled')

    def test_current_row_continuation_does_not_depend_on_button_caption(self):
        for name in ('Load records', 'Aufträge laden', 'Actualizar', '목록 갱신', 'Go'):
            with self.subTest(caption=name):
                button = dict(action='open', target='load', match='automation_id', role='button',
                              command_name=name, expand=False,
                              instruction=dict(mode='recorded', text=f'{name} 실행'))
                row = tasks.interpret_step(dict(action='manual', reason='double_click_needs_review'),
                       '현재 항목을 열어줘', 'double-click-result',
                       dict(role='table', automation_id='records'), result_row='single')
                result = recorded_plan.compile_plan(recording([button, row]))
                self.assertEqual(result['status'], 'compiled', result)
                steps = result['envelope']['recipe']['steps']
                self.assertEqual([s['action'] for s in steps], ['invoke', 'double-click-result'])
                self.assertEqual(result['envelope']['source_steps'], [1, 2])

    def test_paraphrase_edit_keeps_typed_evidence_but_is_not_executable(self):
        for action, selector, extra in (
                ('read-value', dict(role='textfield', automation_id='memo'), {}),
                ('double-click-result', dict(role='group', automation_id='records'), {'result_row':'single'})):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                raw = dict(action='manual', reason='unsupported_recorded_action')
                step = tasks.interpret_step(raw, '원래 설명', action, selector, **extra)
                text = '이 부분은 다음 설명대로 처리해줘'
                spec = tasks.compile_instruction(step, text)
                self.assertEqual(spec, dict(mode='draft', text=text))
                edited = tasks.instructed_step(step, spec)
                self.assertEqual(edited['selector'], selector)
                doc = recording([edited])
                tasks.save_task(doc, folder)
                loaded = tasks.load_task(doc['name'], folder)
                plan = recorded_plan.compile_plan(loaded)
                self.assertEqual(plan['diagnostic'], 'instruction_requires_structure')
                repaired = tasks.interpret_step(loaded['steps'][0], text, action, selector, **extra)
                self.assertEqual(recorded_plan.compile_plan(recording([repaired]))['status'], 'compiled')

    def test_known_display_text_does_not_turn_into_unrelated_edit_field(self):
        raw = dict(action='read-value', label='Status', selector=dict(role='statictext', automation_id='status'))
        spec = tasks.compile_instruction(raw, 'Status 읽어줘')
        self.assertEqual(spec['selector'], raw['selector'])
        self.assertEqual(tasks.instructed_step(raw, spec)['action'], 'read-value')

    def test_typed_custom_or_semantic_surface_survives_wording_edit(self):
        raw = dict(action='manual', reason='capture_target_unavailable')
        for selector in (dict(role='custom', automation_id='GridSurface'),
                         dict(role='group', query='배송 목록', within='주문 영역')):
            with self.subTest(selector=selector):
                step = tasks.interpret_step(raw, '가르쳐준 첫 항목 열기', 'double-click-result', selector,
                                            result_row='first')
                text = '현재 결과의 첫 행을 더블클릭해줘'
                updated = tasks.instructed_step(step, tasks.compile_instruction(step, text))
                self.assertEqual(updated['selector'], selector)
                self.assertEqual(recorded_plan.compile_plan(recording([updated]))['status'], 'compiled')

    def test_typed_host_step_cannot_override_protected_or_unknown_capture(self):
        for raw in (dict(action='manual', reason='protected_input_not_recorded'),
                    dict(action='manual', reason='capture_target_unavailable', protected=True),
                    dict(raw_field(), target=dict(kind='element', locator=dict(role='textfield',
                         automation_id='memo', ancestors=[dict(role='group', password=True)])))):
            with self.subTest(raw=raw), self.assertRaises(tasks.TaskError):
                tasks.interpret_step(raw, '기록 읽기', 'read-value', dict(role='textfield', automation_id='memo'))
        with self.assertRaises(tasks.TaskError):
            tasks.interpret_step(raw_field(), '읽기', 'read-value', dict(role='button', automation_id='run'))
        with self.assertRaises(tasks.TaskError):
            tasks.interpret_step(raw_field(), '첫 행', 'double-click-result',
                                 dict(role='table', automation_id='rows'), result_row='all')
        with self.assertRaises(tasks.TaskError):
            tasks.interpret_step(dict(action='open'), '닫아줘', 'close-popup',
                                 dict(role='button', automation_id='close'))

    def test_local_parser_does_not_flatten_negative_or_conditional_requests(self):
        row = dict(action='manual', reason='double_click_needs_review')
        for text in ('결과 행 더블클릭하지 마', '결과가 있으면 더블클릭해줘',
                     'double-click result if it is ready', 'double-click result and close window'):
            with self.subTest(text=text):
                self.assertEqual(tasks.compile_instruction(row, text)['mode'], 'draft')
        field = raw_field()
        before = deepcopy(field)
        for text in ('Comment 추출하지 마', 'Notes가 있으면 읽어줘', 'read Notes and close window'):
            self.assertEqual(tasks.compile_instruction(field, text)['mode'], 'draft')
        self.assertEqual(field, before)


if __name__ == '__main__':
    unittest.main()
