"""A single short launch preserves recorder events and final local receipts."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import recorder_launch as launcher


class RecorderLaunchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Path(temp.name) / '프로젝트 A' / 'tasks'
        environment = patch.dict(os.environ, DESKTOP_SELECT_TASK_DIR=str(self.store))
        environment.start()
        self.addCleanup(environment.stop)
        self.token = 'a' * 32
        self.children = []
        self.addCleanup(self.wait_children)

    def wait_children(self):
        for child in self.children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def session(self, *, events=b'', created=None, complete=False):
        folder = launcher._folder(self.token)
        folder.mkdir(parents=True)
        launcher._write(folder, 'request.json', dict(app='업무 앱', task=None,
                        created=time.time() if created is None else created))
        (folder / 'running.lock').write_bytes(b'0')
        (folder / 'events.jsonl').write_bytes(events)
        if complete:
            launcher._write(folder, 'complete.json', dict(exit_code=0))
        return folder

    def fake_child(self, body):
        """Run the actual child entrypoint with an isolated GUI-free worker."""
        real_popen = subprocess.Popen
        code = ('import sys, types, json, time\n'
                f'sys.path.insert(0, {str(Path(launcher.__file__).parent)!r})\n'
                'import recorder_launch\n'
                'def worker(args):\n' + '\n'.join('    ' + line for line in body.splitlines()) + '\n'
                'sys.modules["desktop_select"] = types.SimpleNamespace(worker=worker)\n'
                'raise SystemExit(recorder_launch._child(sys.argv[1]))\n')

        def start(argv, **kwargs):
            self.assertEqual(argv[:4], [sys.executable, '-X', 'utf8', '-B'])
            self.assertEqual(argv[4], str(Path(launcher.__file__).absolute()))
            self.assertEqual(argv[5], '--child')
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['close_fds'])
            self.assertNotIn('shell', kwargs)
            if os.name == 'nt':
                self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
            child = real_popen([sys.executable, '-X', 'utf8', '-B', '-c', code, argv[6]], **kwargs)
            self.children.append(child)
            return child
        return patch.object(launcher.subprocess, 'Popen', side_effect=start)

    def test_one_child_reports_true_ready_then_recording_and_exact_final_receipt(self):
        app_hint = ' 앱  "A" & [$value] (창) '
        task = "--새 작업 '고객' & [$value] (1)"
        with self.fake_child('''assert args.command == '_record_app'
print(json.dumps(dict(status='recorder_ready', task=args.task, input_dispatched=False)), flush=True)
time.sleep(0.4)
print(json.dumps(dict(status='recorded', app=args.app, task=args.task, recover_args=['recover', '--task', args.task], input_dispatched=False)), flush=True)
return 0''') as spawn:
            first = launcher.launch(app_hint, task)
        self.assertEqual(first['status'], 'recorder_ready')
        self.assertEqual(first['host_action'], 'wait_for_user_to_save')
        self.assertLess(first['startup_wait_seconds'], launcher.STARTUP_WAIT_SECONDS)
        spawn.assert_called_once()
        self.assertEqual(first['status_args'], ['recorder', '--session', first['session'], '--wait', '8'])
        active = launcher.status(first['session'])
        self.assertEqual((active['status'], active['host_action']), ('recording', 'wait_for_user_to_save'))
        self.children[0].wait(timeout=5)
        last = launcher.status(first['session'])
        self.assertEqual(last['status'], 'recorded')
        self.assertEqual((last['app'], last['task']), (app_hint, task))
        self.assertEqual(last['recover_args'], ['recover', '--task', task])
        self.assertEqual(last['session'], first['session'])
        self.assertEqual(launcher._folder(first['session']).parent, self.store.parent / 'recording-sessions')

    def test_terminal_revision_survives_failed_ui_checkpoint(self):
        final = dict(status='recorded', task={'name': '수정본'}, input_dispatched=False)
        folder = self.session(events=(json.dumps(final) + '\n').encode())
        launcher._publish_saved(folder, dict(final, task={'name': '원본'}), editing=True)
        self.assertEqual(launcher.status(self.token)['task'], final['task'])
        (folder / 'events.jsonl').write_bytes(b'')
        self.assertEqual(launcher.status(self.token)['task'], {'name': '원본'})

    def test_checkpoint_failure_returns_saved_receipt_without_starting_another_action(self):
        self.session()
        saved = dict(status='recorded', task={'name': '저장된 업무'}, input_dispatched=False)
        app = Mock()
        with patch.object(launcher, '_publish_saved', side_effect=OSError('disk busy')) as publish:
            result = launcher.after_save(app, self.token, saved)
        publish.assert_called_once()
        self.assertEqual(result['status'], 'recorded')
        self.assertEqual(result['edit_args'], ['review', '--task', '저장된 업무', '--edit'])
        self.assertEqual(result['restart_args'], ['review', '--task', '저장된 업무', '--restart'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(app.mock_calls, [])

    def test_timeout_returns_starting_without_terminating_the_child(self):
        with patch.object(launcher, 'STARTUP_WAIT_SECONDS', 0.08), self.fake_child('''time.sleep(0.3)
print(json.dumps(dict(status='cancelled', input_dispatched=False)), flush=True)
return 2'''):
            first = launcher.launch('느린 앱')
        self.assertEqual(first['status'], 'recorder_starting')
        self.assertLess(first['startup_wait_seconds'], 1)
        self.assertIsNone(self.children[0].poll())
        self.children[0].wait(timeout=5)
        self.assertEqual(launcher.status(first['session'])['status'], 'cancelled')

    def test_cancellation_before_ready_returns_final_not_ready(self):
        with self.fake_child('''print(json.dumps(dict(status='cancelled', diagnostic='app_selection_cancelled', input_dispatched=False)), flush=True)
return 2'''):
            result = launcher.launch('업무 앱')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['diagnostic'], 'app_selection_cancelled')
        self.assertEqual(result['host_action'], 'report_recording_cancelled')

    def test_unspecified_app_reaches_the_child_as_a_picker_request(self):
        with self.fake_child('''assert args.app is None
print(json.dumps(dict(status='recorder_selection_required', input_dispatched=False)), flush=True)
time.sleep(0.2)
print(json.dumps(dict(status='cancelled', input_dispatched=False)), flush=True)
return 2''') as spawn:
            first = launcher.launch(None)
        self.assertEqual(first['status'], 'recorder_selection_required')
        self.assertEqual(first['host_action'], 'wait_for_user_to_select_window')
        self.assertIsNone(launcher._request(launcher._folder(first['session']))['app'])
        spawn.assert_called_once()

    def test_selection_event_is_live_and_does_not_claim_editor_readiness(self):
        with self.fake_child('''print(json.dumps(dict(status='recorder_selection_required', input_dispatched=False)), flush=True)
time.sleep(0.3)
print(json.dumps(dict(status='cancelled', input_dispatched=False)), flush=True)
return 2'''):
            result = launcher.launch('여러 창')
        self.assertEqual(result['status'], 'recorder_selection_required')
        self.assertEqual(launcher.status(result['session'])['status'], 'recorder_selection_required')

    def test_saved_editor_processing_remains_live_until_final_recovery_receipt(self):
        folder = self.session(events=b'{"status":"recorder_ready"}\n')
        progress = dict(status='recorder_processing', task='업무', saved=True, input_dispatched=False)
        with (folder / 'events.jsonl').open('ab') as stream:
            stream.write(json.dumps(progress).encode() + b'\n')
        with launcher._lease(folder):
            observed = launcher.status(self.token)
            self.assertEqual(observed['status'], 'recorder_processing')
            self.assertIs(observed['saved'], True)
            final = dict(status='recorded', task='업무', recover_args=['recover', '--task', '업무'])
            with (folder / 'events.jsonl').open('ab') as stream:
                stream.write(json.dumps(final).encode() + b'\n')
        observed = launcher.status(self.token)
        self.assertEqual(observed['status'], 'recorded')
        self.assertEqual(observed['recover_args'], final['recover_args'])

    def test_worker_failure_diagnostic_is_preserved(self):
        with self.fake_child('''print(json.dumps(dict(status='blocked', diagnostic='app_not_found', candidates=[], input_dispatched=False)), flush=True)
return 2'''):
            result = launcher.launch('없는 앱')
        self.assertEqual(result['diagnostic'], 'app_not_found')
        self.assertEqual(result['candidates'], [])

    def test_status_waits_for_postsave_processing_in_one_call(self):
        with self.fake_child('''print(json.dumps(dict(status='recorder_processing', saved=True, input_dispatched=False)), flush=True)
time.sleep(0.25)
print(json.dumps(dict(status='recorded', task='업무', input_dispatched=False)), flush=True)
return 0'''):
            first = launcher.launch('앱')
        self.assertEqual(first['status'], 'recorder_processing')
        final = launcher.status(first['session'], wait_seconds=2)
        self.assertEqual(final['status'], 'recorded')

    def test_one_trial_wait_handles_slow_saved_preparation_and_has_a_deadline(self):
        self.session()
        processing = dict(status='recorder_processing', saved=True, input_dispatched=False)
        finished = dict(status='recorded', task={'name': '업무'}, input_dispatched=False)
        for finishes, expected in ((18.3, finished), (float('inf'), processing)):
            clock = [0.0]
            def sleep(seconds):
                clock[0] += seconds
            def observe(*args):
                return finished if clock[0] >= finishes else processing
            with (self.subTest(finishes=finishes), patch.object(launcher, '_observe', side_effect=observe),
                  patch.object(launcher.time, 'monotonic', side_effect=lambda: clock[0]),
                  patch.object(launcher.time, 'sleep', side_effect=sleep)):
                self.assertEqual(launcher.wait_for_trial(self.token), expected)
                self.assertAlmostEqual(clock[0], min(finishes, launcher.SAVED_PROCESSING_WAIT_SECONDS), delta=.5)

    def test_status_never_waits_on_user_recording_even_with_wait_requested(self):
        folder = self.session(events=b'{"status":"recorder_ready"}\n')
        with launcher._lease(folder), patch.object(launcher.time, 'sleep') as sleep:
            self.assertEqual(launcher.status(self.token, wait_seconds=30)['status'], 'recording')
            sleep.assert_not_called()

    def test_status_wait_is_bounded_without_restarting_or_killing_worker(self):
        folder = self.session(events=b'{"status":"recorder_processing"}\n')
        with launcher._lease(folder):
            start = time.monotonic()
            self.assertEqual(launcher.status(self.token, wait_seconds=.08)['status'], 'recorder_processing')
            self.assertLess(time.monotonic() - start, 1)
        for invalid in (-1, 31, float('nan'), float('inf'), True):
            self.assertEqual(launcher.status(self.token, wait_seconds=invalid)['diagnostic'], 'invalid_recorder_wait')

    def test_unexpected_child_failure_is_visible_and_finishes(self):
        with self.fake_child("raise RuntimeError('synthetic failure')"):
            result = launcher.launch('앱')
        self.children[0].wait(timeout=5)
        self.assertEqual(self.children[0].returncode, 2)
        self.assertEqual(result['diagnostic'], 'recorder_worker_failed')
        self.assertIn('synthetic failure', (launcher._folder(result['session']) / 'diagnostic.log').read_text())

    def test_failure_after_ready_preserves_input_uncertainty(self):
        with self.fake_child('''print(json.dumps(dict(status='recorder_ready', input_dispatched=False)), flush=True)
time.sleep(0.1)
raise RuntimeError('failed during recording')'''):
            first = launcher.launch('앱')
        self.children[0].wait(timeout=5)
        result = launcher.status(first['session'])
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(self.children[0].returncode, 3)

    def test_final_receipt_survives_corrupt_and_partial_output(self):
        receipt = dict(status='needs_review', diagnostic='recording_incomplete',
                       recover_args=['recover', '--task', '업무'], input_dispatched=False)
        self.session(events=b'bad output\n' + json.dumps(receipt).encode() + b'\n{"status":', complete=True)
        observed = launcher.status(self.token)
        self.assertEqual({key: observed[key] for key in receipt}, receipt)

    def test_final_receipt_arriving_between_read_and_exit_observation_is_preserved(self):
        folder = self.session(events=b'{"status":"recorder_ready"}\n', complete=True)
        final = dict(status='recorded', task='업무', input_dispatched=False)
        with patch.object(launcher, '_events', side_effect=[
                [dict(status='recorder_ready')], [dict(status='recorder_ready'), final]]):
            observed = launcher._observe(folder, self.token)
        self.assertEqual(observed['status'], 'recorded')
        self.assertEqual(observed['task'], '업무')

    def test_partial_terminal_is_not_accepted_and_stale_ready_is_not_final(self):
        folder = self.session(events=b'{"status":"recorder_ready"}\n{"status":"recorded"')
        with launcher._lease(folder):
            self.assertEqual(launcher.status(self.token)['status'], 'recording')
        observed = launcher.status(self.token)
        self.assertEqual(observed['status'], 'uncertain')
        self.assertEqual(observed['diagnostic'], 'recorder_exited_without_receipt')
        self.assertIsNone(observed['input_dispatched'])

    def test_terminated_child_cannot_leave_a_stale_ready_status(self):
        with self.fake_child('''print(json.dumps(dict(status='recorder_ready', input_dispatched=False)), flush=True)
time.sleep(10)
return 0'''):
            first = launcher.launch('앱')
        self.children[0].kill()
        self.children[0].wait(timeout=5)
        observed = launcher.status(first['session'])
        self.assertEqual(observed['diagnostic'], 'recorder_exited_without_receipt')
        self.assertEqual(observed['status'], 'uncertain')

    @unittest.skipUnless(os.name == 'nt', 'Windows read-only file attribute')
    def test_read_only_lock_is_not_mistaken_for_a_live_child(self):
        folder = self.session(events=b'{"status":"recorder_ready"}\n')
        lock = folder / 'running.lock'
        os.chmod(lock, stat.S_IREAD)
        self.addCleanup(os.chmod, lock, stat.S_IREAD | stat.S_IWRITE)
        observed = launcher.status(self.token)
        self.assertEqual(observed['status'], 'blocked')
        self.assertEqual(observed['diagnostic'], 'recorder_session_unavailable')

    def test_token_collision_preserves_the_existing_session(self):
        folder = self.session()
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        with patch.object(launcher.secrets, 'token_hex', return_value=self.token), \
                patch.object(launcher.subprocess, 'Popen') as spawn:
            self.assertEqual(launcher.launch('앱')['status'], 'blocked')
        spawn.assert_not_called()
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, before)

    def test_expired_never_started_session_fails_without_pid_probes(self):
        self.session(created=time.time() - launcher.STARTUP_GRACE_SECONDS - 1)
        self.assertEqual(launcher.status(self.token)['diagnostic'], 'recorder_exited_without_receipt')

    def test_untrusted_tokens_never_touch_the_filesystem(self):
        for token in ('../request', 'a' * 31, 'A' * 32, 'a' * 33, '', None, '..\\' + 'a' * 32):
            with self.subTest(token=token), patch.object(launcher.tasks, '_directory') as directory:
                self.assertEqual(launcher.status(token)['diagnostic'], 'invalid_recorder_session')
                directory.assert_not_called()

    def test_redirected_session_file_is_rejected_before_read(self):
        folder = self.session()
        real_check = launcher.tasks._deny_redirect
        def deny(path):
            if path == folder / 'events.jsonl':
                raise launcher.tasks.TaskError('redirected_task_path')
            return real_check(path)
        with patch.object(launcher.tasks, '_deny_redirect', side_effect=deny):
            self.assertEqual(launcher.status(self.token)['diagnostic'], 'redirected_task_path')

    def test_oversized_events_are_bounded(self):
        self.session(events=b'x' * 257)
        with patch.object(launcher, 'MAX_EVENT_BYTES', 256):
            self.assertEqual(launcher.status(self.token)['diagnostic'], 'recorder_session_file_too_large')

    def test_non_file_events_are_rejected(self):
        folder = self.session()
        (folder / 'events.jsonl').unlink()
        (folder / 'events.jsonl').mkdir()
        self.assertEqual(launcher.status(self.token)['diagnostic'], 'recorder_session_file_invalid')

    def test_bad_request_never_starts_a_process(self):
        with patch.object(launcher.subprocess, 'Popen') as spawn:
            for app_hint, task, expected in (
                    ('', None, 'invalid_recorder_request'),
                    ('x' * 201, None, 'invalid_recorder_request'),
                    ('a\nb', None, 'invalid_recorder_request'),
                    ('앱', 'bad:name', 'invalid_task_name'),
                    ('앱', '"작업"', 'invalid_task_name'),
                    ('앱', ' padded ', 'invalid_task_name')):
                with self.subTest(app_hint=app_hint, task=task):
                    self.assertEqual(launcher.launch(app_hint, task)['diagnostic'], expected)
                    self.assertFalse(self.store.parent.exists())
            spawn.assert_not_called()

    def test_spawn_error_is_actionable(self):
        with patch.object(launcher.subprocess, 'Popen', side_effect=OSError('no process')):
            result = launcher.launch('앱')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['diagnostic'], 'recorder_session_unavailable')
        self.assertEqual(launcher.status(result['session'])['diagnostic'], result['diagnostic'])


if __name__ == '__main__':
    unittest.main()
