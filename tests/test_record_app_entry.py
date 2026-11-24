"""App-name startup hands a pinned native window to the existing recorder."""
import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app


class AppRecordingEntryTests(unittest.TestCase):
    def test_window_snapshot_caption_and_process_lifetime_reach_original_editor(self):
        import recorder_targets as targets
        row = dict(exe_name='Example.exe', title='조건  "A" & [$value] (1) ',
                   hwnd=42, pid=13, process_started_utc_ticks='1234')
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(targets, 'inventory', return_value=[row]) as inventory,
              patch.object(targets, 'choose_window', return_value=row),
              patch.object(app.bridge, 'Binding') as binding,
              patch.object(app.bridge, 'set_dpi_awareness'),
              patch.object(app.bridge, 'UIA', side_effect=AssertionError('no UIA before Start')),
              patch.object(app, 'windows', side_effect=AssertionError('no duplicate inventory')),
              patch.object(app, 'guard') as guard,
              patch.object(app, 'task_directory', return_value=Path(folder)),
              patch.object(app, 'edit_recording', return_value={'status': 'cancelled'}) as editor):
            result = app.record_application('Example')
            self.assertEqual(result, {'status': 'cancelled'})
            inventory.assert_called_once_with()
            binding.assert_called_once_with(row)
            guard.assert_called_once_with(binding.return_value, binding.return_value, row['title'])
            owner, storage = editor.call_args.args
            self.assertIs(owner, binding.return_value)
            self.assertEqual((storage.exe, storage.title), (row['exe_name'], row['title']))
            self.assertTrue(editor.call_args.kwargs['report_ready'])
            app.tasks._name(storage.name)

    def test_picker_cancel_has_no_binding_or_recording(self):
        import recorder_targets as targets
        with (patch.object(targets, 'inventory', return_value=[]),
              patch.object(targets, 'choose_window', return_value=None),
              patch.object(app.bridge, 'Binding') as binding,
              patch.object(app.bridge, 'set_dpi_awareness')):
            self.assertEqual(app.record_application('Missing'),
                             dict(status='cancelled', input_dispatched=False))
            binding.assert_not_called()

    def test_stale_window_is_not_replaced_with_a_fresh_inventory_match(self):
        import recorder_targets as targets
        row = dict(exe_name='Example.exe', title='Title', hwnd=42, pid=13,
                   process_started_utc_ticks='1234')
        with (patch.object(targets, 'inventory', return_value=[row]) as inventory,
              patch.object(targets, 'choose_window', return_value=row),
              patch.object(app.bridge, 'set_dpi_awareness'),
              patch.object(app.bridge, 'Binding', side_effect=app.bridge.BridgeError('target_window_mismatch')),
              patch.object(app, 'record_editor') as editor, redirect_stdout(StringIO()) as output):
            code = app.worker(argparse.Namespace(command='_record_app', app='Example', task=None))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())['diagnostic'], 'target_window_mismatch')
            inventory.assert_called_once_with()
            editor.assert_not_called()

    def test_saved_draft_still_gets_same_live_recovery_receipt(self):
        receipt = dict(status='recorded', draft=True, input_dispatched=False)
        with (patch.object(app, 'guard'), patch.object(app, 'task_directory'),
              patch.object(app.review_drafts, 'ReviewStorage'),
              patch.object(app, 'edit_recording', return_value=receipt),
              patch.object(app, 'attach_recovery', return_value=dict(receipt, host_action='use_prepared_task')) as recovery):
            result = app.record_editor(object(), 'New', 'App.exe', 'Title', report_ready=True)
            recovery.assert_called_once_with(receipt, 'New', prepare=True)
            self.assertEqual(result['host_action'], 'use_prepared_task')

    def test_saved_draft_reports_processing_before_recovery_to_avoid_reasking_save(self):
        receipt = dict(status='recorded', draft=True, input_dispatched=False)
        with (patch.object(app, 'guard'), patch.object(app, 'task_directory'),
              patch.object(app.review_drafts, 'ReviewStorage'),
              patch.object(app, 'edit_recording', return_value=receipt),
              redirect_stdout(StringIO()) as output):
            def recover(*_, **__):
                self.assertEqual(json.loads(output.getvalue()), dict(
                    status='recorder_processing', task='New', saved=True, input_dispatched=False))
                return receipt
            with patch.object(app, 'attach_recovery', side_effect=recover):
                self.assertEqual(app.record_editor(object(), 'New', 'App.exe', 'Title',
                                 report_ready=True, report_progress=True), receipt)

    def test_public_command_routes_app_and_session_without_request_file(self):
        import recorder_launch as launcher
        with (patch.object(launcher, 'launch', return_value={'status': 'recorder_ready'}) as start,
              patch.object(launcher, 'status', return_value={'status': 'recording'}) as status,
              patch.object(app, 'recorder_request', side_effect=AssertionError('no request JSON'))):
            self.assertEqual(app.run(app.parser().parse_args(['recorder', '--app', 'FXMENUAGNET'])),
                             {'status': 'recorder_ready'})
            start.assert_called_once_with('FXMENUAGNET', task=None)
            self.assertEqual(app.run(app.parser().parse_args(['recorder', '--session', 'a' * 32])),
                             {'status': 'recording'})
            status.assert_called_once_with('a' * 32)

    def test_unnamed_app_uses_one_picker_with_optional_task_name(self):
        import recorder_launch as launcher
        import recorder_targets as targets
        with (patch.object(launcher, 'launch') as start,
              patch.object(targets, 'inventory', return_value=[]) as inventory,
              patch.object(targets, 'choose_window', return_value=None) as picker,
              patch.object(app.bridge, 'Binding') as binding, patch.object(app.bridge, 'set_dpi_awareness')):
            for options, task in (([], None), (['--task', '설비 조회'], '설비 조회')):
                args = app.parser().parse_args(['recorder', *options])
                app.run(args)
                start.assert_called_with(None, task=task)
            self.assertEqual(app.record_application(None), dict(status='cancelled', input_dispatched=False))
            inventory.assert_called_once_with()
            self.assertEqual(picker.call_args.args, ('', []))
            binding.assert_not_called()
