"""Run old click descriptions and new demonstrated canvas targets through the normal compiler."""
import base64
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import desktop_select
import learned_tasks as tasks
import python_workflow as flow
import recorded_plan
import visual_target
import row_capture
from test_linked_window_replay import binding, window
from test_ordinary_row_capture import sample


SENTENCE = '그림 게임 버튼 눌러서 시행 (gallery)'


def visual():
    side = visual_target.SIDE
    pixels = bytes((x+y)%2*240 for y in range(side) for x in range(side) for _ in range(3))
    return dict(size=[side, side], offset=[side//2]*2, rgb=base64.b64encode(pixels).decode('ascii'))


def source():
    rows = [dict(action='row-launch', label=f'{n}번째 행 실행', ordinal=n) for n in (1,2)]
    actions = [dict(action='manual', reason='unsupported_recorded_action', instruction_draft=SENTENCE,
                    window_context=dict(exe='Viewer.exe',title=name, after_step=i))
               for i,name in enumerate(('A','B'),1)]
    return dict(schema=1, name='Two viewers', exe='Manager.exe', window='Manager', steps=rows+actions)


class DraftClickTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.primary=SimpleNamespace(hwnd=1,pid=101,exe='Manager.exe',created='100')
        self.rows = []
        self.patch(flow, 'bind', return_value=self.primary)
        self.patch(flow.app, 'windows', side_effect=lambda _:list(self.rows))
        self.patch(flow.app.bridge, 'Binding', side_effect=binding)
        self.patch(flow.app, 'guard')
        self.patch(flow.app.visibility, 'restore_for_action')
        uia = self.patch(flow.app.bridge, 'UIA').return_value.__enter__.return_value
        uia.pid.return_value = self.primary.pid

    def patch(self,module,name,**args):
        return self.stack.enter_context(mock.patch.object(module,name,**args))

    def test_exact_old_sentence_builds_two_distinct_queries_without_editing_recording(self):
        plan=source()
        original=deepcopy(plan)
        result=recorded_plan.compile_plan(plan)
        self.assertEqual(result['status'], 'compiled')
        recipe=result['envelope']['recipe']
        self.assertEqual([s['selector']['query'] for s in recipe['steps'][2:]], ['gallery']*2)
        self.assertEqual([s['grounding_text'] for s in recipe['steps'][2:]], [SENTENCE]*2)
        self.assertEqual([s['window_context']['after_step'] for s in recipe['steps'][2:]], [1,2])
        self.assertEqual(plan, original)
        self.assertEqual(result['envelope']['source'], original)

    def test_scoped_icon_caption_drops_location_and_open_effect_from_semantic_query(self):
        for location in ('04 기기 홈 화면', '05 기기 창'):
            sentence = location + '에서 그림 카드 게임 아이콘을 눌러 열어줘.'
            self.assertEqual(tasks.click_request(sentence), ('그림 카드 게임', 'button'))
            self.assertIsNone(tasks.click_request('다른 앱을 누르고 ' + sentence))

    def test_already_open_canvas_missing_both_names_yields_scoped_handoff_before_any_launch(self):
        self.rows=[window(2,'A'),window(3,'B')]
        recipe=recorded_plan.compile_plan(source())['envelope']['recipe']
        self.patch(flow, 'resolve_step', side_effect=desktop_select.SelectionError('target_missing'))
        dispatch=self.patch(flow, 'row_receipt')
        result=flow.run(recipe)
        self.assertEqual(result['status'], 'needs_grounding')
        self.assertFalse(result['input_dispatched'])
        self.assertEqual([(s['number'], s['window_context']['title'], s['instruction'], s['query'])
                          for s in result['grounding_steps']], [(3,'A',SENTENCE,'gallery'),(4,'B',SENTENCE,'gallery')])
        dispatch.assert_not_called()

    def test_visual_clicks_compile_edited_descriptions_then_dispatch_once_per_new_viewer(self):
        plan=source()
        for n, old in enumerate(plan['steps'][2:],2):
            raw=dict(action='visual-click', visual=visual(), label='녹화한 화면 항목 누르기',
                     window_context=old['window_context'])
            raw['instruction']=tasks.compile_instruction(raw, SENTENCE)
            plan['steps'][n]=tasks.recorded_step(raw)
        recipe=recorded_plan.compile_plan(plan)['envelope']['recipe']
        self.assertEqual([s['label'] for s in recipe['steps'][2:]], [SENTENCE]*2)
        self.assertNotIn('rgb', str(tasks.summary(plan)))
        def launch(bound, title, step, **kwargs):
            idx=step['ordinal']
            self.rows.append(window(idx+1, 'AB'[idx-1]))
            return dict(status='verified', input_dispatched=True, observed_state='after')
        self.patch(flow, 'row_receipt', side_effect=launch)
        sent=[]
        def click(bound, spec, guard, **kwargs):
            guard()
            self.assertEqual(kwargs['first_match_timeout'], flow.CHILD_VISUAL_TIMEOUT)
            sent.append(bound.hwnd)
            return dict(status='delivered_unverified', diagnostic='visual_click_dispatched',
                        input_dispatched=True, physical_input=True)
        self.patch(visual_target, 'execute', side_effect=click)
        self.assertFalse(flow.run(recipe, background_only=True)['input_dispatched'])
        result=flow.run(recipe)
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual(sent, [2,3])
        self.assertTrue(result['physical_input'])
        unscoped = dict(recipe, steps=[{k:v for k,v in s.items() if k != 'window_context'} for s in recipe['steps']])
        self.assertEqual(flow.worker_timeout(recipe) - flow.worker_timeout(unscoped),
                         2 * (flow.window_context.OPEN_TIMEOUT + flow.CHILD_VISUAL_TIMEOUT))

    def test_portable_project_registers_from_evidence_with_no_running_app_and_no_cwd_dependency(self):
        plan = source()
        before = [('a','stopped','start'),('b','stopped','start')]
        captions = ['첫 기기 열기', '둘째 기기 열기', '첫 창에서 그림 열기', '둘째 창에서 그림 열기']
        for i in range(2):
            press = row_capture.prepare(sample(before, 1), [157,205+15*i], 1.2)
            after = before.copy()
            after[i] = (before[i][0], 'running','stop')
            plan['steps'][i] = dict(action='row-action', row=row_capture.infer(press, sample(after, 2)),
                                    label='녹화한 행 실행', description=captions[i])
            plan['steps'][i+2] = dict(action='visual-click', label=SENTENCE, description=captions[i+2],
                                      visual=visual(), window_context=plan['steps'][i+2]['window_context'])
        original = deepcopy(plan)
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            engine = project / 'desktop-select'
            shutil.copytree(Path(flow.__file__).resolve().parents[1], engine, ignore=shutil.ignore_patterns('__pycache__', '.desktop-select'))
            from runtime_integrity import checked_path
            for filename in ('win_bridge.py','recorder.py'):
                shutil.copyfile(checked_path(filename), engine/'scripts'/filename)
            tasks.save_task(plan, engine / '.desktop-select/tasks')
            dest = project / '.claude/skills/two-viewers'
            env = dict(os.environ)
            env.pop('DESKTOP_SELECT_TASK_DIR', None)
            def cli(cwd, *args):
                result = subprocess.run([sys.executable, '-X', 'utf8', '-B', *map(str,args)],
                                        cwd=cwd, env=env, encoding='utf8', capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            registered = cli(project, engine/'scripts/python_workflow.py', 'register', '--task', plan['name'],
                             '--out', dest.relative_to(project))
            self.assertFalse(registered['input_dispatched'])
            self.assertEqual(registered['target_validation'], 'recorded_evidence')
            self.assertEqual([s['number'] for s in registered['pending_scopes']], [3,4])
            guide = (dest/'SKILL.md').read_text(encoding='utf8')
            self.assertIn('${CLAUDE_SKILL_DIR}/scripts/run.py', guide)
            self.assertIn(f'Claude Code에서 사용자가 `/{dest.name}`으로 직접 호출했고 Company Agent', guide)
            self.assertIn(f'아직 성공하지 않은 경우에만 `Skill(skill="{dest.name}")`을 한 번 먼저 호출', guide)
            self.assertIn(plan['name'], guide.split('---')[1])
            self.assertNotIn('## 녹화한 순서', guide)
            details = (dest / flow.GUIDE_REFERENCE).read_text(encoding='utf8')
            self.assertEqual([s for s in details.splitlines() if s[:1].isdecimal()],
                             [f'{i}. {text}' for i,text in enumerate(captions,1)])
            self.assertEqual(json.loads((dest/'source-task.json').read_text(encoding='utf8')), original)
            self.assertEqual(flow.load(dest/'workflow.json'), recorded_plan.compile_plan(original)['envelope']['recipe'])
            elsewhere = project/'another-project'
            elsewhere.mkdir()
            (elsewhere/'python_workflow.py').write_text('raise AssertionError("wrong runtime")', encoding='utf8')
            preview = cli(elsewhere, dest/'scripts/run.py', '--input-plan')
            self.assertFalse(preview['input_dispatched'])
            self.assertEqual([s['number'] for s in preview['input_requirements']['steps']], [1,2,3,4])

    def test_arbitrary_draft_and_protected_steps_do_not_schedule_a_click(self):
        raw=dict(action='visual-click', visual=visual(), label='녹화한 화면 항목 누르기')
        draft='원하는 자료를 모아 보고서를 만들어 줘'
        saved=tasks.instructed_step(raw, tasks.compile_instruction(raw, draft))
        self.assertEqual(saved['instruction_draft'], draft)
        self.assertEqual(saved['visual_hint'], raw['visual'])
        self.assertNotIn('rgb', str(tasks.summary(dict(steps=[saved]))))
        self.assertEqual(tasks.compile_instruction(saved, SENTENCE)['mode'], 'visual-click')
        for reason in ('target_protected','protected_window'):
            with self.assertRaises(tasks.TaskError):
                tasks.compile_instruction(dict(action='manual', reason=reason), SENTENCE)

    def test_failed_visual_observation_does_not_register_target(self):
        recipe=dict(schema=1,name='visual',exe='Manager.exe',window='Manager',
                    steps=[dict(action='visual-click', label=SENTENCE, visual=visual())])
        self.patch(visual_target, 'execute', return_value=dict(status='blocked', input_dispatched=False,
                                                               diagnostic='visual_target_ambiguous'))
        self.assertEqual(flow.run(recipe, read_only=True)['status'], 'blocked')
