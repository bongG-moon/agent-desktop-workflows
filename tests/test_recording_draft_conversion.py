"""Editor instructions cross the recording, persistence and replay boundaries."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import compound_plan
import learned_tasks as tasks
import recorded_plan
import teach_capture as capture


class RecordingDraftConversionTests(unittest.TestCase):
    sentence = '10초 기다렸다가 안내가 있으면 닫고 조회를 계속해줘'

    def recording(self, raw):
        return tasks.from_recording('초안', 'Demo.exe', '조건', raw)

    def stored(self, plan):
        with tempfile.TemporaryDirectory() as directory:
            tasks.save_task(plan, directory)
            return tasks.load_task(plan['name'], directory)

    def test_manual_note_roundtrips_with_scope_without_promoting_capture(self):
        for reason in ('right_click_requires_menu_teaching', 'popup_needs_live_binding',
                       'protected_input_not_recorded', 'delivery_uncertain'):
            with self.subTest(reason=reason):
                raw = capture.manual(reason)
                raw['window_context'] = {'mode': 'existing', 'exe': 'Child.exe', 'title': '상세'}
                edits = capture.StepEdits()
                edits.store(raw, '후속 단계', self.sentence)
                task = self.stored(self.recording(edits.apply([raw])))
                self.assertEqual(task['steps'], [{'action': 'manual', 'reason': reason,
                                                  'instruction_draft': self.sentence, 'alias': '후속 단계',
                                                  'window_context': raw['window_context']}])
                self.assertEqual(tasks.resolve_saved_intents(task), task)
                result = tasks.replay(task, 'Demo.exe', '조건', {}, lambda _: self.fail('sent input'))
                self.assertEqual((result['status'], result['diagnostic'], result['input_dispatched']),
                                 ('needs_review', reason, False))
                self.assertNotIn('grounding_steps', result)
                for replacements in (None, {1: {'action': 'conditional-open'}}):
                    compiled = recorded_plan.compile_plan(task, replacements=replacements)
                    self.assertEqual((compiled['status'], compiled['diagnostic']), ('not_compatible', reason))
                with self.assertRaisesRegex(ValueError, 'recorded_instruction_required'):
                    compound_plan._eligible(task, 1)
                with self.assertRaises(tasks.TaskError):
                    tasks.compile_instruction(task['steps'][0], '조회 버튼을 눌러줘', mode='recorded',
                                              target='조회', role='button')

    def test_incomplete_capture_cannot_lose_its_original_boundary(self):
        for extra, reason in [
                ({'secure': True}, 'target_protected'),
                ({'target': {'kind': 'element', 'locator': {'ancestors': [{'is_password': True}]}}},
                 'target_protected'),
                ({'action': 'right-click'}, 'right_click_requires_menu_teaching'),
                ({'target': {'kind': 'element', 'window': {}, 'locator': {}}}, 'popup_needs_live_binding')]:
            with self.subTest(extra=extra):
                raw = dict(capture.manual('capture_target_unavailable'), **extra)
                raw['instruction'] = {'mode': 'draft', 'text': self.sentence}
                step = self.stored(self.recording([raw]))['steps'][0]
                self.assertEqual(step, {'action': 'manual', 'reason': reason, 'instruction_draft': self.sentence})
                self.assertFalse(tasks.is_recoverable(step))

    def test_mixed_draft_handoff_excludes_nonrecoverable_steps(self):
        edits = capture.StepEdits()
        raw = [capture.manual(reason) for reason in ('capture_target_unavailable', 'delivery_uncertain')]
        for item in raw:
            edits.store(item, '', self.sentence)
        task = self.stored(self.recording(edits.apply(raw)))
        replay = tasks.replay(task, 'Demo.exe', '조건', {}, lambda _: self.fail('sent input'))
        for result in (replay, recorded_plan.compile_plan(task)):
            self.assertEqual(result['diagnostic'], 'instruction_requires_structure')
            self.assertEqual([step['number'] for step in result['grounding_steps']], [1])

    def test_fixed_and_required_queries_survive_save_as_entered(self):
        raw = [capture.manual('capture_target_unavailable') for _ in range(2)]
        edits = capture.StepEdits()
        edits.store(raw[0], '', 'MES Setup 조건을 N으로 설정해줘')
        edits.store(raw[1], '', '입력받은 {제품군} 변수를 Family 조건에 입력해줘')
        task = self.stored(self.recording(edits.apply(raw)))
        first, second = task['steps']
        self.assertEqual((first['match'], first['target'], first['instruction']['value']), ('query', 'MES Setup', 'N'))
        self.assertEqual((second['match'], second['target'], second['instruction']['parameter']),
                         ('query', 'Family', '제품군'))
        self.assertEqual(tasks.replay(task, 'Demo.exe', '조건', {},
                                      lambda _: self.fail('sent input'))['missing_parameters'], ['제품군'])

    def test_invalid_instruction_reports_its_original_row_and_preserves_others(self):
        raw = [capture.manual('capture_target_unavailable'), capture.manual('capture_target_unavailable')]
        raw[1]['instruction'] = {'mode': 'draft', 'text': ''}
        original = deepcopy(raw)
        with self.assertRaisesRegex(tasks.TaskError, 'invalid_step_instruction') as exc:
            self.recording(raw)
        self.assertEqual(exc.exception.step_number, 2)
        self.assertEqual(raw, original)
        plan = self.recording(raw[:1])
        plan['steps'].append({'action': 'manual', 'reason': 'target_protected', 'instruction_draft': ''})
        with self.assertRaisesRegex(tasks.TaskError, 'invalid_step_instruction') as exc:
            self.stored(plan)
        self.assertEqual(exc.exception.step_number, 2)
        del plan['steps'][1]
        self.assertEqual(self.stored(plan), plan)

    def test_window_opener_diagnostic_reports_the_dependent_row(self):
        raw = [capture.manual('capture_target_unavailable') for _ in range(2)]
        raw[1]['window_context'] = {'after_step': 1, 'exe': 'Child.exe', 'title': '상세'}
        plan = self.recording(raw)
        with self.assertRaisesRegex(tasks.TaskError, 'invalid_window_origin_step') as exc:
            tasks._validate(plan)
        self.assertEqual(exc.exception.step_number, 2)

    def test_conflicting_instructions_do_not_fall_back_to_the_recorded_gesture(self):
        for raw in [dict(capture.manual('capture_target_unavailable'),
                         instruction={'mode': 'draft', 'text': '첫 문장'}, instruction_draft='두 번째 문장'),
                    {'action': 'result-menu', 'label': '내보내기', 'context': {}, 'instruction_draft': self.sentence}]:
            with self.subTest(action=raw['action']), self.assertRaisesRegex(tasks.TaskError, 'invalid_step_instruction') as exc:
                self.recording([raw])
            self.assertEqual(exc.exception.step_number, 1)
