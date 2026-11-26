"""A demonstrated detail field remains an observation in its recorded window."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import capture_trail
import learned_tasks as tasks
import recorded_plan
import teach_capture as capture
from test_recorded_plan import button


def field(**changes):
    return dict(dict(id='memo', action='click', field_label='Comment',
                     recognition=dict(target='identified', action='unsupported', result='manual'),
                     target=dict(kind='element', locator=dict(role='textfield', automation_id='txtComment',
                         name='Comment', ancestors=[dict(role='group', automation_id='Information')]))), **changes)


class PopupReadCaptureTests(unittest.TestCase):
    def test_header_demonstration_generates_a_named_instruction_from_the_first_press(self):
        from test_visual_recording import VisualRecordingTests
        fixture = VisualRecordingTests()
        fixture.setUp()
        worker, notes = fixture.worker, fixture.notes
        table = dict(role='group', automation_id='inventory')
        header = dict(role='headeritem', name='Part ID', ancestors=[table])
        observed = dict(fixture.observed, node=dict(role='headeritem', secure=False, locator=header))
        worker.uia.basic.return_value = table
        worker.uia.bounds.return_value = dict(x=195, y=65, width=220, height=80)
        worker.uia.same.side_effect = lambda a, b: a is b
        notes.add(capture.manual('unsupported_recorded_action', 'first'))
        worker.recent_click = dict(token=41, step_id='first', observation=observed)
        worker._remember_double_target(fixture.event)
        worker.capture_double(dict(fixture.event, double=True, previous_token=41), observed)
        raw, = notes.snapshot()
        sentence = capture.default_text(raw)
        self.assertEqual(sentence, 'Part ID 열 머리 더블클릭')
        edits = capture.StepEdits()
        edits.store(raw, '', sentence)
        saved = tasks.from_recording('Open header', 'Orders.exe', 'Orders', edits.apply([raw]))
        step, = saved['steps']
        self.assertEqual(step['selector'], table)
        self.assertEqual(step['result_point'], dict(x=13, y=51, width=220, height=80))
        self.assertEqual((step['action'], step['result_row'], step['result_column']),
                         ('double-click-result', 'header', 'Part ID'))
        self.assertNotIn('visual', step)
        recipe = recorded_plan.compile_plan(saved)['envelope']['recipe']
        self.assertEqual(recipe['steps'][0]['result_row'], 'header')
        self.assertEqual(recipe['steps'][0]['result_point'], step['result_point'])

    def test_header_requires_named_instruction_and_recorded_table_and_point(self):
        table = dict(role='table', automation_id='inventory')
        point = dict(x=31, y=12, width=124, height=64)
        raw = dict(capture.manual('double_click_needs_review'), result_surface=table, result_point=point)
        for sentence in ('Part ID 열 머리 더블클릭', 'double-click the Part ID column header'):
            spec = tasks.compile_instruction(raw, sentence)
            self.assertEqual((spec['result_column'], spec['result_row']), ('Part ID', 'header'))
            self.assertEqual(tasks.interpret_step(raw, sentence, 'double-click-result', table,
                                                 result_row='header')['result_point'], point)
        sentence = 'Part ID 열 머리 더블클릭'
        self.assertEqual(tasks.compile_instruction(dict(raw, result_header='Other'), sentence)['mode'], 'draft')
        self.assertEqual(tasks.compile_instruction(capture.manual('double_click_needs_review'), sentence)['mode'], 'draft')
        different = dict(table, automation_id='other')
        with self.assertRaisesRegex(tasks.TaskError, 'recorded_header_required'):
            tasks.interpret_step(raw, sentence, 'double-click-result', different, result_row='header')
        self.assertEqual(tasks.compile_instruction(dict(raw, result_header='Part ID'),
                                                  '조회 결과 행 더블클릭')['result_row'], 'recorded')

    def test_related_field_check_after_drag_uses_the_runtime_value(self):
        row = dict(capture.manual('drag_needs_review'),
                   window_context=dict(exe='Orders.exe', title='Details', related=True, after_step=2),
                   target=dict(kind='element', locator=dict(role='textfield', automation_id='identifier',
                                                            name='Part ID', ancestors=[dict(role='group', name='Info')])))
        spec = tasks.compile_instruction(row, '상세 창에서 Part ID가 입력한 값과 같은지 확인해줘')
        self.assertEqual((spec['mode'], spec['parameter'], spec['selector']),
                         ('verify-value', 'Part ID', dict(role='textfield', automation_id='identifier',
                                                         ancestors=[dict(role='group', name='Info')])))
        self.assertNotIn('result_column', spec)
        self.assertEqual(tasks.instructed_step(row, spec)['selector'], spec['selector'])
        self.assertNotEqual(tasks.compile_instruction(row, '조회 결과 표에서 Part ID가 일치하는지 확인해줘')['mode'],
                            'verify-value')

    def test_plain_list_double_click_requires_a_demonstrated_table(self):
        from test_visual_target import image, visual, VisualTargets
        surface = dict(role='group', automation_id='paintedResults')
        marker = dict(visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162)), gesture='double-click')
        default = '녹화한 항목 더블클릭'
        raw = dict(capture.manual('double_click_needs_review'), result_surface=surface)
        visual_row = dict(action='visual-click', visual=marker, label=default, result_surface=surface)
        for text in ('그냥 이 list를 더블클릭', '목록을 더블클릭', 'double-click this table', default):
            for captured in (raw, visual_row):
                intent = tasks.compile_instruction(captured, text)
                self.assertEqual((intent['result_row'], intent['selector']), ('recorded', surface))
        for text in ('목록의 설정 버튼 더블클릭', '표에서 장비 ID 부분 더블클릭', 'double-click the row where Name equals A',
                     '결과 표의 실행 버튼 더블클릭'):
            self.assertIsNone(tasks.current_result_intent(visual_row, text), text)
        self.assertIsNone(tasks.current_result_intent(dict(visual_row, label='특정 상품 더블클릭'), default))
        no_surface = dict(visual_row)
        del no_surface['result_surface']
        self.assertEqual(tasks.compile_instruction(no_surface, default)['mode'], 'visual-click')
        old = dict(visual_row, instruction=dict(mode='visual-click', text=default))
        self.assertEqual(tasks.recorded_step(old)['selector'], surface)
        saved = tasks.from_recording('목록 열기', 'Demo.exe', 'Main', [visual_row])
        compiled = recorded_plan.compile_plan(saved)['envelope']['recipe']['steps'][0]
        self.assertEqual((compiled['action'], compiled['result_row'], compiled['selector']),
                         ('double-click-result', 'recorded', surface))
        point = dict(x=23, y=41, width=310, height=150)
        fresh = dict(visual_row, result_point=point)
        stored = tasks.from_recording('새 목록', 'Demo.exe', 'Main', [fresh])['steps'][0]
        self.assertEqual((stored['action'], stored['selector'], stored['result_point']),
                         ('double-click-result', surface, point))
        self.assertNotIn('visual', stored)
        self.assertEqual(tasks.recorded_step(dict(fresh, label='특정 상품 더블클릭'))['visual'], marker)

    def test_old_draft_with_a_table_and_a_drag_defers_the_read_to_its_opener(self):
        import recovery
        import review_drafts
        exe, window = 'Lookup.exe', 'Lookup'
        surface = dict(role='group', automation_id='paintedResults')
        double = dict(capture.manual('double_click_needs_review', 'open'), result_surface=surface)
        drag = dict(capture.manual('drag_needs_review', 'drag'),
                    _capture_window=dict(exe=exe, title='Details', related=True, after_id='open'))
        def row(step, sentence):
            return dict(step=step, form=dict(alias='', text=sentence, mode=None, argument=''))
        sentence = '녹화한 항목 더블클릭 (조회 결과 행 더블클릭)'
        doc = dict(schema=1, kind=review_drafts.KIND, name='lookup', exe=exe, window=window,
                   rows=[row(double, sentence), row(drag, '1단계에서 열린 창에서 comment부분 내용 추출')])
        prepared = recovery._rows(doc)
        self.assertFalse(any(r.get('field_problem') or r.get('scope_problem') for r in prepared))
        task = dict(schema=1, name='lookup', window=window, exe=exe, steps=[r['step'] for r in prepared])
        recipe = recorded_plan.compile_plan(task)['envelope']['recipe']['steps']
        self.assertEqual((recipe[0]['result_row'], recipe[0]['selector']), ('recorded', surface))
        self.assertEqual(recipe[1]['selector'], dict(role='textfield', query='comment'))
        self.assertEqual(recipe[1]['window_context']['after_step'], 1)
        self.assertEqual('read-value', recipe[1]['action'])
        for suffix in ('결과 행 더블클릭 말고 닫기', '결과 행 더블클릭하지 마'):
            self.assertEqual('draft', tasks.compile_instruction(double, '녹화한 항목 더블클릭 (' + suffix + ')')['mode'])

    def test_column_and_exact_value_are_not_replaced_with_demonstrated_point(self):
        surface = dict(role='group', automation_id='grid')
        point = dict(x=30, y=50, width=200, height=300)
        raw = dict(capture.manual('double_click_needs_review'), result_surface=surface, result_point=point)
        self.assertEqual(tasks.recorded_step(raw)['result_point'], point)
        base = tasks.compile_instruction(raw, '조회 결과 행을 더블클릭해줘')
        self.assertEqual(base['result_point'], point)
        column = tasks.compile_instruction(raw, '조회 결과에서 장비 ID 부분 더블클릭')
        self.assertEqual((column['result_row'], column['result_column']), ('first', '장비 ID'))
        self.assertNotIn('result_point', column)
        quoted = tasks.compile_instruction(raw, '조회 결과에서 "Asset ID" 열을 더블클릭')
        self.assertEqual(quoted['result_column'], 'Asset ID')
        for constrained in ('조회 결과에서 상태가 진행중인 행 더블클릭',
                            'double-click the result row where Status equals Ready',
                            '조회 결과에서 완료일/시각 열을 더블클릭'):
            self.assertEqual('draft', tasks.compile_instruction(raw, constrained)['mode'], constrained)
        self.assertEqual('single', tasks.compile_instruction(raw, '유일한 결과 행 더블클릭')['result_row'])
        old_raw = dict(capture.manual('double_click_needs_review'), result_surface=surface)
        old_spec = dict(tasks.compile_instruction(old_raw, '조회 결과 행 더블클릭'), result_row='single')
        self.assertEqual('single', tasks.recorded_step(dict(old_raw, instruction=old_spec))['result_row'])
        self.assertEqual('draft', tasks.compile_instruction(raw, '입력값과 일치하는 결과 행을 더블클릭해줘')['mode'])
        self.assertEqual(tasks.result_location(raw), dict(result_surface=surface, result_point=point))
        with self.assertRaises(tasks.TaskError):
            tasks.recorded_step(dict(raw, result_point=dict(point, x=201)))

    def test_default_saved_double_retains_owner_drawn_surface_when_description_changes(self):
        from test_visual_target import image, visual, VisualTargets
        marker = dict(visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162)), gesture='double-click')
        surface = dict(role='group', automation_id='paintedResults',
                       ancestors=[dict(role='group', automation_id='content')])
        raw = dict(id='double', action='visual-click', label='녹화한 항목 더블클릭', visual=marker,
                   result_surface=surface)
        captured = capture.capture_step(raw)
        saved = tasks.from_recording('Details', 'Example.exe', 'Main', [captured])
        with tempfile.TemporaryDirectory() as folder:
            tasks.save_task(saved, folder)
            loaded = tasks.load_task('Details', folder)['steps'][0]
        self.assertEqual(loaded['result_surface'], surface)
        draft = tasks.instructed_step(loaded, tasks.compile_instruction(loaded, '현재 작업에 맞게 열어줘'))
        self.assertEqual(draft['result_surface'], surface)
        command = tasks.instructed_step(draft, tasks.compile_instruction(draft, '현재 결과 행을 더블클릭해줘'))
        self.assertEqual(command['selector'], surface)
        self.assertNotIn('visual', command)
        self.assertNotIn('result_surface', command)
        tasks._validate(dict(saved, steps=[command]))

    def test_double_replacement_keeps_first_press_result_container(self):
        from test_visual_recording import VisualRecordingTests
        fixture = VisualRecordingTests()
        fixture.setUp()
        surface = dict(role='group', automation_id='paintedResults')
        previous = dict(capture.manual('capture_target_unavailable', 'first'),
                        target=dict(kind='element', locator=surface))
        fixture.notes.steps['first'] = previous
        fixture.notes.times['first'] = 2.
        fixture.worker.recent_click = dict(step_id='first')
        def replace(*args):
            fixture.notes.add(capture.manual('double_click_needs_review', 'first'))
            return True
        with patch.object(capture.recorder.CaptureWorker, 'capture_double', side_effect=replace):
            fixture.worker.capture_double(fixture.event, fixture.observed)
        step, = fixture.notes.snapshot()
        self.assertEqual(step['result_surface'], surface)
        self.assertEqual(tasks.recorded_step(step)['result_surface'], surface)

    def test_double_replacement_uses_only_table_relative_offsets_from_the_first_hit(self):
        from test_visual_recording import VisualRecordingTests
        fixture = VisualRecordingTests()
        fixture.setUp()
        worker, notes = fixture.worker, fixture.notes
        surface = dict(role='group', automation_id='results')
        first = capture.manual('unsupported_recorded_action', 'first')
        notes.add(first)
        worker.recent_click = dict(step_id='first', observation=dict(window=501,
            node=dict(secure=False, role='group', locator=surface, bounds=dict(x=195, y=65, width=220, height=80))))
        worker._remember_double_target(fixture.event)
        with patch.object(capture.recorder.CaptureWorker, 'capture_double',
                          side_effect=lambda *args: (notes.add(capture.manual('double_click_needs_review', 'first')) or True)):
            worker.capture_double(dict(fixture.event, double=True), None)
        step = tasks.recorded_step(notes.snapshot()[0])
        wanted = dict(x=13, y=51, width=220, height=80)
        self.assertEqual((step['result_surface'], step['result_point']), (surface, wanted))
        tasks._validate_step(step, set())
        new = tasks.instructed_step(step, tasks.compile_instruction(step, '나중에 열기'))
        self.assertEqual(tasks.compile_instruction(new, '조회 결과 행 더블클릭')['result_point'], wanted)

    def test_field_drag_keeps_the_same_readable_field_only(self):
        from test_visual_recording import VisualRecordingTests
        fixture = VisualRecordingTests()
        fixture.setUp()
        worker = fixture.worker
        worker.uia.same.side_effect = lambda a, b: a is b
        worker.uia.pid.return_value = fixture.binding.pid
        locator = field()['target']['locator']
        node = dict(role='textfield', locator=locator, semantic_label='Comment', secure=False,
                    bounds=dict(x=200, y=100, width=220, height=50))
        observed = dict(fixture.observed, node=node)
        worker.uia.from_point.return_value = observed['element']
        up = dict(fixture.event, kind='up', inside=True, x=265)
        self.assertTrue(worker._capture_field_drag(fixture.event, observed, up))
        raw, = fixture.notes.snapshot()
        self.assertEqual(tasks.compile_instruction(raw, 'Comment 내용 추출')['selector']['automation_id'], 'txtComment')
        self.assertFalse(worker._capture_field_drag(fixture.event, observed, dict(up, x=550)))
        worker.uia.from_point.return_value = Mock()
        self.assertFalse(worker._capture_field_drag(fixture.event, observed, up))

    def test_current_result_keeps_row_editorial_evidence_but_compiles_only_container(self):
        cue = dict(ordinal=1, point=[14, 4], size=[28, 12], role='row', scope=['controls'])
        row = dict(version=2, role='row', scope=['controls'], size=[28, 12], ordinal=1,
                   boxes=dict(key=[0, 0, 4, 4], button=[12, 2, 4, 4]), key='qlU=', button='Vao=')
        surface = dict(role='group', automation_id='paintedResults')
        for key, evidence in (('row_cue', cue), ('recorded_row', dict(label='녹화한 행 버튼', row=row))):
            with self.subTest(evidence=key):
                raw = dict(capture.manual('row_capture_requires_review'), result_surface=surface,
                           **{key: deepcopy(evidence)})
                command = tasks.instructed_step(raw, tasks.compile_instruction(raw, '현재 결과 행을 더블클릭해줘'))
                plan = dict(schema=1, name='Details', exe='Example.exe', window='Main', steps=[command])
                tasks._validate(plan)
                self.assertEqual(command[key], evidence)
                compiled = recorded_plan.compile_plan(plan)['envelope']['recipe']['steps'][0]
                self.assertEqual((compiled['action'], compiled['result_row'], compiled['selector']),
                                 ('double-click-result', 'recorded', surface))
                self.assertNotIn(key, compiled)
                self.assertNotIn('visual', compiled)

    def test_nonfirst_or_multiple_result_request_is_not_collapsed_to_single_row(self):
        raw = capture.manual('double_click_needs_review')
        for sentence in ('10번째 결과를 더블클릭해줘', '결과 2행을 더블클릭해줘',
                         'double-click second result', '결과 세 번째 행을 더블클릭해줘',
                         '모든 결과 행을 더블클릭해줘', '결과 0행 더블클릭',
                         '결과 행들을 더블클릭해줘'):
            with self.subTest(sentence=sentence):
                self.assertIsNone(tasks.current_result_intent(raw, sentence))

    def test_result_surface_hint_rejects_business_value_and_unrelated_target_data(self):
        for hint in (dict(role='row', name='old-part'), dict(role='group', automation_id='grid', x=20),
                     dict(role='group', automation_id='grid', protected=True)):
            with self.subTest(hint=hint), self.assertRaises(ValueError):
                tasks.result_surface_hint(dict(action='manual', result_surface=hint))

    def test_field_click_retains_locator_without_promoting_input(self):
        raw = field()
        step = capture.capture_step(raw)
        self.assertEqual(step['action'], 'manual')
        self.assertEqual(step['target'], raw['target'])
        self.assertEqual(step['field_label'], 'Comment')
        self.assertEqual(tasks.recorded_step(step)['action'], 'manual')
        self.assertNotIn('value', step)

    def test_extraction_synonyms_keep_field_scope_and_read_only_action(self):
        raw = capture.capture_step(field())
        raw['window_context'] = dict(exe='Example.exe', title='Details', after_step=1, related=True)
        before = deepcopy(raw)
        for text in ('comment 내용 추출', 'Comment 값을 추출해줘', 'Comment 필드 내용을 추출해주세요',
                     'Comment 내용 가져와', 'Comment 알려줘', 'Comment 읽어줘', 'extract Comment text'):
            with self.subTest(text=text):
                spec = tasks.compile_instruction(raw, text)
                step = tasks.instructed_step(raw, spec)
                self.assertEqual(step['action'], 'read-value')
                self.assertEqual(step['selector'], dict(role='textfield', automation_id='txtComment',
                    ancestors=[dict(role='group', automation_id='Information')]))
                self.assertEqual(step['window_context'], raw['window_context'])
                self.assertNotIn('value', step)
        self.assertEqual(raw, before)

    def test_protected_field_is_not_retained_or_read(self):
        raw = field()
        raw['target']['locator']['ancestors'][0]['protected'] = True
        step = capture.capture_step(raw)
        self.assertEqual(step['recognition']['reason'], 'protected_input_not_recorded')
        self.assertEqual(step['target']['locator'], {})
        self.assertIsNone(tasks._observed_intent(step, 'Comment 내용 추출'))
        self.assertIsNone(tasks._observed_intent(raw, 'Comment 내용 추출'))

    def test_compound_or_negative_extraction_does_not_become_single_read(self):
        raw = capture.capture_step(field())
        for text in ('Comment 추출하지 마', 'Comment 추출하고 창 닫기', 'Comment 말고 Notes 추출',
                     '추출', '내용 추출', 'extract Comment and close window'):
            with self.subTest(text=text):
                self.assertIsNone(tasks._observed_intent(raw, text))

    def test_rapid_query_double_extraction_compiles_with_deferred_detail_window(self):
        from test_visual_target import image, visual, VisualTargets
        marker = dict(visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162)), gesture='double-click')
        query = dict(button(), id='query')
        double = dict(id='double', action='visual-click', visual=marker, label='결과 항목 더블클릭')
        scope = dict(exe='Example.exe', title='Details', related=True, _openers=('query', 'double'))
        comment = dict(capture.capture_step(field()), _capture_window=scope)
        close = dict(id='close', action='click',
                     target=dict(kind='element', locator=dict(role='button', automation_id='btnExit', name='Exit')),
                     recognition=dict(target='identified', action='supported', result='manual'),
                     _capture_window=scope)
        gestures = [query, double, comment, close]
        edits = capture.StepEdits()
        for step, sentence in zip(gestures, ('조회 버튼을 눌러줘', '결과 항목 더블클릭',
                                            'comment 내용 추출', 'Exit 버튼을 눌러줘')):
            edits.store(step, '', sentence)
        source = tasks.from_recording('Lookup details', 'Example.exe', 'Main',
                                      capture_trail.finalize_contexts(edits.apply(gestures)))
        self.assertEqual(source['steps'][2]['window_context']['after_step'], 2)
        plan = recorded_plan.compile_plan(source)
        self.assertEqual(plan['status'], 'compiled', plan)
        steps = plan['envelope']['recipe']['steps']
        read = next(step for step in steps if step['action'] == 'read-value')
        opener = next(index for index, step in enumerate(steps, 1)
                      if step['action'] in ('visual-click', 'double-click-result'))
        self.assertEqual(read['window_context'], dict(exe='Example.exe', title='Details', related=True,
                                                    after_step=opener))
        self.assertEqual(read['selector']['automation_id'], 'txtComment')
        self.assertEqual(steps[-1]['window_context'], read['window_context'])
        self.assertTrue(steps[-1]['verify_closed'])


if __name__ == '__main__':
    unittest.main()
