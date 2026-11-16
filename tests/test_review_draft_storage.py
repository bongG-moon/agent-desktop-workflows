"""Exercise atomic creation, resumption and promotion in an isolated task directory."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import learned_tasks as tasks
import recorded_plan
import review_drafts
import teach_capture


class ReviewDraftStorageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name)
        self.path = self.directory / 'draft.json'
        step = teach_capture.manual('capture_target_unavailable', 'field')
        self.rows = [{'step': step, 'form': {'alias': '조회 조건', 'text': '지정한 값으로 바꿔줘',
                                             'mode': 'required', 'argument': ''}}]

    def editor(self, name='draft'):
        return review_drafts.ReviewStorage(name, 'Demo.exe', '조건', self.directory)

    def fixed_steps(self):
        step = deepcopy(self.rows[0]['step'])
        step['instruction'] = tasks.compile_instruction(step, 'Family 조건을 Hybrid로 설정해줘')
        return [step]

    def create(self, name='draft'):
        return self.editor(name).save_draft(self.rows, 'invalid_instruction_argument', 1)

    def test_saved_review_is_visible_but_cannot_load_as_a_replay_task(self):
        saved = self.create()
        self.assertTrue(saved['draft'])
        self.assertEqual((saved['status'], saved['input_dispatched'], saved['step_number']), ('recorded', False, 1))
        self.assertEqual(tasks.list_tasks(self.directory), ['draft'])
        with self.assertRaisesRegex(tasks.TaskError, 'recording_draft_requires_review'):
            tasks.load_task('draft', self.directory)
        restored = self.editor()
        rows = restored.initial_rows()
        self.assertEqual(rows, self.rows)
        rows[0]['form']['argument'] = '제품군'
        self.assertEqual(restored.initial_rows(), self.rows)
        self.assertEqual(tasks.load_task('draft', self.directory, allow_review=True)['rows'], self.rows)

    def test_interrupted_write_or_replace_preserves_original_and_can_retry(self):
        self.create()
        original = self.path.read_bytes()
        editor = self.editor()
        revised = editor.initial_rows()
        revised[0]['form']['text'] += ' 한 번만'
        for function in ('fsync', 'replace'):
            with self.subTest(function=function):
                with patch.object(tasks.os, function, side_effect=OSError('write interrupted')):
                    with self.assertRaisesRegex(tasks.TaskError, 'task_store_unavailable'):
                        editor.save_draft(revised, 'invalid_instruction_argument', 1)
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(list(self.directory.glob('.new-*')), [])
        self.assertTrue(editor.save_draft(revised, 'invalid_instruction_argument', 1)['draft'])
        self.assertEqual(self.editor().initial_rows(), revised)

    def test_two_resumed_editors_cannot_replace_a_newer_draft_or_promoted_task(self):
        for winner_promotes in (False, True):
            with self.subTest(winner_promotes=winner_promotes):
                name = 'promoted' if winner_promotes else 'revised'
                self.create(name)
                winner, stale = self.editor(name), self.editor(name)
                revised = winner.initial_rows()
                revised[0]['form']['alias'] = '편집된 이름'
                if winner_promotes:
                    winner.save_recording(self.fixed_steps())
                else:
                    winner.save_draft(revised, 'invalid_instruction_argument', 1)
                path = self.directory / (name + '.json')
                latest_bytes = path.read_bytes()
                with self.assertRaisesRegex(tasks.TaskError, 'recording_draft_changed'):
                    stale.save_draft(self.rows, 'invalid_instruction_argument', 1)
                with self.assertRaisesRegex(tasks.TaskError, 'recording_draft_changed'):
                    stale.save_recording(self.fixed_steps())
                self.assertEqual(path.read_bytes(), latest_bytes)
                if winner_promotes:
                    with self.assertRaisesRegex(tasks.TaskError, 'task_already_exists'):
                        self.editor(name)
                    self.assertEqual(tasks.load_task(name, self.directory)['steps'][0]['instruction']['value'], 'Hybrid')
                else:
                    self.assertEqual(self.editor(name).initial_rows(), revised)

    def test_racing_first_publication_cannot_clobber_an_existing_name(self):
        winner, late = self.editor(), self.editor()
        winner.save_recording(self.fixed_steps())
        original = self.path.read_bytes()
        with self.assertRaisesRegex(tasks.TaskError, 'task_already_exists'):
            late.save_draft(self.rows, 'invalid_instruction_argument', 1)
        with self.assertRaisesRegex(tasks.TaskError, 'task_already_exists'):
            late.save_recording(self.fixed_steps())
        self.assertEqual(self.path.read_bytes(), original)

    def test_malformed_review_and_wrong_window_fail_without_replacing_it(self):
        self.create()
        original = self.path.read_bytes()
        for exe, window in [('Other.exe', '조건'), ('Demo.exe', '다른 창')]:
            with self.subTest(exe=exe, window=window), self.assertRaisesRegex(tasks.TaskError, 'recorded_window_mismatch'):
                review_drafts.ReviewStorage('draft', exe, window, self.directory)
        invalid = deepcopy(self.rows)
        invalid[0]['form']['mode'] = 'invalid'
        for rows, number in ((invalid, 1), (self.rows * 2, 1), (self.rows, 3)):
            with self.assertRaisesRegex(tasks.TaskError, 'invalid_recording_draft'):
                self.editor().save_draft(rows, 'invalid_step_instruction', number)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_bytes(original.replace(b'"mode": "required"', b'"mode": "invalid"'))
        malformed = self.path.read_bytes()
        self.assertNotEqual(malformed, original)
        with self.assertRaisesRegex(tasks.TaskError, 'invalid_recording_draft'):
            self.editor()
        self.assertEqual(self.path.read_bytes(), malformed)

    def test_plan_failure_after_promotion_keeps_valid_task_and_successful_save_receipt(self):
        self.create()
        editor = self.editor()

        def accept_correction(owner, *, title, initial_draft, initial_step, save_recording, save_draft):
            self.assertEqual(initial_draft, self.rows)
            self.assertEqual(initial_step, editor.previous['step_number'])
            return save_recording(self.fixed_steps())

        with (patch.object(teach_capture, 'capture', side_effect=accept_correction),
              patch.object(desktop_select, 'task_directory', return_value=self.directory),
              patch.object(recorded_plan, 'get_plan', side_effect=RuntimeError('plan interrupted'))):
            saved = desktop_select.edit_recording(None, editor)
        self.assertEqual(saved['status'], 'recorded')
        self.assertFalse(saved['input_dispatched'])
        self.assertEqual(saved['execution_plan']['status'], 'unavailable')
        published = tasks.load_task('draft', self.directory)
        self.assertEqual(published, editor.saved_plan)
        self.assertEqual(published['steps'][0]['instruction']['value'], 'Hybrid')
