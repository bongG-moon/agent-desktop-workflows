"""Saving finishes the session while keeping explicit later revision commands usable."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import recorder_launch as launcher
import review_drafts
import teach_capture as capture
from test_draft_save_ui import row


class SavedRecordingActions(unittest.TestCase):
    def test_save_finishes_without_another_window_and_keeps_draft_revision_commands(self):
        token = 'd' * 32
        name = '테스트 조회'
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            store = Path(tmp) / 'tasks'
            stack.enter_context(patch.dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(store)))
            folder = launcher._folder(token)
            folder.mkdir(parents=True)
            launcher._write(folder, 'request.json', dict(app='Example', task=name, created=time.time()))
            (folder / 'running.lock').write_bytes(b'0')
            storage = review_drafts.ReviewStorage(name, 'Example.exe', '예제 창', store)
            rows = [row('mode', 'Mode를 N으로 설정해줘')]
            saved = storage.save_draft(rows, 'invalid_step_instruction', 1)
            original = (store / (name + '.json')).read_bytes()
            window = stack.enter_context(patch.object(capture, 'saved_recording_actions',
                side_effect=AssertionError('saving must not open another window')))
            run = stack.enter_context(patch.object(app, 'run',
                side_effect=AssertionError('later revisions require an explicit command')))
            with launcher._lease(folder):
                final = launcher.after_save(app, token, saved)
                during_exit = launcher.status(token)
            latest = launcher.status(token)
            for state in (during_exit, latest):
                self.assertEqual(state['status'], 'recorded')
                self.assertEqual(state['task']['name'], name)
                self.assertEqual(state['first_trial_args'], ['try-recording', '--session', token, '--summary'])
                self.assertTrue(state['draft'])
                self.assertEqual(state['resume_args'], ['review', '--task', name])
                self.assertEqual(state['edit_args'], ['review', '--task', name, '--edit'])
                self.assertEqual(state['restart_args'], ['review', '--task', name, '--restart'])
            for key, flag in (('edit_args', 'edit'), ('restart_args', 'restart')):
                command = app.parser().parse_args(final[key])
                self.assertEqual((command.command, command.task), ('review', name))
                self.assertTrue(getattr(command, flag))
            checkpoint = json.loads((folder / 'saved.json').read_text(encoding='utf-8'))
            self.assertEqual(checkpoint, dict(receipt=final, editing=False))
            self.assertEqual((store / (name + '.json')).read_bytes(), original)
            self.assertEqual(app.tasks.read_document(name, store)['rows'], rows)
            window.assert_not_called()
            run.assert_not_called()

    def test_explicit_saved_revision_can_finish_its_own_session_without_reopening_a_dialog(self):
        token = 'e' * 32
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,
                DESKTOP_SELECT_TASK_DIR=str(Path(tmp) / 'tasks')):
            store = Path(tmp) / 'tasks'
            original = review_drafts.ReviewStorage('원본', 'Example.exe', '예제 창', store)
            rows = [row('mode', 'Mode를 N으로 설정해줘')]
            original.save_draft(rows, 'invalid_step_instruction', 1)
            source = app.tasks.read_document('원본', store)
            revision = review_drafts.ReviewStorage('원본 - 수정본', 'Example.exe', '예제 창', store, source=source)
            edits = capture.StepEdits()
            steps = edits.restore_rows(revision.initial_rows())
            edits.store(steps[0], '', 'Mode를 W로 설정해줘')
            saved = revision.save_recording(edits.apply(steps))
            folder = launcher._folder(token)
            folder.mkdir(parents=True)
            with patch.object(capture, 'saved_recording_actions') as window:
                final = launcher.after_save(app, token, saved)
            window.assert_not_called()
            self.assertEqual(final['task']['name'], '원본 - 수정본')
            self.assertEqual(final['edit_args'], ['review', '--task', '원본 - 수정본', '--edit'])
            self.assertEqual(app.tasks.load_task('원본 - 수정본', store)['steps'][0]['instruction']['value'], 'W')
            self.assertEqual(app.tasks.read_document('원본', store), source)

    def test_revision_destination_never_adopts_a_colliding_raw_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            args = ('사본', 'Example.exe', '예제 창', store)
            winner = review_drafts.ReviewStorage(*args, new_only=True)
            stale = review_drafts.ReviewStorage(*args, new_only=True)
            rows = [row('mode', 'Mode를 N으로 설정해줘')]
            winner.save_draft(rows, 'invalid_step_instruction', 1)
            with self.assertRaisesRegex(app.tasks.TaskError, 'task_already_exists'):
                review_drafts.ReviewStorage(*args, new_only=True)
            with self.assertRaisesRegex(app.tasks.TaskError, 'task_already_exists'):
                stale.save_draft(rows, 'invalid_step_instruction', 1)
