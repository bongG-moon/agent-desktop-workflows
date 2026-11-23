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
                                 ('double-click-result', 'single', surface))
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
