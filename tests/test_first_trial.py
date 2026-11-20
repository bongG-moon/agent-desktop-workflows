"""Test the public first-trial command with real recording storage and a counted input boundary."""
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
        with (patch.object(app.bridge, 'Binding') as bind, patch.object(grounding, '_capture',
                return_value=([20,30,100,120], b'frame')) as capture,
              patch.object(grounding, '_directory',return_value=self.directory)):
            frame = app.run(app.parser().parse_args(paused['result_inspection']['args']))
            bind.assert_called_once_with(window)
            self.assertEqual(frame['result_match'],'unverified')
            self.assertFalse(frame['input_dispatched'])
            self.assertTrue(Path(frame['image']).is_file())
            with patch.object(first_trial.time,'time',return_value=time.time()+grounding.TTL+1):
                with self.assertRaisesRegex(tasks.TaskError, 'result_inspection_expired'):
                    app.run(app.parser().parse_args(paused['result_inspection']['args']))
            capture.assert_called_once()

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
                   dict(number=2,receipt=dict(status='blocked',input_dispatched=False,result_window=identity))])
        with (patch.object(first_trial, '_compiled', return_value=dict(envelope=dict(recipe=recipe,source_steps=[1,2,3,4,5]))),
              patch.object(grounding,'_directory',return_value=self.directory),patch.object(app,'worker_receipt',self.worker)):
            original = self.trial('--task', self.task['name'], '--allow-physical')
            key = original['trial_receipt']
            pixels = random.Random(243).randbytes(125*125*4)
            token,_ = grounding.store_frame(self.directory,key.encode('ascii'),grounding._png(125,125,pixels))
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
            result = resume('North')
            self.assertEqual(result['observations'],[dict(source_step=4,field='Note',value='memo value')])
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
