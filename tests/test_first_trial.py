"""Test the public first-trial command with real recording storage and a counted input boundary."""
from contextlib import redirect_stdout
from io import StringIO
import json
import random
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import first_trial
import learned_tasks as tasks
import recovery
import review_drafts
from test_recorded_plan import button, combo


class FirstTrial(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        raw = combo(label='Region', identifier='REG', sentence='입력받은 변수를 Region에 입력해줘')
        self.task = tasks.from_recording('지역 조회', 'A.exe', '업무 창', [raw, button()])
        self.raw = raw
        self.directory_patch = patch.object(app, 'task_directory', return_value=self.directory)
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.worker = Mock(return_value=dict(status='completed_unverified', input_dispatched=True,
            steps=[dict(number=i, receipt=dict(status='verified',input_dispatched=True)) for i in (1, 2)],
            observations=[dict(field='Region', value='North')]))

    def trial(self, *argv):
        args = app.parser().parse_args(['try-recording', *argv])
        with patch.object(app,'worker_receipt', self.worker):
            return app.run(args)

    def host_receipt(self, *argv):
        with redirect_stdout(StringIO()) as output, patch.object(app, 'worker_receipt', self.worker):
            app.worker(app.parser().parse_args(argv))
        return json.loads(output.getvalue())

    def test_host_summary_returns_one_verbatim_read_value_and_preserves_cached_receipt(self):
        value = 'ABC12345' * 512
        task = dict(self.task, steps=[dict(action='read-value', label='Memo',
                                            selector=dict(role='textfield', automation_id='memo'))])
        tasks.save_task(task, self.directory)
        evidence = dict(status='observed', source='value_pattern')
        self.worker.return_value = dict(status='verified', input_dispatched=False, steps=[dict(number=1,
            label='Memo', receipt=dict(status='observed', diagnostic='value_observed', value=value,
                                       input_dispatched=False, value_evidence=evidence))])
        args = ['try-recording', '--task', task['name']]
        first = self.host_receipt(*args, '--summary')
        path = self.directory / 'first-trials' / (first['trial_receipt'] + '.json')
        saved = path.read_bytes()
        raw = self.host_receipt(*args)
        compact = self.host_receipt(*args, '--summary')
        self.assertEqual(raw['steps'][0]['value'], value)
        self.assertEqual(json.loads(saved)['steps'][0]['value'], value)
        for result in (first, compact):
            self.assertEqual(result['observations'], [dict(field='Memo', value=value, source_step=1)])
            self.assertEqual(result['steps'][0]['value_evidence'], evidence)
            self.assertEqual(result['steps'][0]['source_step'], 1)
            self.assertNotIn('value', result['steps'][0])
        self.assertEqual(len(json.dumps(raw)) - len(json.dumps(compact)), len(value) + len(', "value": ""'))
        self.assertFalse(compact['this_call_dispatched_input'])
        self.assertEqual(path.read_bytes(), saved)
        self.worker.assert_called_once()

    def test_host_summary_preserves_ambiguous_or_interrupted_value_evidence(self):
        observed = dict(status='observed', value='previous', value_evidence=dict(source='provider'))
        previous = dict(number=1, source_step=1, receipt=observed)
        other = dict(number=2, source_step=2, action='read-value', **observed)
        report = dict(status='completed_unverified', observations=[dict(source_step=1, value='previous')],
                      steps=[previous, other])
        result = first_trial.host_summary(report)
        self.assertNotIn('value', result['steps'][0]['receipt'])
        self.assertEqual(result['steps'][0]['receipt']['value_evidence'], observed['value_evidence'])
        self.assertEqual(result['steps'][1], other)
        self.assertEqual(report['steps'][0]['receipt']['value'], 'previous')
        for status in ('blocked', 'uncertain', 'needs_grounding'):
            interrupted = dict(report, status=status, stopped_at=2, host_action='check_current_effect_before_continuing')
            self.assertEqual(first_trial.host_summary(interrupted), interrupted)

    def test_failed_header_can_be_reinspected_without_requerying_or_capturing(self):
        import grounding
        import python_workflow as flow
        tasks.save_task(self.task, self.directory)
        recipe = dict(self.task, steps=[
            dict(action='invoke', selector=dict(role='button', name='VIEW')),
            dict(action='double-click-result', result_row='header', selector=dict(role='group', automation_id='Grid'),
                 result_column='Code', result_point=dict(x=10, y=4, width=50, height=25))])
        plan = dict(envelope=dict(recipe=recipe, source_steps=[1, 2]))
        owner = dict(hwnd=7, pid=19, exe_name=recipe['exe'], title=recipe['window'], process_started_utc_ticks=11)
        self.worker.return_value = dict(status='blocked', input_dispatched=True, stopped_at=2,
            diagnostic='result_header_point_mismatch', inspection_owner=owner,
            steps=[dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True)),
                   dict(number=2, receipt=dict(status='blocked', input_dispatched=False))])
        args = ['--task', self.task['name'], '--allow-physical']
        with (patch.object(first_trial, '_compiled', return_value=plan),
              patch.object(flow.input_policy, 'interactive_desktop_blocked', return_value=None),
              patch.object(flow, 'run', return_value=dict(status='observed', input_dispatched=False)) as observe,
              patch.object(grounding, '_capture', side_effect=AssertionError('image used'))):
            report = self.trial(*args)
            self.assertEqual(report['host_action'], 'inspect_current_state_without_replaying')
            self.assertEqual(self.trial(*args)['this_call_dispatched_input'], False)
            state = first_trial.inspect_result(app, report['trial_receipt'])
            self.assertFalse(state['input_dispatched'])
            self.assertFalse(state['prior_steps_replayed'])
            self.assertEqual(observe.call_args.kwargs, dict(read_only=True, only='2', hwnd=7, result_owner=owner))
            self.worker.assert_called_once()

    def test_wrong_parameter_name_returns_the_recorded_names_before_any_input(self):
        from contextlib import redirect_stdout
        from io import StringIO
        tasks.save_task(self.task, self.directory)
        args = app.parser().parse_args(['try-recording', '--task', self.task['name'],
                                        '--set', 'Zone=sample-user-value'])
        with redirect_stdout(StringIO()) as output, patch.object(app, 'worker_receipt') as worker:
            self.assertEqual(app.worker(args), 2)
        result = json.loads(output.getvalue())
        self.assertEqual((result['diagnostic'], result['required_parameters'], result['input_dispatched']),
                         ('unknown_parameter', ['Region'], False))
        self.assertNotIn('sample-user-value', output.getvalue())
        worker.assert_not_called()

    def test_one_call_offline_draft_then_single_dispatch_reuses_previous_result(self):
        review_drafts.ReviewStorage('초안', 'A.exe', '업무', self.directory).save_draft(
            [dict(step=dict(self.raw, id='1', label='Region'), form=dict(alias='',text=self.raw['instruction']['text'],
                  mode=None,argument=''))], 'invalid_step_instruction', 1)
        self.worker.return_value['steps'] = self.worker.return_value['steps'][:1]
        args = ['--task','초안','--set','Region=North']
        report = self.trial(*args)
        self.assertTrue(report['input_dispatched'])
        self.assertEqual(self.worker.call_args.args, (['_plan'],))
        self.assertEqual(self.worker.call_args.kwargs['payload']['overrides'], {'Region':'North'})
        again = self.trial(*args)
        self.assertTrue(again['previous_trial'])
        self.assertFalse(again['this_call_dispatched_input'])
        self.assertEqual(again['observations'], report['observations'])
        self.worker.assert_called_once()
        self.trial(*args,'--trial-id','deliberate-2')
        self.assertEqual(self.worker.call_count,2)

    def test_signed_and_unsigned_hwnd_share_one_trial_receipt(self):
        tasks.save_task(self.task, self.directory)
        hwnd = 0x81234567
        args = ['--task', self.task['name'], '--set', 'Region=North', '--hwnd']
        self.assertTrue(self.trial(*args, str(hwnd - 2**32))['input_dispatched'])
        for alias in (hwnd, 0xffffffff00000000 | hwnd):
            self.assertTrue(self.trial(*args, str(alias))['previous_trial'])
        self.worker.assert_called_once()
        self.assertEqual(self.worker.call_args.kwargs['payload']['hwnd'], hwnd)

    def test_old_signed_trial_receipt_resumes_without_dispatch_after_upgrade(self):
        tasks.save_task(self.task, self.directory)
        name = self.task['name']
        hwnd = 0x81234567
        args = ['--task', name, '--set', 'Region=North', '--hwnd', str(hwnd)]
        self.trial(*args)
        stored = self.directory / 'first-trials'
        receipt = next(stored.glob('*.json'))
        doc = tasks.load_task(name, self.directory, allow_review=True)
        modified = (self.directory / (name + '.json')).stat().st_mtime_ns
        old_key = recovery._fingerprint([name, modified, recovery._fingerprint(doc),
                                         {'region': 'North'}, hwnd - (1 << 32), None])
        receipt.rename(stored / (old_key + '.json'))
        self.assertTrue(self.trial(*args)['previous_trial'])
        self.worker.assert_called_once()

    def test_session_with_existing_failed_observation_returns_same_evidence(self):
        raw = dict(id='manual', action='manual', label='unresolved',
                   target={'kind':'manual'},recognition={'reason':'capture_target_unavailable'})
        review_drafts.ReviewStorage('초안','A.exe','업무',self.directory).save_draft(
            [dict(step=raw, form=dict(alias='',text='나중에 필요한 내용',mode=None,argument=''))],
            'invalid_step_instruction', 1)
        doc=tasks.read_document('초안', self.directory)
        blocked = dict(status='needs_grounding', source_fingerprint=recovery._fingerprint(doc),
                       diagnostic='instruction_requires_structure', step_number=1, input_dispatched=False)
        recorded = dict(status='recorded', task=dict(name='초안'),live_recovery=blocked)
        with patch('recorder_launch.status', return_value=recorded):
            self.assertEqual(self.trial('--session','a'*32),blocked)
        self.worker.assert_not_called()

    def test_summary_reads_only_the_blocker_from_the_session_and_keeps_the_full_receipt(self):
        raw = dict(id='step', action='manual', label='Action',
                   target={'kind':'manual'}, recognition={'reason':'capture_target_unavailable'})
        rows = [dict(step=raw, form=dict(alias='', text='Review intent', mode=None, argument=''))]
        review_drafts.ReviewStorage('초안', 'Demo.exe', 'Main', self.directory).save_draft(rows, 'invalid_step_instruction', 1)
        doc = tasks.read_document('초안', self.directory)
        blocked = dict(status='needs_grounding', diagnostic='instruction_requires_structure', input_dispatched=False,
                       source_task='초안', source_fingerprint=recovery._fingerprint(doc), step_number=1,
                       steps=[dict(number=1, action='manual', text='Review intent', window='Main', scope_index=0)],
                       live_scopes=[dict(targets=[dict(role='button', name='Unrelated') for _ in range(104)])],
                       bindings_template={'steps':[]})
        event = dict(status='recorded', task={'name':'초안'}, live_recovery=blocked)
        with patch('recorder_launch.status', return_value=event):
            short = self.host_receipt('try-recording', '--session', 'a'*32, '--summary')
            full = self.host_receipt('try-recording', '--session', 'a'*32)
        self.worker.assert_not_called()
        self.assertNotIn('Unrelated', json.dumps(short))
        self.assertEqual(short['step_number'], 1)
        self.assertEqual(short['next_action']['kind'], 'review_unresolved_instruction')
        self.assertEqual(len(full['live_scopes'][0]['targets']), 104)

    def test_saved_minimized_handoff_waits_for_the_window_before_retrying_without_activation(self):
        from recording_followup import followup
        retry = ['recover', '--task', 'Example', '--restore-window', '--prepare']
        saved = dict(status='needs_grounding', diagnostic='target_window_minimized', live_scopes=[],
                     input_dispatched=False, retry_args=retry, next_action=dict(kind='continue_preparation', args=retry))
        event = followup(dict(status='recorded', task={'name': 'Example'}, live_recovery=dict(saved), input_dispatched=False))
        self.assertNotIn('--restore-window', json.dumps(event))
        reported = first_trial.host_summary(followup(saved))
        self.assertEqual(reported['host_action'], 'show_recorded_window_then_retry')
        self.assertEqual(reported['next_action']['args'], ['recover', '--task', 'Example', '--prepare'])
        self.assertEqual(reported['next_action']['args'], reported['retry_args'])

    def test_processing_timeout_keeps_the_entire_trial_request(self):
        argv = ['--session','a'*32,'--set','Region=North','--trial-id','explicit-2',
                '--hwnd','42','--auto-repair-frame','--allow-physical']
        with patch('recorder_launch.status', return_value=dict(status='recorder_processing',
                   input_dispatched=False, saved=True, status_args=['recorder', '--session', 'a'*32])) as waiting:
            report = self.trial(*argv)
        self.assertEqual(report['host_action'], 'continue_same_trial')
        self.assertNotIn('status_args', report)
        self.assertEqual(vars(app.parser().parse_args(report['retry_args'])),
                         vars(app.parser().parse_args(['try-recording', *argv])))
        waiting.assert_called_once()
        self.worker.assert_not_called()
        with patch('recorder_launch.status', return_value=dict(status='recorder_processing', saved=True)):
            retry = self.host_receipt('try-recording', *argv, '--summary')['retry_args']
            self.assertEqual(app.parser().parse_args(retry), app.parser().parse_args(['try-recording', *argv, '--summary']))

    def test_missing_value_and_pointer_permission_are_reported_before_one_retry(self):
        self.task['steps'].append(dict(action='double-click-result', result_row='first',
                                       selector=dict(role='table', automation_id='results'), label='Open result'))
        tasks.save_task(self.task, self.directory)
        base = ['--task', self.task['name']]
        consent = self.trial(*base)
        self.assertEqual(consent['status'], 'needs_consent')
        self.assertEqual(consent['missing_parameters'], ['Region'])
        self.assertEqual(consent['required_option'], '--allow-physical')
        self.assertFalse(consent['input_dispatched'])
        self.assertEqual(self.trial(*base, '--background-only')['diagnostic'],
                         'background_requires_desktop_interaction')
        self.worker.assert_not_called()
        self.worker.return_value['steps'].append(dict(number=3,receipt=dict(status='verified',input_dispatched=True)))
        with patch('input_policy._interactive_desktop', return_value=True):
            self.trial(*base, '--set', 'Region=North', consent['required_option'])
        self.worker.assert_called_once()

    def test_coordinate_layout_change_stops_trial_without_offering_an_image_or_resending_the_query(self):
        self.task['steps'].append(dict(action='double-click-result', result_row='coordinate', label='Open point',
            selector=dict(role='group', automation_id='Results'), result_point=dict(x=30, y=20, width=500, height=100)))
        tasks.save_task(self.task, self.directory)
        self.worker.return_value = dict(status='blocked', stopped_at=3, input_dispatched=True,
            steps=[dict(number=3, receipt=dict(status='blocked', diagnostic='recorded_result_surface_resized',
                result_window=dict(hwnd=1), input_dispatched=False))])
        with patch('input_policy._interactive_desktop', return_value=True):
            report = self.trial('--task', self.task['name'], '--set', 'Region=North', '--allow-physical')
        self.assertEqual(report['host_action'], 'check_named_table_and_chosen_position')
        self.assertNotIn('result_inspection', report)
        self.worker.assert_called_once()

    def test_deferred_session_prepares_once_across_consent_value_and_delivery(self):
        name = '새 녹화'
        rows = [dict(step=dict(id=str(i), label='step', action='manual', target={'kind': 'manual'},
                               recognition={'reason': 'capture_target_unavailable'}),
                     form=dict(alias='', text=label, mode=None, argument=''))
                for i, label in enumerate(('Region 입력', '조회', '결과 열기'))]
        review_drafts.ReviewStorage(name, 'A.exe', self.task['window'], self.directory).save_draft(
            rows, 'invalid_step_instruction', 1)
        source = tasks.read_document(name, self.directory)
        digest = recovery._fingerprint(source)
        prepared = dict(self.task, name=f'{name} - {digest[:12]}', steps=[*self.task['steps'],
            dict(action='double-click-result', result_row='first', label='Open result',
                 selector=dict(role='table', automation_id='results'))])
        tasks.save_task(prepared, self.directory)
        prepared_receipt = dict(status='recorded', host_action='use_prepared_task', task={'name': prepared['name']},
            source_fingerprint=digest, source_steps={'1': 1, '2': 2, '3': 3},
            execution_plan=dict(source_fingerprint=first_trial.recorded_plan.fingerprint(prepared)),
            live_scopes=[{'private': 'do-not-store-live-evidence'}])
        self.worker.side_effect = [prepared_receipt, dict(status='uncertain', input_dispatched=None, steps=[])]
        session = ['--session', 'a' * 32]
        with (patch('recorder_launch.status', return_value=dict(status='recorded', task={'name': name})),
              patch('input_policy._interactive_desktop', return_value=True)):
            self.assertEqual(self.trial(*session)['status'], 'needs_consent')
            self.assertEqual(self.trial(*session, '--allow-physical')['missing_parameters'], ['Region'])
            prepared_path = self.directory / (prepared['name'] + '.json')
            original_bytes = prepared_path.read_bytes()
            modified = json.loads(original_bytes)
            modified['steps'][-1]['selector']['automation_id'] = 'different-table'
            prepared_path.write_text(json.dumps(modified), encoding='utf8')
            with self.assertRaisesRegex(tasks.TaskError, 'prepared_recording_changed'):
                self.trial(*session, '--allow-physical', '--set', 'Region=private-test-value')
            prepared_path.write_bytes(original_bytes)
            dispatched = self.trial(*session, '--allow-physical', '--set', 'Region=private-test-value')
            previous = self.trial('--task', name, '--allow-physical', '--set', 'Region=private-test-value')
        self.assertEqual([call.args[0][0] for call in self.worker.call_args_list], ['_worker', '_plan'])
        self.assertEqual(self.worker.call_args_list[0].args[0], ['_worker', 'recover', '--task', name, '--prepare'])
        self.assertEqual(dispatched['effective_task'], prepared['name'])
        self.assertEqual(dispatched['source_steps'], prepared_receipt['source_steps'])
        self.assertEqual(dispatched['trial_receipt'], previous['trial_receipt'])
        self.assertTrue(previous['previous_trial'])
        self.assertIsNone(previous['input_dispatched'])
        handoff, = (self.directory / 'first-trials/preparations').glob('*.json')
        text = handoff.read_text(encoding='utf8')
        self.assertNotIn('private-test-value', text)
        self.assertNotIn('do-not-store-live-evidence', text)

    def test_interrupted_delivery_reserves_trial_and_missing_parameter_never_reserves(self):
        tasks.save_task(self.task, self.directory)
        missing = self.trial('--task','지역 조회')
        self.assertEqual(missing['missing_parameters'], ['Region'])
        self.assertFalse((self.directory/'first-trials').exists())
        self.worker.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.trial('--task','지역 조회','--set','Region=A')
        after = self.trial('--task','지역 조회','--set','Region=A')
        self.assertEqual(after['diagnostic'], 'first_trial_running_or_interrupted')
        self.assertIsNone(after['input_dispatched'])
        self.worker.assert_called_once()

    def test_recorded_scope_never_drops_or_guesses_parent_on_duplicate_ids(self):
        task = self.task['steps'][0]
        parent = dict(role='group',automation_id='MainContent')
        task.update(ancestors=[parent], within='MainContent')
        row = dict(step=task, text=task['instruction']['text'], mode='required', argument='Region',context=None)
        selectors = recovery._recorded_lookups([row])
        self.assertEqual(selectors['selectors'][0]['ancestors'],[parent])
        observed = dict(role='combobox', automation_id='REG',semantic_label='Region',enabled=True,offscreen=False)
        identity = dict(role='combobox',target='REG',match='automation_id',ancestors=[parent])
        inventory=dict(targets=[dict(observed,binding_scopes={'MainContent':identity}), observed])
        suggestion = recovery._suggest_binding(1,row,inventory)
        self.assertEqual(suggestion['within'],'MainContent')
        self.assertEqual(recovery._field(row,suggestion,inventory)['ancestors'],[parent])
        inventory['targets'] = [observed,observed]
        self.assertIsNone(recovery._suggest_binding(1,row,inventory))

    def test_readable_column_without_exact_match_stops_without_image(self):
        tasks.save_task(self.task, self.directory)
        receipt = dict(status='blocked', diagnostic='result_value_not_found', input_dispatched=False,
                       result_window=dict(hwnd=7))
        self.worker.return_value = dict(status='blocked', diagnostic='result_value_not_found', stopped_at=2,
            input_dispatched=True, steps=[dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True)),
                                         dict(number=2, receipt=receipt)])
        recipe = dict(steps=[dict(action='invoke'), dict(action='verify-value', result_column='Region', value='LAB-42')])
        with patch.object(first_trial, '_compiled', return_value=dict(envelope=dict(recipe=recipe, source_steps=[1, 2]))):
            report = self.trial('--task', self.task['name'])
        self.assertEqual(report['host_action'], 'report_unmatched_result')
        self.assertNotIn('result_inspection', report)
        self.worker.assert_called_once()

    def test_opaque_result_offers_bound_read_only_inspection_and_never_repeats_query(self):
        import grounding
        tasks.save_task(self.task, self.directory)
        window = dict(hwnd=719, pid=182, exe_name='A.exe', title='업무 창', process_started_utc_ticks=144)
        self.worker.return_value = dict(status='blocked', diagnostic='result_cell_missing', stopped_at=2,
            input_dispatched=True, steps=[dict(number=1,receipt=dict(status='delivered_unverified',input_dispatched=True)),
            dict(number=2,receipt=dict(status='blocked', input_dispatched=False, result_window=window))])
        recipe = dict(steps=[dict(action='invoke'), dict(action='verify-value', result_column='Region', value='LAB-42')])
        plan = dict(envelope=dict(recipe=recipe, source_steps=[1, 2]))
        with patch.object(first_trial, '_compiled', return_value=plan):
            paused = self.trial('--task',self.task['name'])
        self.assertEqual(paused['result_inspection']['args'], ['inspect-trial','--receipt',paused['trial_receipt']])
        with patch.object(first_trial, '_compiled', side_effect=AssertionError('should use receipt')):
            again = self.trial('--task',self.task['name'])
        self.assertEqual(again['source_stopped_at'],2)
        self.worker.assert_called_once()
        pixels = bytes((31, 61, 91, 0)) * 64
        png = grounding._png(8, 8, pixels)
        with (patch.object(app.bridge, 'Binding') as bind, patch.object(grounding, '_capture',
                return_value=([20,30,8,8], png)) as capture,
              patch.object(app, 'worker_receipt', self.worker),
              patch.object(grounding, '_directory',return_value=self.directory)):
            frame = app.run(app.parser().parse_args(paused['result_inspection']['args']))
            bind.assert_called_once_with(window)
            self.assertEqual(frame['result_match'],'unverified')
            self.assertFalse(frame['input_dispatched'])
            self.assertTrue(Path(frame['image']).is_file())
            identical = app.run(app.parser().parse_args(paused['result_inspection']['args']))
            self.assertEqual((identical['frame'], identical['image']), (frame['frame'], frame['image']))
            self.assertEqual(Path(frame['image']).read_bytes(), png)
            changed_pixels = bytearray(pixels)
            changed_pixels[0] ^= 255
            changed_png = grounding._png(8, 8, changed_pixels)
            capture.return_value = ([20,30,8,8], changed_png)
            changed = app.run(app.parser().parse_args(paused['result_inspection']['args']))
            self.assertNotEqual(changed['frame'], frame['frame'])
            self.assertNotEqual(changed['image'], frame['image'])
            self.assertEqual(Path(changed['image']).read_bytes(), changed_png)
            self.assertEqual(Path(frame['image']).read_bytes(), png)
            for inspected in (frame, identical, changed):
                self.assertFalse(inspected['input_dispatched'])
                self.assertFalse(inspected['prior_steps_replayed'])
                self.assertEqual(inspected['trial_receipt'], paused['trial_receipt'])
            self.assertEqual(len(list(self.directory.glob('*.png'))), 2)
            self.worker.assert_called_once()  # Only the initial query trial, never the inspections.
            with patch.object(first_trial.time,'time',return_value=time.time()+grounding.TTL+1):
                with self.assertRaisesRegex(tasks.TaskError, 'result_inspection_expired'):
                    app.run(app.parser().parse_args(paused['result_inspection']['args']))
            self.assertEqual(capture.call_count, 3)  # Freshness checked even when the token is reused.

    def test_dependent_unknown_step_does_not_observe_prefix_or_register(self):
        task = dict(self.task, steps=[self.task['steps'][1], dict(action='manual',
            instruction_draft='필요한 내용 처리',reason='capture_target_unavailable',
            window_context=dict(exe='A.exe', title='Details', after_step=1, related=True))])
        tasks.save_task(task,self.directory)
        def closed(app, doc, owner, rows, seq, number):
            self.assertEqual(number,2)
            raise tasks.TaskError('recorded_window_not_open')
        import live_recovery
        with (patch.object(app, 'windows', return_value=[dict(hwnd=123,title='업무 창')]),
              patch.object(app.bridge,'Binding'), patch.object(app.visibility,'require_observable'),
              patch.object(recovery,'_bind_surface',side_effect=closed) as route,
              patch.object(live_recovery,'observe_scope') as observe):
            report = app.run(app.parser().parse_args(['recover','--task',task['name'],'--prepare']))
        self.assertEqual(report['host_action'],'inspect_recorded_step_when_popup_opens')
        self.assertEqual(report['inspect_args'][-2:],['--only','2'])
        self.assertEqual(report['opener_step'],1)
        route.assert_called_once()
        observe.assert_not_called()

    def test_confirmed_one_shot_executes_only_suffix_and_original_returns_final_observations(self):
        import grounding
        tasks.save_task(self.task, self.directory)
        scope = dict(exe='A.exe', title='Details', after_step=3, related=True)
        recipe = dict(schema=1,name='lookup',exe='A.exe',window='업무 창',steps=[
            dict(action='invoke', selector=dict(role='button',automation_id='search')),
            dict(action='verify-value',result_column='Region',value='North',selector=dict(role='table')),
            dict(action='double-click-result',label='double'),
            dict(action='read-value',label='Note',window_context=scope,selector=dict(role='textfield',automation_id='note')),
            dict(action='invoke',verify_closed=True,label='Exit',window_context=scope,selector=dict(role='button',automation_id='exit'))])
        identity = dict(hwnd=7,pid=19,exe_name='A.exe',title='업무 창',process_started_utc_ticks=111)
        self.worker.return_value = dict(status='blocked',stopped_at=2,input_dispatched=True,
            steps=[dict(number=1,receipt=dict(status='delivered_unverified',input_dispatched=True)),
                   dict(number=2,receipt=dict(status='blocked',diagnostic='result_cell_unreadable',
                                              input_dispatched=False,result_window=identity))])
        with (patch.object(first_trial, '_compiled', return_value=dict(envelope=dict(recipe=recipe,source_steps=[1,2,3,4,5]))),
              patch.object(grounding,'_directory',return_value=self.directory),patch.object(app,'worker_receipt',self.worker)):
            original = self.trial('--task', self.task['name'], '--allow-physical')
            key = original['trial_receipt']
            paused = self.host_receipt('try-recording', '--task', self.task['name'], '--summary')
            self.assertEqual(paused['steps'], original['steps'])
            inspect = paused['result_inspection']['args']
            self.assertEqual(inspect, ['inspect-trial', '--receipt', key, '--summary'])
            pixels = random.Random(243).randbytes(125*125*4)
            image = grounding._png(125, 125, pixels)
            with (patch.object(grounding, '_capture', return_value=([0, 0, 125, 125], image)),
                  patch.object(app.bridge, 'Binding')):
                capture = self.host_receipt(*inspect)
            token = capture['frame']
            def resume(value, token=token, point='24'):
                args = ['resume-trial','--receipt',key,'--frame',token,'--column','Region',
                        '--value',value,'--point',point,'24','--allow-physical']
                return app.run(app.parser().parse_args(args))
            with self.assertRaisesRegex(tasks.TaskError,'confirmed_result_value_mismatch'):
                resume('South')
            with self.assertRaisesRegex(tasks.TaskError,'grounding_frame_expired'):
                resume('North','b'*64)
            self.worker.assert_called_once()
            self.worker.return_value = dict(status='blocked',stopped_at=1,input_dispatched=False,
                steps=[dict(number=1,receipt=dict(status='blocked',diagnostic='result_point_outside_table',input_dispatched=False))])
            rejected = resume('North',point='52')
            self.assertFalse(rejected['resume_dispatched_input'])
            self.assertFalse(resume('North',point='52')['this_call_dispatched_input'])
            self.assertEqual(self.worker.call_count,2)
            self.worker.return_value = dict(status='completed_unverified',input_dispatched=True,steps=[
                dict(number=1,receipt=dict(status='verified',input_dispatched=True)),
                dict(number=2,receipt=dict(status='observed',value='memo value',input_dispatched=False)),
                dict(number=3,receipt=dict(status='verified',input_dispatched=True))])
            replacements = {'<observed exact cell text>': 'North', '<cell text x>': '24', '<cell text y>': '24'}
            summarized = self.host_receipt(*(replacements.get(arg, arg) for arg in capture['resume_args']))
            result = resume('North')
            self.assertEqual(result['observations'],[dict(source_step=4,field='Note',value='memo value')])
            self.assertEqual(summarized['observations'], result['observations'])
            self.assertEqual(summarized['steps'][:2], result['steps'][:2])
            self.assertNotIn('value', summarized['steps'][3])
            self.assertFalse(summarized['prior_steps_replayed'])
            payload = self.worker.call_args.kwargs['payload']
            self.assertEqual([s['action'] for s in payload['recipe']['steps']],['visual-click','read-value','invoke'])
            self.assertEqual(payload['result_confirmation']['owner'],identity)
            self.assertEqual(payload['result_confirmation']['selector'],{'role':'table'})
            self.assertEqual(payload['recipe']['steps'][1]['window_context']['after_step'],1)
            again = self.trial('--task', self.task['name'])
            self.assertEqual(again['steps'],result['steps'])
            self.assertTrue(again['previous_trial'])
            self.assertFalse(again['this_call_dispatched_input'])
            self.assertEqual(resume('North')['status'],'completed_unverified')
            self.assertEqual(self.worker.call_count,3)

    def test_mixed_legacy_defaults_and_field_alias_reach_result_check_without_legacy_replay(self):
        from copy import deepcopy
        field = dict(action='fill',match='automation_id',target='code',role='textfield',label='Region',
                     parameter='Region',value='South',alias='Code',recorded_effect='matched')
        check = dict(action='verify-value',selector=dict(role='table'),result_column='Region',
                     parameter='Region',label='Region',description='Region 일치 확인')
        task = dict(self.task, steps=[field, deepcopy(self.task['steps'][1]), check])
        tasks.save_task(task,self.directory)
        argv = ['replay','--task',task['name'],'--exe',task['exe'],'--window',task['window']]
        with patch.object(app,'worker_receipt',return_value=dict(status='verified',steps=[])) as runner:
            result = app.run(app.parser().parse_args(argv))
            self.assertEqual(result['execution_engine'],'saved_python_plan')
            steps = runner.call_args.kwargs['payload']['recipe']['steps']
            self.assertEqual((steps[0]['value'],steps[2]['value']),('South','South'))
            app.run(app.parser().parse_args([*argv,'--set','Code=North']))
            payload = runner.call_args.kwargs['payload']
            self.assertEqual(payload['overrides'],{'Region':'North'})
            self.assertEqual(payload['recipe']['steps'][2]['parameter'],'Region')

    def test_replay_key_with_older_recorded_value_and_key_only_scope(self):
        field = dict(action='fill', match='automation_id', target='code', role='textfield',
                     label='Region', parameter='Region', value='South', recorded_effect='matched')
        key = dict(action='press-key', selector=dict(automation_id='code', role='textfield'),
                   label='Region', keys='Tab')
        task = dict(self.task, steps=[field, key])
        tasks.save_task(task, self.directory)
        argv = ['replay', '--task', task['name'], '--exe', task['exe'], '--window', task['window']]
        with patch.object(app, 'worker_receipt', return_value=dict(status='verified',steps=[])) as runner:
            app.run(app.parser().parse_args(argv))
            payload = runner.call_args.kwargs['payload']
            self.assertEqual([(s['action'], s.get('value'), s.get('keys')) for s in payload['recipe']['steps']],
                             [('set-value', 'South', None), ('press-key', None, 'Tab')])
            app.run(app.parser().parse_args([*argv, '--only', '2']))
            self.assertEqual(runner.call_args.kwargs['payload']['only'], '2')

    def test_enter_only_uses_the_same_keyboard_executor_as_a_full_trial(self):
        submit = dict(action='submit', selector=dict(automation_id='code', role='textfield'), label='Region')
        task = dict(self.task, steps=[submit])
        tasks.save_task(task, self.directory)
        args = app.parser().parse_args(['replay', '--task', task['name'], '--exe', task['exe'],
                                        '--window', task['window'], '--only', '1'])
        with patch.object(app, 'worker_receipt', return_value=dict(status='completed_unverified', steps=[])) as worker:
            result = app.run(args)
        self.assertEqual(result['execution_engine'], 'saved_python_plan')
        payload = worker.call_args.kwargs['payload']
        self.assertEqual((payload['only'], payload['recipe']['steps'][0]['action']), ('1', 'submit'))
