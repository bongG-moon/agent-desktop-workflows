"""One bitmapped list, many identical controls, free-form named-row instructions."""
from pathlib import Path
import sys
from contextlib import ExitStack
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/desktop-select/scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import desktop_select
import grounding
import learned_tasks
import python_workflow as flow
import recorded_plan
import row_target as target
from test_row_intent import painted


def rows(items, pos=(70,110)):
    data = bytearray()
    layout = []
    for index, (key, launchable) in enumerate(items):
        pixels, w, h = painted(triangle=launchable)
        rgb = bytearray(pixels)
        for y in range(13,26):
            for x in range(7,20):
                ink = x in (7,8) or y in (13,14)
                if key != '10.':
                    ink = x in (16,17) or y in (24,25)
                if ink:
                    rgb[(y*w+x)*3:(y*w+x+1)*3] = bytes(3)
        bgra = bytearray(w*h*4)
        bgra[::4],bgra[1::4],bgra[2::4] = rgb[2::3],rgb[1::3],rgb[::3]
        data.extend(bgra)
        layout.append(([pos[0],pos[1]+index*h,w,h],dict(role='listitem',scope=['Devices'])))
    return [*pos,w,h*len(items)], layout, bytes(data)


class GroundedRowTests(TestCase):
    def setUp(self):
        self.items = [('10.',True), ('11.',True)]
        self.binding = MagicMock(hwnd=4,pid=9)
        self.guard = MagicMock()
        self.sample = rows(self.items)
        self.png = grounding._png(*self.sample[0][2:],self.sample[2])
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(target.vision,'_snapshot',side_effect=lambda b:self.sample))
        self.stack.enter_context(patch.object(target.row_intent,'_named_button',return_value=None))
        self.foreground = self.stack.enter_context(patch.object(target.row_action,'foreground'))
        self.combo = self.stack.enter_context(patch.object(target.bridge,'_combo_surface'))
        self.stack.enter_context(patch.object(target.row_action,'_input_tick',return_value=103))
        self.hit = self.stack.enter_context(patch.object(target.vision,'confirm_point'))
        self.check = self.stack.enter_context(patch.object(target,'_confirm'))
        self.send = self.stack.enter_context(patch.object(target.bridge,'_send_point',
                                                          return_value=dict(ok=True,input_dispatched=True)))

    def bind(self):
        return target.bind(self.binding,self.png,[12,19],[52,20])

    def test_repeated_glyph_can_bind_and_launch_correct_sorted_row(self):
        spec = self.bind()
        self.assertNotIn('button', spec)
        self.sample = rows(list(reversed(self.items)), (-210,160))
        result = target.execute(self.binding,spec,self.guard,child_open=lambda:False)
        self.assertEqual(result['diagnostic'], 'row_open_dispatched')
        self.assertEqual(result['status'], 'delivered_unverified')
        self.assertEqual(self.send.call_args.args[1], 220)
        self.check.assert_called_once()
        self.hit.assert_called_once()
        self.send.assert_called_once()

    def test_warm_bound_power_button_never_serves_as_launch(self):
        self.sample = rows([('10.',False), ('11.',True)])
        self.png = grounding._png(*self.sample[0][2:],self.sample[2])
        spec = self.bind()
        result = target.execute(self.binding,spec,self.guard,child_open=lambda:False)
        self.assertEqual(result['diagnostic'], 'row_launch_button_not_found')
        result = target.execute(self.binding,spec,self.guard,child_open=lambda:True)
        self.assertEqual(result['observed_state'], 'after')
        self.foreground.assert_not_called()
        self.send.assert_not_called()
        self.sample = rows(self.items)
        self.assertEqual(target.execute(self.binding,spec,self.guard,read_only=True)['observed_state'], 'before')
        self.send.assert_not_called()

    def test_cannot_bind_across_rows_or_to_stale_screenshot(self):
        with self.assertRaisesRegex(target.bridge.BridgeError,'row_points_in_different_rows'):
            target.bind(self.binding,self.png,[12,19],[52,60])
        self.sample = rows(list(reversed(self.items)))
        with self.assertRaisesRegex(target.bridge.BridgeError,'row_changed_since_grounding'):
            self.bind()
        self.send.assert_not_called()

    def test_neither_duplicate_label_nor_existing_window_proves_the_row(self):
        spec = self.bind()
        self.sample = rows([self.items[0], self.items[0]])
        for opened in (False,True):
            result = target.execute(self.binding,spec,self.guard,child_open=lambda:opened)
            self.assertEqual(result['diagnostic'], 'row_identity_ambiguous')
        self.foreground.assert_not_called()
        self.send.assert_not_called()

    def test_stop_or_sort_before_dispatch_sends_nothing(self):
        spec = self.bind()
        self.foreground.side_effect = lambda b:setattr(self,'sample',rows(list(reversed(self.items))))
        result = target.execute(self.binding,spec,self.guard)
        self.assertEqual(result['diagnostic'], 'row_changed_before_action')
        self.foreground.side_effect = lambda b:setattr(self,'sample',rows([('10.',False),('11.',True)]))
        result = target.execute(self.binding,spec,self.guard)
        self.assertEqual(result['diagnostic'], 'row_launch_button_not_found')
        self.send.assert_not_called()

    def test_pointer_delivery_uncertainty_is_not_retried(self):
        spec = self.bind()
        self.send.side_effect = OSError()
        result = target.execute(self.binding,spec,self.guard)
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['input_dispatched'])
        self.send.assert_called_once()

    def test_replay_handoff_binds_two_repeated_buttons_without_rewriting_source(self):
        def opener(label):
            return dict(action='open',match='query',target=label,role='button',expand=False,
                        instruction=dict(mode='recorded',text=label+' 버튼 누르기'))
        def child(number, title):
            return dict(action='open',match='name',target='Preferences',role='button',expand=False,
                        window_context=dict(after_step=number,exe='Viewer.exe',title=title))
        plan = dict(schema=1, name='watch', exe='Manager.exe',window='Managers',steps=[
            opener('Blue'),opener('Green'),child(1,'Blue task'),child(2,'Green task')])
        store = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = learned_tasks.save_task(plan,store)
        original = path.read_bytes()
        owner = SimpleNamespace(hwnd=5,pid=42,created='100',exe='Manager.exe')
        app = desktop_select
        def worker(args, mutating, payload):
            return flow.run(payload['recipe'])
        with (patch.object(app,'task_directory',return_value=store),
              patch.object(app,'worker_receipt',side_effect=worker),
              patch.object(flow,'bind',return_value=owner),
              patch.object(flow,'resolve_step',side_effect=app.SelectionError('target_missing')),
              patch.object(app,'windows',return_value=[]),patch.object(app,'guard'),
              patch.object(app.bridge,'UIA'),patch.object(app.bridge,'set_dpi_awareness'),
              patch.object(grounding,'_directory',return_value=store),
              patch.object(app,'pick_window'),patch.object(app.bridge,'Binding',return_value=owner),
              patch.object(grounding,'_capture',return_value=(self.sample[0],self.png))):
            cli = app.parser()
            replay = cli.parse_args(['replay','--task','watch','--exe','Manager.exe','--window','Managers'])
            result = app.run(replay)
            self.assertEqual(result['host_action'],'ground_named_rows')
            self.assertEqual([(r['number'],r['row_binding_command']) for r in result['grounding_steps']],
                             [(1,'bind-row'),(2,'bind-row')])
            self.assertFalse(result['input_dispatched'])
            self.assertFalse(result['prior_input_requires_observation'])
            for index, request in enumerate(result['grounding_steps']):
                report = app.run(cli.parse_args(request['ground_args']))
                self.assertEqual(report['expected_windows'][0]['title'], ('Blue task','Green task')[index])
                args = ['bind-row','--task','watch','--only',str(index+1),'--frame',report['frame'],
                        '--row-point','12',str(19+40*index),'--point','52',str(20+40*index)]
                bound = app.run(cli.parse_args(args))
                self.assertEqual(app.result_code(bound),0)
                self.assertFalse(bound['input_dispatched'])
            self.send.assert_not_called()
            self.assertEqual(path.read_bytes(),original)
            envelope = recorded_plan.load_plan(plan,store)['envelope']
            self.assertEqual([s['action'] for s in envelope['recipe']['steps']],['row-open','row-open','invoke','invoke'])
            self.assertEqual(envelope['source'],plan)

    def test_bound_row_and_legacy_variable_share_normal_and_single_step_replay(self):
        raw = dict(schema=1, name='open-job', exe='Manager.exe', window='Manager', steps=[
            dict(action='select', match='automation_id', target='cboType', role='combobox',
                 parameter='Type', value='Old', recorded_effect='matched'),
            dict(action='open', match='query', target='Blue', role='button', expand=False,
                 instruction=dict(mode='recorded', text='Blue 버튼 누르기')),
            dict(action='open', match='name', target='Preferences', role='button', expand=False,
                 window_context=dict(after_step=2,exe='Viewer.exe',title='Blue job'))])
        store = Path(self.enterContext(tempfile.TemporaryDirectory()))
        learned_tasks.save_task(raw, store)
        grounding._save_row(raw, store, 2, self.bind())
        args = SimpleNamespace(exe=raw['exe'],window=raw['window'],only=None,hwnd=None)
        with (patch.object(desktop_select,'task_directory',return_value=store),
              patch.object(desktop_select,'worker_receipt',return_value=dict(steps=[])) as worker):
            for overrides, expected in (({},'Old'), ({'Type':'New'},'New')):
                result = desktop_select.fast_replay(raw,args,overrides)
                self.assertEqual(result['execution_engine'],'saved_python_plan')
                steps = worker.call_args.kwargs['payload']['recipe']['steps']
                self.assertEqual(steps[0]['value'],expected)
                self.assertEqual(steps[1]['action'],'row-open')
            args.only = '2'
            result = desktop_select.fast_replay(raw,args,{})
            self.assertEqual(result['execution_engine'],'saved_python_plan')
            self.assertEqual(worker.call_args.kwargs['payload']['only'],'2')

    def test_missing_child_after_click_preserves_delivery_and_stops_before_next_row(self):
        spec = self.bind()
        recipe = dict(schema=1,name='open-jobs',exe='Manager.exe',window='Manager',steps=[
            dict(action='row-open',label='Blue',row_target=spec),
            dict(action='row-open',label='Green',row_target=spec),
            dict(action='invoke',selector=dict(role='button',name='OK'),
                 window_context=dict(after_step=1,exe='Viewer.exe',title='Blue job')),
            dict(action='invoke',selector=dict(role='button',name='OK'),
                 window_context=dict(after_step=2,exe='Viewer.exe',title='Green job'))])
        owner = SimpleNamespace(hwnd=5,pid=42,created='100',exe='Manager.exe')
        app = desktop_select
        with (patch.object(flow,'bind',return_value=owner),patch.object(app,'guard'),
              patch.object(flow,'check_surface'),patch.object(app,'windows',return_value=[]),
              patch.object(flow.window_context,'OPEN_TIMEOUT',0),patch.object(app.bridge,'UIA'),
              patch.object(app.bridge,'set_dpi_awareness'),patch.object(app.visibility,'restore_for_action')):
            result = flow.run(recipe)
        self.assertEqual(result['diagnostic'],'recorded_window_not_open')
        self.assertEqual(result['stopped_at'],1)
        self.assertTrue(result['input_dispatched'])
        self.assertTrue(result['physical_input'])
        self.assertEqual(len(result['steps']),1)
        self.assertEqual(result['steps'][0]['receipt']['diagnostic'],'row_open_dispatched')
        self.send.assert_called_once()
