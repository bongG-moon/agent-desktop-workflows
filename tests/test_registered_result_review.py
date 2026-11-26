"""Resume a registered lookup locally, without redispatching its query or sharing its image."""
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import random
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import grounding
import local_result_review
import registered_result
import result_cell
import workflow_repair


class RegisteredResultTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict('os.environ', LOCALAPPDATA=str(self.directory / 'localappdata')))
        self.path = self.directory / 'workflow.json'
        (self.directory / 'scripts').mkdir()
        (self.directory / 'scripts/run.py').touch()
        scope = dict(exe='Demo.exe', title='Details', after_step=3, related=True)
        self.recipe = dict(schema=1, name='Example lookup', exe='Demo.exe', window='Lookup', steps=[
            dict(action='invoke', selector=dict(role='button', automation_id='query')),
            dict(action='verify-value', selector=dict(role='table'), result_column='Part', parameter='Part'),
            dict(action='double-click-result', label='Open matching row'),
            dict(action='read-value', label='Note', selector=dict(role='textfield', automation_id='note'), window_context=scope),
            dict(action='invoke', label='Exit', verify_closed=True, selector=dict(role='button', automation_id='close'), window_context=scope)])
        self.path.write_text(json.dumps(self.recipe), encoding='utf8')
        self.owner = dict(hwnd=7, pid=19, process_started_utc_ticks=11, exe_name='Demo.exe', title='Lookup')
        self.blocked = dict(status='blocked', stopped_at=2, input_dispatched=True, steps=[
            dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True)),
            dict(number=2, receipt=dict(status='blocked', diagnostic='result_table_missing',
                                        input_dispatched=False, result_window=self.owner))])
        self.group = dict(role='group', automation_id='PaintedGrid')
        self.box = dict(x=105, y=107, width=111, height=101)
        self.pixels = random.Random(223).randbytes(128 * 128 * 4)
        self.png = grounding._png(128, 128, self.pixels)
        self.frame = ([100,100,128,128], self.png)
        self.choose = stack.enter_context(patch.object(local_result_review, 'choose'))
        self.run = stack.enter_context(patch.object(flow, 'run', return_value=self.blocked))
        stack.enter_context(patch.object(workflow_repair.tempfile, 'gettempdir', return_value=str(self.directory)))
        stack.enter_context(patch.object(grounding, '_directory', return_value=self.directory))
        self.capture = stack.enter_context(patch.object(grounding, '_capture', side_effect=self.snapshot))
        self.dpi = stack.enter_context(patch.object(flow.app.bridge, 'set_dpi_awareness'))
        self.foreground = stack.enter_context(patch.object(flow.row_action, 'foreground'))
        stack.enter_context(patch.object(flow.app.bridge, 'Binding'))
        self.uia = stack.enter_context(patch.object(flow.app.bridge, 'UIA'))
        stack.enter_context(patch.object(flow.app, 'guard'))
        stack.enter_context(patch.object(flow.app.visibility, 'require_observable'))
        stack.enter_context(patch.object(flow, 'result_surface', side_effect=flow.WorkflowError('result_table_missing')))
        stack.enter_context(patch.object(flow, '_group_for_column', side_effect=flow.WorkflowError('result_table_missing')))
        self.hit = stack.enter_context(patch.object(result_cell, 'bound_group', return_value=(self.group, self.box)))
        stack.enter_context(patch.object(flow, 'check_result_point'))
        self.proof = dict(header=[13, 19, 67], point=[48, 51], value='D27')

    def snapshot(self, *args, group_boxes=None):
        if group_boxes is not None:
            group_boxes['automation_id','PaintedGrid'] = self.box.copy()
        return self.frame

    def changed_at(self, x, y):
        copy = bytearray(self.pixels)
        copy[4*(y*128+x)] ^= 255
        return self.frame[0], grounding._png(128,128,copy)

    def call(self, *args):
        with redirect_stdout(StringIO()) as out:
            code = flow.main(['--_worker', 'run', '--recipe', str(self.path), *args])
        return code, json.loads(out.getvalue())

    def start(self):
        code, report = self.call('--set', 'Part=D27', '--allow-physical')
        self.assertEqual(code, 2)
        self.assertEqual(report['host_action'], 'confirm_result_locally_without_replaying', report)
        self.assertEqual(self.capture.call_count, 0)
        return report['result_review']['resume_args']

    def finished(self):
        return dict(status='completed_unverified', input_dispatched=True, steps=[
            dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True)),
            dict(number=2, receipt=dict(status='observed', value='sample note', value_evidence=dict(status='observed'), input_dispatched=False)),
            dict(number=3, receipt=dict(status='verified', input_dispatched=True))])

    def test_previous_guide_duplicate_set_can_resume_only_with_the_same_value(self):
        args = self.start()
        self.assertIn('part=D27', args)
        self.assertEqual(self.call(*args, '--set', 'Part=Other')[1]['diagnostic'], 'duplicate_override')
        self.assertEqual(self.run.call_count, 1)
        self.choose.return_value = self.proof
        self.run.return_value = self.finished()
        _, resumed = self.call(*args, '--set', 'Part=D27')
        self.assertEqual(resumed['status'], 'completed_unverified', resumed)
        self.assertEqual(resumed['resumed_at'], 3)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.call('--allow-physical', '--set', 'Part=D27', '--set', 'part=D27')[1]['diagnostic'],
                         'duplicate_override')

    def row_review(self, mode='single'):
        scope = dict(exe='Demo.exe', title='Details', after_step=2, related=True)
        self.recipe['steps'][1:3] = [dict(action='double-click-result', label='Open current row',
                                              result_row=mode, selector=self.group)]
        for step in self.recipe['steps'][2:]:
            step['window_context'] = deepcopy(scope)
        self.path.write_text(json.dumps(self.recipe), encoding='utf8')
        self.blocked['steps'][-1]['receipt']['diagnostic'] = 'result_row_unavailable'
        session = workflow_repair.RepairSession(self.path)
        review = registered_result.ResultReview(session, self.recipe, self.path)
        record = review.offer(flow, self.blocked, {})
        return review, record

    def test_current_row_offer_has_no_value_hash_and_rejects_missing_or_ambiguous_rows(self):
        review, record = self.row_review()
        self.assertEqual(record['row_policy'], 'single')
        self.assertNotIn('expected', record)
        self.capture.assert_not_called()
        for diagnostic in ('result_row_missing', 'result_row_ambiguous', 'result_table_missing'):
            self.blocked['steps'][-1]['receipt']['diagnostic'] = diagnostic
            self.assertIsNone(review.offer(flow, self.blocked, {}))

    def test_header_stop_reuses_no_query_and_never_offers_cell_review(self):
        self.recipe['steps'][1:3] = [dict(action='double-click-result', result_row='header',
            selector=self.group, result_column='Part', result_point=dict(x=10,y=8,width=80,height=40))]
        for step in self.recipe['steps'][2:]:
            step['window_context']['after_step'] = 2
        self.path.write_text(json.dumps(self.recipe), encoding='utf8')
        self.blocked['diagnostic'] = self.blocked['steps'][-1]['receipt']['diagnostic'] = 'result_header_point_mismatch'
        self.blocked['inspection_owner'] = self.owner
        with patch.object(registered_result, 'ResultReview', side_effect=AssertionError('header used cell review')):
            _, consent = self.call()
            self.assertEqual(consent['status'], 'needs_consent')
            self.run.assert_not_called()
            _, failed = self.call('--allow-physical', '--local-result-review')
            self.assertTrue(failed['input_dispatched'])
            self.assertEqual(failed['inspect_args'], ['--read-only', '--only', '2', '--summary'])
            with patch('time.time', return_value=time.time() + 3600):
                _, repeated = self.call('--allow-physical', '--local-result-review', '--hwnd', str(self.owner['hwnd']))
            self.assertFalse(repeated['input_dispatched'])
            self.assertTrue(repeated['previous_input_dispatched'])
            self.assertEqual(self.run.call_count, 1)
            _, retry = self.call('--allow-physical', '--new-attempt')
            self.assertTrue(retry['input_dispatched'])
        self.assertEqual(self.run.call_count, 2)
        self.capture.assert_not_called()

    def test_plain_recipe_header_requires_consent_before_query(self):
        self.path = self.directory / 'trial.json'
        header = dict(action='double-click-result', result_row='header', selector=self.group,
                      result_column='Part', result_point=dict(x=10, y=8, width=80, height=40))
        self.path.write_text(json.dumps(dict(self.recipe, steps=[self.recipe['steps'][0], header])), encoding='utf8')
        code, report = self.call()
        self.assertEqual((code, report['status'], report['input_dispatched']), (2, 'needs_consent', False))
        self.run.assert_not_called()

    def test_worker_rejects_a_changed_recipe_before_dispatch(self):
        digest = workflow_repair.digest(self.recipe)
        self.recipe['steps'][0]['label'] = 'Updated after the requested run'
        self.path.write_text(json.dumps(self.recipe), encoding='utf8')
        _, refused = self.call('--allow-physical', '--_recipe-sha', digest)
        self.assertEqual(refused['diagnostic'], 'workflow_changed_before_worker')
        self.assertFalse(refused['input_dispatched'])
        self.run.assert_not_called()

    def test_window_aliases_recover_the_existing_pending_review(self):
        hwnd = 0x81aa0011
        self.owner['hwnd'] = hwnd
        review, record = self.row_review()
        for alias in (hwnd - 2**32, 0xffffffff00000000 | hwnd):
            self.assertEqual(review.existing({}, None, alias)['ticket'], record['ticket'])
        with self.assertRaisesRegex(flow.app.tasks.TaskError, 'invalid_target'):
            review.existing({}, None, 0x100000000 | hwnd)
        self.assertEqual(review.existing({}, None, hwnd)['ticket'], record['ticket'])

    def test_local_current_row_confirmation_skips_header_and_value_and_runs_only_suffix(self):
        for mode in ('single', 'first'):
            with self.subTest(mode=mode):
                if mode == 'first':
                    # Keep the same three suffix operations; only the declared row policy changes.
                    self.recipe['steps'][1]['result_row'] = mode
                    self.path.write_text(json.dumps(self.recipe), encoding='utf8')
                    review = registered_result.ResultReview(workflow_repair.RepairSession(self.path), self.recipe, self.path)
                    record = review.offer(flow, self.blocked, {})
                else:
                    review, record = self.row_review(mode)
                self.choose.return_value = dict(point=[48,51], row_policy=mode, confirmed=True)
                self.run.return_value = self.finished()
                self.uia.return_value.__enter__.return_value.bounds.return_value = self.box
                with (patch.object(flow, '_result_target', return_value=(MagicMock(), self.group)),
                      patch.object(review, '_check_header', side_effect=AssertionError('no header needed'))):
                    report = review.confirm(flow, record, self.path, {})
                self.assertEqual([r['number'] for r in report['steps']], [2,3,4])
                self.assertEqual(report['result_match_source'], 'local_row_confirmation')
                self.assertFalse(report['prior_steps_replayed'])
                self.assertEqual(self.choose.call_args.kwargs['row_policy'], mode)
                self.assertIsNone(self.choose.call_args.kwargs['expected'])
                self.assertEqual([s['action'] for s in self.run.call_args.args[0]['steps']],
                                 ['visual-click','read-value','invoke'])
                self.assertEqual(self.run.call_args.args[0]['steps'][1]['window_context']['after_step'], 1)
                self.assertEqual(flow.load(self.path), self.recipe)
                self.assertFalse(list(self.directory.glob('*.png')))
                self.hit.assert_not_called()

    def test_confirmed_demonstrated_point_persists_only_on_the_tested_registered_recipe(self):
        review, record = self.row_review('recorded')
        self.uia.return_value.__enter__.return_value.bounds.return_value = self.box
        self.choose.return_value = dict(point=[48,51], row_policy='recorded', confirmed=True)
        self.run.return_value = self.finished()
        with patch.object(flow, '_result_target', return_value=(MagicMock(), self.group)):
            report = review.confirm(flow, record, self.path, {})
        self.assertTrue(report['correction_saved'])
        self.assertFalse(report['prior_steps_replayed'])
        expected = deepcopy(self.recipe)
        expected['steps'][1]['result_point'] = dict(x=43, y=44, width=111, height=101)
        self.assertEqual(flow.load(self.path), expected)
        self.assertEqual(workflow_repair.approval_state(self.path), '')

    def test_current_row_local_confirmation_keeps_scope_choice_and_frame_guards(self):
        review, record = self.row_review()
        self.uia.return_value.__enter__.return_value.bounds.return_value = self.box
        with patch.object(flow, '_result_target', return_value=(MagicMock(), self.group)):
            for choice in (dict(point=[48,51], row_policy='first', confirmed=True),
                           dict(point=[48,51], row_policy='single', confirmed=False),
                           dict(point=[127,127], row_policy='single', confirmed=True)):
                self.choose.return_value = choice
                report = review.confirm(flow, record, self.path, {})
                self.assertEqual(report['diagnostic'], 'result_row_not_confirmed')
                self.assertFalse(list(self.directory.glob('*.png')))
            self.choose.return_value = dict(point=[48,51], row_policy='single', confirmed=True)
            self.capture.side_effect = lambda *a, **k: self.snapshot(*a, **k) if not self.foreground.called else self.changed_at(82,30)
            self.assertEqual(review.confirm(flow, record, self.path, {})['diagnostic'], 'result_frame_changed')
            self.assertFalse(list(self.directory.glob('*.png')))
        self.run.assert_not_called()

    def test_current_row_missing_surface_never_prompts_for_an_unscoped_click(self):
        review, record = self.row_review()
        with patch.object(flow, '_result_target', side_effect=flow.WorkflowError('result_table_missing')):
            report = review.confirm(flow, record, self.path, {})
        self.assertEqual(report['diagnostic'], 'result_table_missing')
        self.choose.assert_not_called()
        self.capture.assert_not_called()
        self.run.assert_not_called()

    def test_row_provider_becomes_readable_on_resume_without_query_or_image(self):
        review, record = self.row_review()
        self.run.return_value = self.finished()
        with patch.object(flow, 'observe_result_row', return_value=dict(status='observed')) as probe:
            report = review.confirm(flow, record, self.path, {}, try_elements=True)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(probe.call_args.kwargs['wait_seconds'], 0)
        self.assertEqual(report['result_match_source'], 'accessibility_row')
        self.assertEqual([r['number'] for r in report['steps']], [2,3,4])
        self.assertEqual([s['action'] for s in self.run.call_args.args[0]['steps']],
                         ['double-click-result','read-value','invoke'])
        self.assertEqual(self.run.call_args.args[0]['steps'][1]['window_context']['after_step'], 1)
        self.choose.assert_not_called()
        self.capture.assert_not_called()

    def test_registered_row_review_one_call_keeps_action_number_and_never_adds_value_verification(self):
        review, record = self.row_review()
        review.pending.unlink()  # Start a fresh registered run, not a prior ticket.
        self.recipe['steps'][1].pop('label')
        self.path.write_text(json.dumps(self.recipe), encoding='utf8')
        self.run.side_effect = [deepcopy(self.blocked), self.finished()]
        self.choose.return_value = dict(point=[48,51], row_policy='single', confirmed=True)
        self.uia.return_value.__enter__.return_value.bounds.return_value = self.box
        with patch.object(flow, '_result_target', return_value=(MagicMock(), self.group)):
            code, report = self.call('--allow-physical', '--local-result-review')
        self.assertEqual(code, 3, report)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual([r['number'] for r in report['steps']], [1,2,3,4])
        self.assertEqual(report['steps'][1]['receipt']['status'], 'delivered_unverified')
        self.assertEqual(report['result_match_source'], 'local_row_confirmation')
        self.assertFalse(report['prior_steps_replayed'])
        self.assertNotIn('value_evidence', report['steps'][1]['receipt'])

    def test_registration_reuses_pending_query_and_runs_only_verified_detail_then_caches_surface(self):
        args = self.start()
        _, repeat = self.call('--set', 'part=D27', '--allow-physical')
        self.assertEqual(repeat['result_review']['resume_args'], args)
        self.assertEqual(self.run.call_count, 1)
        self.assertFalse(repeat['input_dispatched'])
        self.choose.side_effect = [dict(refresh=True), self.proof]
        self.run.return_value = self.finished()
        _, result = self.call(*args)
        self.dpi.assert_called_once_with()
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual([s['action'] for s in self.run.call_args.args[0]['steps']], ['visual-click','read-value','invoke'])
        self.assertEqual(self.run.call_args.kwargs['result_confirmation']['selector'], self.group)
        self.assertEqual(self.run.call_args.kwargs['hwnd'], self.owner['hwnd'])
        self.assertEqual([s['number'] for s in result['steps']], [3, 4, 5])
        self.assertEqual(result['steps'][1]['receipt']['value'], 'sample note')
        self.assertFalse(result['prior_steps_replayed'])
        self.assertEqual(result['host_action'], 'report_execution_receipt')
        self.assertEqual(self.capture.call_count, 3)
        self.assertFalse(list(self.directory.glob('*.png')))
        self.assertNotIn('.png', json.dumps(result))
        self.assertEqual(self.hit.call_args.args[-2:], ((140,119), (148,151)))
        self.assertEqual(flow.load(self.path), self.recipe)
        self.run.return_value = self.finished()
        self.call('--set', 'Part=Other', '--allow-physical')
        self.assertEqual(self.run.call_args.kwargs['result_surface_hint'], dict(number=2, selector=self.group))
        self.assertNotIn('D27', next(self.directory.rglob('result-surface.json')).read_text())
        self.assertEqual(self.call(*args)[1]['diagnostic'], 'result_review_expired')

    def test_ocr_confusion_wrong_column_changed_frame_cancel_and_consent_never_send_a_click(self):
        args = self.start()
        self.assertNotIn('D27', next(self.directory.rglob('result-review.json')).read_text())
        self.assertEqual(self.call(*[a for a in args if a != '--allow-physical'])[1]['status'], 'needs_consent')
        for choice, code in ((None, 'local_result_review_cancelled'),
                             (dict(self.proof, value='027'), 'confirmed_result_value_mismatch'),
                             (dict(self.proof, point=[78,51]), 'result_point_outside_column')):
            self.choose.return_value = choice
            _, report = self.call(*args)
            self.assertEqual(report['diagnostic'], code)
            self.assertFalse(report['input_dispatched'])
            self.assertEqual(report['result_review']['resume_args'], args)
        self.foreground.assert_not_called()
        self.choose.return_value = self.proof
        self.capture.side_effect = lambda *a, **k: self.snapshot(*a, **k) if k else self.changed_at(9,10)
        self.assertEqual(self.call(*args)[1]['diagnostic'], 'result_frame_changed')
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.hit.call_count, 1)

    def test_clock_outside_table_can_change_but_any_other_cell_or_table_geometry_cannot(self):
        args = self.start()
        self.choose.return_value = self.proof
        self.run.return_value = self.finished()
        for x, y in ((7, 7), (110, 104)):
            self.capture.side_effect = lambda *a, **k: self.snapshot(*a, **k) if k else self.changed_at(x,y)
            self.assertEqual(self.call(*args)[1]['diagnostic'], 'result_frame_changed')
        self.hit.return_value = self.group, dict(self.box, y=110)
        self.assertEqual(self.call(*args)[1]['diagnostic'], 'result_table_changed')
        self.assertEqual(self.run.call_count, 1)
        self.hit.return_value = self.group, self.box
        self.capture.side_effect = lambda *a, **k: self.snapshot(*a, **k) if k else self.changed_at(2,2)
        _, report = self.call(*args)
        self.assertEqual(report['status'], 'completed_unverified')
        self.assertEqual(self.run.call_count, 2)

    def test_final_before_click_rechecks_table_pixels_and_bounds(self):
        record = dict(receipt='1'*64, frame='2'*64, selector=self.group, point=[48,51],
                      window_rect=self.frame[0], table_box=self.box)
        with (patch.object(grounding,'read_frame',return_value=(None,self.png)),
              patch.object(flow,'check_result_point',return_value=self.box) as live_box):
            self.capture.side_effect = None
            self.capture.return_value = self.changed_at(2,2)
            flow.check_result_frame(MagicMock(),record,(148,151),lambda:None)
            self.capture.return_value = self.changed_at(82,30)
            with self.assertRaisesRegex(ValueError, 'result_frame_changed'):
                flow.check_result_frame(MagicMock(),record,(148,151),lambda:None)
            self.capture.return_value = self.frame
            live_box.return_value = dict(self.box, width=100)
            with self.assertRaisesRegex(ValueError, 'result_table_changed'):
                flow.check_result_frame(MagicMock(),record,(148,151),lambda:None)

    def test_local_opt_in_runs_as_one_call_and_preserves_accessibility_diagnostic(self):
        self.run.side_effect = [deepcopy(self.blocked), dict(self.finished(), status='verified')]
        self.choose.return_value = self.proof
        _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
        self.assertEqual(self.choose.call_args.kwargs['expected'], 'D27')
        self.assertEqual([s['number'] for s in report['steps']], list(range(1,6)))
        self.assertEqual(report['steps'][1]['receipt']['provider_diagnostic'], 'result_table_missing')
        self.assertEqual(report['steps'][1]['receipt']['value_evidence']['source'], 'local_visual_confirmation')
        self.assertEqual(report['status'], 'completed_unverified')  # Query delivery is still unverified.
        self.assertEqual(self.run.call_count, 2)

    def test_foreground_and_input_lease_start_after_choice_and_end_before_detail_suffix(self):
        state = dict(locked=False, chosen=False, activated=False)
        @contextmanager
        def lease():
            self.assertFalse(state['locked'])
            state['locked'] = True
            try:
                yield
            finally:
                state['locked'] = False
        def choose(*args, **kwargs):
            self.assertFalse(state['locked'])
            self.assertFalse(state['activated'])
            state['chosen'] = True
            return self.proof
        def foreground(*args):
            self.assertTrue(state['locked'])
            self.assertTrue(state['chosen'])
            state['activated'] = True
        def capture(*args, **kwargs):
            self.assertEqual(state['locked'],state['activated'])
            return self.snapshot(*args, **kwargs)
        def run(*args, **kwargs):
            self.assertFalse(state['locked'])
            return self.finished() if state['chosen'] else deepcopy(self.blocked)
        self.choose.side_effect = choose
        self.foreground.side_effect = foreground
        self.capture.side_effect = capture
        self.run.side_effect = run
        with patch.object(flow.coordination,'desktop_input',side_effect=lease):
            _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
        self.foreground.assert_called_once()
        self.assertEqual(self.run.call_count,2)
        self.assertFalse(report['prior_steps_replayed'])
        self.assertEqual([s['action'] for s in self.run.call_args.args[0]['steps']],['visual-click','read-value','invoke'])

    def test_exposing_reviewed_window_still_rejects_a_moved_capture(self):
        self.choose.return_value = self.proof
        self.capture.side_effect = lambda *args, **kwargs: (
            ([101,100,128,128],self.png) if self.foreground.called else self.snapshot(*args, **kwargs))
        _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
        self.foreground.assert_called_once()
        self.assertEqual(report['review_diagnostic'],'result_frame_changed')
        self.assertEqual(self.run.call_count,1)

    def test_oversized_recorded_table_is_clipped_for_review_but_raw_geometry_is_preserved(self):
        self.box = dict(x=110,y=297,width=1393,height=562)
        self.pixels = random.Random(224).randbytes(1296*839*4)
        self.png = grounding._png(1296,839,self.pixels)
        self.frame = ([100,100,1296,839],self.png)
        self.proof = dict(header=[30,210,100],point=[55,240],value='D27')
        self.choose.return_value = self.proof
        self.run.side_effect = [deepcopy(self.blocked),self.finished()]
        self.uia.return_value.__enter__.return_value.bounds.return_value = self.box
        with (patch.object(flow,'_result_target',return_value=(MagicMock(),self.group)),
              patch.object(registered_result.ResultReview,'_check_header'),
              patch.object(flow,'check_result_point') as point):
            _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
        self.assertEqual(self.choose.call_args.kwargs['table_box'],[10,197,1286,562])
        self.assertEqual(self.choose.call_args.kwargs['frame_size'],[1296,839])
        confirmation = self.run.call_args.kwargs['result_confirmation']
        self.assertEqual(confirmation['table_box'],self.box)
        self.assertEqual(confirmation['point'],[55,240])
        self.assertEqual(point.call_args.args[-1],(155,340))
        self.assertEqual(point.call_args.kwargs['reviewed_frame'],self.frame[0])
        self.assertEqual([s['action'] for s in self.run.call_args.args[0]['steps']],['visual-click','read-value','invoke'])
        self.assertEqual(self.run.call_count,2)
        self.assertEqual(report['status'],'completed_unverified')
        self.assertFalse(report['prior_steps_replayed'])

    def test_table_outside_frame_reports_raw_geometry_without_opening_review_or_replaying(self):
        self.box = dict(x=300,y=107,width=111,height=101)
        self.uia.return_value.__enter__.return_value.bounds.return_value = self.box
        with patch.object(flow,'_result_target',return_value=(MagicMock(),self.group)):
            _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
        self.assertEqual(report['review_diagnostic'],'result_table_outside_frame')
        self.assertEqual(report['result_geometry'],dict(capture_rect=self.frame[0],table_absolute=self.box,
                                                       table_relative=[200,7,111,101]))
        self.assertIn('resume_args',report['result_review'])
        self.choose.assert_not_called()
        self.assertEqual(self.run.call_count,1)

    def test_clipped_review_still_rejects_geometry_change_and_keeps_exact_image_diagnostic(self):
        self.box = dict(x=105,y=107,width=150,height=101)
        uia = self.uia.return_value.__enter__.return_value
        self.choose.return_value = self.proof
        self.run.return_value = deepcopy(self.blocked)
        with (patch.object(flow,'_result_target',return_value=(MagicMock(),self.group)),
              patch.object(registered_result.ResultReview,'_try_elements',return_value=None),
              patch.object(registered_result.ResultReview,'_check_header')):
            args = self.start()
            uia.bounds.side_effect = [self.box,dict(self.box,width=151)]
            _, changed = self.call(*args)
            self.assertEqual(changed['diagnostic'],'result_table_changed')
            self.assertFalse(changed['input_dispatched'])
            self.assertEqual(changed['result_geometry']['visible_relative'],[5,7,123,101])
            self.assertEqual(changed['result_review']['resume_args'],args)
            uia.bounds.side_effect = None
            uia.bounds.return_value = self.box
            self.choose.side_effect = local_result_review.ResultReviewError(
                'result_capture_size_mismatch',capture_size=[128,128],image_size=[127,128])
            _, mismatch = self.call(*args)
            self.assertEqual(mismatch['diagnostic'],'result_capture_size_mismatch')
            self.assertEqual(mismatch['result_geometry']['image_size'],[127,128])
            self.assertFalse(mismatch['input_dispatched'])
        self.assertEqual(self.run.call_count,1)

    def test_cancelled_local_review_reports_time_spent_after_the_query(self):
        self.blocked['elapsed_seconds'] = 4.5
        with patch.object(workflow_repair.time, 'monotonic', return_value=100) as clock:
            def cancel(*args, **kwargs):
                clock.return_value = 118
                return None
            self.choose.side_effect = cancel
            _, report = self.call('--set', 'Part=D27', '--allow-physical', '--local-result-review')
        self.assertEqual(report['review_diagnostic'], 'local_result_review_cancelled')
        self.assertEqual(report['elapsed_seconds'], 18)
        self.assertEqual(report['original_elapsed_seconds'], 4.5)
        self.assertEqual(self.run.call_count, 1)

    def test_refresh_that_can_read_elements_keeps_one_native_verification_receipt(self):
        self.choose.side_effect = [dict(refresh=True)]
        observation = dict(status='verified',value='D27',input_dispatched=False,
                           value_evidence=dict(source='grid_pattern',status='observed'))
        suffix = self.finished()
        suffix['steps'].insert(0,dict(receipt=observation))
        for number,row in enumerate(suffix['steps'],1):
            row['number'] = number
        self.run.side_effect = [deepcopy(self.blocked),suffix]
        with patch.object(flow,'observe_result',return_value=observation):
            _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
        self.assertEqual(report['result_match_source'],'accessibility')
        self.assertEqual([s['number'] for s in report['steps']],list(range(1,6)))
        self.assertEqual(report['steps'][1]['receipt']['value_evidence']['source'],'grid_pattern')
        self.choose.assert_called_once()

    def test_separate_ticket_reads_late_provider_without_an_image_or_replaying_query(self):
        args = self.start()
        table = dict(selector=dict(role='group', automation_id='Grid'), source='element')
        self.run.return_value = self.finished()
        self.run.return_value['steps'].insert(0, dict(number=0, receipt=dict(status='verified', input_dispatched=False)))
        for i,row in enumerate(self.run.return_value['steps'],1):
            row['number'] = i
        with patch.object(flow, 'observe_result', return_value=dict(status='verified',result_element=table)) as probe:
            self.assertEqual(args.count('--set'), 1)
            _, report = self.call(*args)
        self.assertEqual(probe.call_args.args[3], 'D27')
        recipe = self.run.call_args.args[0]
        self.assertEqual([s['action'] for s in recipe['steps']], ['verify-value','double-click-result','read-value','invoke'])
        self.assertEqual(recipe['steps'][0]['value'], 'D27')
        self.assertEqual(recipe['steps'][-1]['window_context']['after_step'],2)
        self.assertEqual([r['number'] for r in report['steps']],[2,3,4,5])
        self.assertEqual(report['result_match_source'],'accessibility')
        self.assertFalse(report['prior_steps_replayed'])
        self.capture.assert_not_called()
        self.choose.assert_not_called()

    def test_readable_missing_or_duplicate_result_offers_no_images(self):
        for reason in ('result_value_not_found', 'result_cell_ambiguous', 'result_column_ambiguous'):
            self.run.return_value = deepcopy(self.blocked)
            self.run.return_value['steps'][-1]['receipt']['diagnostic'] = reason
            _, report = self.call('--set','Part=D27','--allow-physical','--local-result-review')
            self.assertNotIn('result_review', report)
        self.capture.assert_not_called()

    def test_noninteractive_standalone_verification_caches_each_step_without_values(self):
        step = self.recipe['steps'][1]
        self.recipe['steps'] = [step, dict(step, result_column='Zone')]
        self.path.write_text(json.dumps(self.recipe),encoding='utf8')
        result = dict(status='verified',input_dispatched=False,steps=[])
        expected = []
        for n, column in enumerate(('Part','Zone'),1):
            hint = dict(source='element', selector=dict(automation_id='Grid'+str(n), role='table'),
                        column_hint=dict(automation_id='Header'+column, role='headeritem'))
            expected.append(dict(number=n, **hint))
            result['steps'].append(dict(number=n,receipt=dict(status='verified',value='D27',input_dispatched=False,
                result_element=hint)))
        self.run.return_value = result
        _, report = self.call('--set','Part=D27','--background-only')
        self.assertTrue(self.run.call_args.kwargs['background_only'])
        self.assertNotIn('result_surface_hint', self.run.call_args.kwargs)
        self.call('--set','Part=Changed','--background-only')
        self.assertEqual(self.run.call_args.kwargs['result_surface_hint'], expected)
        self.assertNotIn('D27',next(self.directory.rglob('result-surface.json')).read_text())
        self.assertEqual(json.loads(self.path.read_text()),self.recipe)
        self.capture.assert_not_called()

    def test_uncertain_suffix_is_claimed_before_dispatch_and_cannot_be_repeated(self):
        args = self.start()
        self.choose.return_value = self.proof
        self.run.side_effect = OSError('uncertain')
        for _ in range(2):
            _, result = self.call(*args)
            self.assertEqual(result['status'], 'uncertain')
            self.assertIsNone(result['input_dispatched'])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.choose.call_count, 1)

    def test_project_hint_survives_temp_cleanup_and_skips_unchanged_writes(self):
        path = self.directory / 'app/.claude/skills/lookup/workflow.json'
        path.parent.mkdir(parents=True)
        session = workflow_repair.RepairSession(path)
        review = registered_result.ResultReview(session,self.recipe,path)
        hint = dict(number=2,source='element',selector=dict(role='group', automation_id='ResultGrid'))
        # The previous release kept its locator in TEMP. Promote it on a verified match.
        review._write(review.temporary_hint,dict(recipe=review.fingerprint,hint=hint))
        self.assertEqual(review.hint(),hint)
        review.remember_elements([hint])
        self.assertEqual(review.hint_path,self.directory/'app/.desktop-select/result-elements/lookup/result-surface.json')
        review.temporary_hint.unlink()
        with patch.object(workflow_repair.tempfile,'gettempdir',return_value=str(self.directory/'empty-temp')):
            resumed = registered_result.ResultReview(workflow_repair.RepairSession(path),self.recipe,path)
            self.assertEqual(resumed.hint(),hint)
            with patch.object(resumed,'_write') as write:
                resumed.remember_elements([hint])
                write.assert_not_called()

    def test_repeat_input_opt_in_survives_separate_resume_only_after_actual_test(self):
        marker = self.directory / 'approval.txt'
        with patch.object(workflow_repair,'approval_path',return_value=marker):
            _, first = self.call('--set','Part=D27','--allow-physical','--remember-physical')
            args = first['result_review']['resume_args']
            self.assertFalse(marker.exists())
            self.choose.return_value = self.proof
            self.run.return_value = self.finished()
            _, done = self.call(*args)
            self.assertEqual(done.get('repeat_physical_input'), 'approved', done)
            self.assertTrue(workflow_repair.approved(self.path, self.recipe))

    def test_live_header_evidence_rejects_a_different_column(self):
        header = MagicMock()
        header.property.return_value = 'Other'
        uia = MagicMock()
        uia.bounds.return_value = dict(x=100,y=100,width=100,height=12)
        with (patch.object(flow.lookup,'find_all',return_value=[header]), patch.object(flow.live,'_visible',return_value=True)):
            with self.assertRaisesRegex(ValueError, 'result_point_outside_column'):
                registered_result.ResultReview._check_header(flow,MagicMock(),uia,MagicMock(),'Part',(150,104),(150,155))
