"""Compile a host's interpretation for an existing recording without changing the recorded evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

from test_draft_click_workflow import source, visual
from test_learned_tasks import demo, task
from test_linked_window_replay import window, binding
from test_ordinary_row_capture import sample
import compound_plan
import desktop_select as app
import learned_tasks
import recorded_plan
import row_capture
from window_context import WindowRoutes


class InterpretedRecording(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / '.desktop-select/tasks'
        self.original = source()
        for step in self.original['steps'][2:]:
            step['instruction_draft'] = '잠시 기다린 뒤 안내가 보이면 닫고 앱을 열어줘.'
        self.original['steps'][2]['visual_hint'] = visual()
        self.path = learned_tasks.save_task(self.original, self.store)
        self.before = self.path.read_bytes()
        self.host = dict(schema=1, steps=[
            dict(number=n, text='10초 뒤 안내가 있으면 닫고 항상 Gallery를 열어줘.', seconds=10,
                 when_present=dict(name='안내'), dismiss=dict(name='닫기'), always=dict(name='Gallery'))
            for n in (3, 4)])

    def install_donor(self):
        task = deepcopy(self.original)
        task['name'] = '기존 작업'
        before = [('a','stopped','start'), ('b','stopped','start')]
        for i in (0, 1):
            after = before.copy()
            after[i] = (before[i][0], 'running','stop')
            press = row_capture.prepare(sample(before, 1), [157,205+15*i], 1.2)
            task['steps'][i]['row_hint'] = row_capture.infer(press, sample(after,2))
            step = task['steps'][i + 2]
            task['steps'][i + 2] = dict(action='visual-click', label='Gallery 열어줘', visual=visual(),
                                        window_context=step['window_context'])
        dest = self.root / '.claude/skills/gallery'
        dest.mkdir(parents=True)
        (dest / 'source-task.json').write_text(json.dumps(task), encoding='utf8')
        (dest / 'workflow.json').write_text(json.dumps(recorded_plan.compile_plan(task)['envelope']['recipe']), encoding='utf8')
        return task, dest

    def test_existing_original_name_picks_up_conditional_steps_for_replay_and_preview(self):
        self.assertEqual(recorded_plan.compile_saved(self.original, self.store)['diagnostic'], 'instruction_requires_structure')
        instructions = self.root/'host.json'
        instructions.write_text(json.dumps(self.host), encoding='utf8')
        with (mock.patch.object(app, 'task_directory', return_value=self.store),
              mock.patch.object(app.bridge, 'set_dpi_awareness'),
              mock.patch.object(app, 'windows') as windows,
              mock.patch.object(app, 'worker_receipt', return_value=dict(status='verified', steps=[])) as dispatch):
            result = app.run(app.parser().parse_args(
                ['interpret','--task',self.original['name'],'--plan-file',str(instructions)]))
            self.assertEqual(result['status'], 'planned')
            self.assertEqual([s['number'] for s in result['execution_plan']['interpreted_steps']], [3,4])
            self.assertEqual(self.path.read_bytes(), self.before)
            windows.assert_not_called()
            dispatch.assert_not_called()
            cli = ['replay','--task',self.original['name'],'--exe',self.original['exe'],'--window',self.original['window']]
            preview = app.run(app.parser().parse_args(cli + ['--input-plan']))
            self.assertEqual([s['action'] for s in preview['input_requirements']['steps']][-2:], ['conditional-open']*2)
            dispatch.assert_not_called()
            app.run(app.parser().parse_args(cli))
        sent = dispatch.call_args.kwargs['payload']['recipe']['steps']
        self.assertEqual([s['window_context']['after_step'] for s in sent[2:]], [1,2])
        self.assertEqual([s['seconds'] for s in sent[2:]], [10,10])
        self.assertEqual(sent[2]['always'], {'name':'Gallery'})
        self.assertNotIn('visual', sent[2]['dismiss'])
        loaded = recorded_plan.load_plan(self.original, self.store)
        self.assertEqual(loaded['envelope']['source'], self.original)
        self.assertEqual(loaded['envelope']['source_steps'], [1,2,3,4])

    def test_reuse_only_the_registered_workflow_target_with_matching_window_and_ordinal(self):
        donor, skill = self.install_donor()
        for item in self.host['steps']:
            item['always'].update(from_skill='gallery', from_step=item['number'])
        envelope = compound_plan.save(self.original, self.host, self.store)['envelope']
        for i, step in enumerate(envelope['recipe']['steps'][2:]):
            self.assertEqual(step['always']['visual'], donor['steps'][i+2]['visual'])
            self.assertEqual(envelope['recipe']['steps'][i]['row'], donor['steps'][i]['row_hint'])
        self.assertEqual(self.path.read_bytes(), self.before)
        self.host['steps'][0]['always']['from_step'] = 4
        with self.assertRaisesRegex(learned_tasks.TaskError, 'scope_mismatch'):
            compound_plan.save(self.original,self.host,self.store)
        changed = json.loads((skill/'workflow.json').read_bytes())
        changed['steps'][2]['label'] = 'Different button'
        (skill/'workflow.json').write_text(json.dumps(changed))
        self.assertEqual(recorded_plan.get_plan(self.original,self.store)['diagnostic'],
                         'conditional_reference_workflow_mismatch')

    def test_no_hint_when_a_different_ordinal_opened_the_same_window(self):
        donor, _ = self.install_donor()
        donor['steps'][0].update(ordinal=7, label='7번째 행 실행')
        learned_tasks.save_task(donor,self.store)
        self.host['steps'][0]['always'].update(from_task=donor['name'], from_step=3)
        result = compound_plan.prepare(self.original,self.host,self.store)
        self.assertNotIn(1,result)
        self.assertIn('visual',result[3]['always'])

    def test_warm_pair_rebinds_two_existing_windows_by_their_recorded_rows(self):
        self.install_donor()
        for item in self.host['steps']:
            item['always'].update(from_skill='gallery', from_step=item['number'])
        recipe = compound_plan.save(self.original,self.host,self.store)['envelope']['recipe']
        records = [window(41,'A'),window(63,'B')]
        owner = SimpleNamespace(hwnd=7,exe=recipe['exe'],pid=15,created='1')
        with (mock.patch.object(app,'guard'), mock.patch.object(app,'windows',return_value=records),
              mock.patch.object(app.bridge,'Binding',side_effect=binding)):
            routes = WindowRoutes(recipe['steps'],owner,recipe['window'])
            for n, opener in enumerate(recipe['steps'][:2],1):
                self.assertEqual(opener['action'], 'row-action')
                routes.before(n)
                routes.after(n,dict(status='verified',input_dispatched=False,observed_state='after',
                                    initial_state='after',verification_scope='recorded_row_state'))
            matches = [routes.resolve(s['window_context']).hwnd for s in recipe['steps'][2:]]
            self.assertEqual(matches,[41,63])

    def test_named_child_never_launches_the_current_ordinal_or_uses_its_cache(self):
        import python_workflow as flow
        self.install_donor()
        self.host['steps'][1]['always'].update(from_skill='gallery', from_step=4)
        recipe = compound_plan.save(self.original,self.host,self.store)['envelope']['recipe']
        opener = recipe['steps'][1]
        with (mock.patch.object(flow,'check_surface'), mock.patch.object(flow.row_intent,'execute') as ordinal,
              mock.patch.object(flow.row_vision,'observe_row',return_value=dict(kind='missing'))):
            result = flow.row_receipt(object(),'Manager',opener,read_only=False)
        self.assertEqual((result['status'], result['input_dispatched']), ('blocked', False))
        ordinal.assert_not_called()

    def test_recordings_own_opener_template_pins_the_row_without_a_donor(self):
        donor, _ = self.install_donor()
        step = self.original['steps'][1]
        step['row_hint'] = donor['steps'][1]['row_hint']
        pinned = compound_plan.prepare(self.original,self.host,self.store)[2]
        self.assertEqual(pinned,dict(label=step['label'],action='row-action',row=step['row_hint']))

    def test_unidentified_ordinal_opener_requests_its_window_before_any_dispatch(self):
        import python_workflow as flow
        recipe = compound_plan.save(self.original,self.host,self.store)['envelope']['recipe']
        with mock.patch.object(flow,'bind') as bind, mock.patch('scene_session.start') as start:
            result = flow.run(recipe)
        self.assertEqual(result['status'],'needs_grounding')
        self.assertFalse(result['input_dispatched'])
        self.assertEqual([(s['number'],s['expected_window']) for s in result['grounding_steps']],
                         [(1,'A'),(2,'B')])
        bind.assert_not_called()
        start.assert_not_called()

    def test_host_can_bind_unique_named_opener_from_manager_frame_in_a_copy(self):
        import grounding
        import random
        args = SimpleNamespace(command='ground',task=self.original['name'],only='2',
                               save_as='grounded',point=(52,47))
        owner = SimpleNamespace(hwnd=5,pid=42,created='100')
        frame = grounding._png(120,95,random.Random(5).randbytes(120*95*4))
        with (mock.patch.object(app,'task_directory',return_value=self.store),
              mock.patch.object(grounding,'_directory',return_value=self.store),
              mock.patch.object(app,'windows'), mock.patch.object(app,'pick_window'),
              mock.patch.object(app.bridge,'Binding',return_value=owner), mock.patch.object(app,'guard'),
              mock.patch.object(grounding,'WindowRoutes'), mock.patch.object(grounding.visual_target,'observe'),
              mock.patch.object(grounding,'_capture',return_value=([0,0,120,95],frame))):
            seen = grounding.run(app,args)
            self.assertEqual(seen['expected_windows'], [self.original['steps'][3]['window_context']])
            args.command, args.frame = 'bind-visual', seen['frame']
            self.assertFalse(grounding.run(app,args)['input_dispatched'])
        self.assertEqual(self.path.read_bytes(),self.before)
        rebound = learned_tasks.load_task(args.save_as,self.store)
        self.assertEqual(rebound['steps'][1]['action'],'visual-click')
        self.assertIn('envelope',compound_plan.save(rebound,self.host,self.store))

    def test_donor_cannot_replace_its_registered_opener_template(self):
        donor, skill = self.install_donor()
        donor['steps'][1]['row_hint'] = donor['steps'][0]['row_hint']
        (skill/'source-task.json').write_text(json.dumps(donor), encoding='utf8')
        self.host['steps'][1]['always'].update(from_skill='gallery', from_step=4)
        with self.assertRaisesRegex(learned_tasks.TaskError,'conditional_reference_workflow_mismatch'):
            compound_plan.save(self.original,self.host,self.store)

    def test_conditional_prose_on_existing_visual_stays_a_draft_with_original_hint(self):
        raw=dict(action='visual-click',label='녹화한 화면 항목 누르기',visual=visual())
        sentence='10초 기다린 다음 광고가 보이면 닫고 앱을 열어줘.'
        directive=learned_tasks.compile_instruction(raw,sentence)
        self.assertEqual(directive,{'mode':'draft','text':sentence})
        edited=learned_tasks.recorded_step(dict(raw,instruction=directive))
        self.assertEqual(edited['instruction_draft'],sentence)
        self.assertEqual(edited['visual_hint'],raw['visual'])

    def test_conditional_on_recognized_combo_plays_with_legacy_default_or_alias(self):
        original = task(demo('Category','Basic'), demo('Choice','Previous'))
        original['steps'][0]['alias'] = '제품'
        original['steps'][1]['instruction_draft'] = self.original['steps'][2]['instruction_draft']
        learned_tasks.save_task(original,self.store)
        host = dict(schema=1,steps=[dict(self.host['steps'][0],number=2)])
        self.assertIn('envelope', compound_plan.save(original,host,self.store))
        cmd=['replay','--task',original['name'],'--exe',original['exe'],'--window',original['window']]
        with (mock.patch.object(app,'task_directory',return_value=self.store),
              mock.patch.object(app.bridge,'set_dpi_awareness'),
              mock.patch.object(app,'worker_receipt',return_value={'status':'verified'}) as dispatch):
            for args, expected in (([], 'Basic'), (['--set','제품=Hybrid'], 'Hybrid')):
                self.assertEqual(app.run(app.parser().parse_args(cmd+args))['status'],'verified')
                payload=dispatch.call_args.kwargs['payload']
                self.assertEqual(payload['recipe']['steps'][0]['value'],expected)
                self.assertEqual(payload['recipe']['steps'][1]['action'],'conditional-open')

    def test_named_click_in_other_child_is_independent_of_following_conditional(self):
        original=deepcopy(self.original)
        original['steps'][2]=dict(task(demo('Open','','button','click'))['steps'][0],
                                  window_context=self.original['steps'][2]['window_context'])
        replacements=compound_plan.prepare(original,dict(schema=1,steps=[self.host['steps'][1]]),self.store)
        result=recorded_plan.compile_plan(original,replacements=replacements)
        self.assertEqual([x['action'] for x in result['envelope']['recipe']['steps'][-2:]],
                         ['invoke','conditional-open'])

    def test_duplicate_partial_wrong_source_and_modified_saved_interpretation(self):
        partial=deepcopy(self.host)
        partial['steps'].pop()
        self.assertEqual(compound_plan.save(self.original,partial,self.store)['diagnostic'],
                         'instruction_requires_structure')
        path=compound_plan.path_for(self.original,self.store)
        self.assertFalse(path.exists())
        bad=deepcopy(self.host)
        bad['steps'][1]['number']=3
        with self.assertRaisesRegex(learned_tasks.TaskError,'duplicate_interpreted_step'):
            compound_plan.save(self.original,bad,self.store)
        bad=deepcopy(self.host)
        bad['steps'][0]['number']=1
        with self.assertRaisesRegex(learned_tasks.TaskError,'recorded_instruction_required'):
            compound_plan.save(self.original,bad,self.store)
        compound_plan.save(self.original,self.host,self.store)
        changed=deepcopy(self.original)
        changed['steps'][2]['instruction_draft']='changed text'
        self.assertEqual(recorded_plan.compile_saved(changed,self.store)['diagnostic'],'interpretation_source_changed')
        stored=json.loads(path.read_bytes())
        stored['instructions']['steps'][1]['seconds']=50
        path.write_text(json.dumps(stored))
        self.assertEqual(recorded_plan.compile_saved(self.original,self.store)['diagnostic'],'interpretation_targets_changed')
        self.assertEqual(self.path.read_bytes(),self.before)
