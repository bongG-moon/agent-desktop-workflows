"""An opened window must be the one named and bound by this recording."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import recorded_plan
import window_context
from test_recorded_plan import recording


def window(hwnd, title='Instance A', created='1234'):
    return dict(hwnd=hwnd, pid=hwnd+100, exe_name='Viewer.exe', title=title, process_started_utc_ticks=created)


def binding(row):
    return SimpleNamespace(hwnd=row['hwnd'], pid=row['pid'], exe=row['exe_name'],
                           created=row['process_started_utc_ticks'], check=mock.Mock())


def scope(**changes):
    return dict(exe='Viewer.exe', title='Instance A', **changes)


class LinkedWindowTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.owner = SimpleNamespace(hwnd=0x81230007, exe='Manager.exe', pid=1, created='1111', check=mock.Mock())
        self.records = []
        self.stack.enter_context(mock.patch.object(flow.app, 'windows', side_effect=lambda _:list(self.records)))
        self.stack.enter_context(mock.patch.object(flow.app.bridge, 'Binding', side_effect=binding))
        self.stack.enter_context(mock.patch.object(flow.app, 'guard'))
        self.stack.enter_context(mock.patch.object(flow, 'bind', return_value=self.owner))
        uia = self.stack.enter_context(mock.patch.object(flow.app.bridge, 'UIA')).return_value.__enter__.return_value
        uia.pid.return_value = self.owner.pid
        self.stack.enter_context(mock.patch.object(flow.app.bridge.user32, 'IsIconic', return_value=False))
        self.stack.enter_context(mock.patch.object(flow.app.visibility, 'restore_for_action', return_value=False))
        self.stack.enter_context(mock.patch.object(flow, 'resolve_step', return_value=(mock.Mock(), {}, None)))
        self.stack.enter_context(mock.patch.object(window_context, 'OPEN_TIMEOUT', 0))
        self.command = dict(action='invoke', selector=dict(automation_id='run', role='button'))
        self.child = dict(action='set-value', selector=dict(automation_id='field', role='textfield'),
                          value='example', window_context=scope(after_step=1))
        self.recipe = dict(schema=1, name='Two windows', exe='Manager.exe', window='Manager',
                           steps=[self.command, self.child, deepcopy(self.command)])

    def test_verified_child_scope_and_return_to_root(self):
        sent = []
        def execute(bound, *args, **kwargs):
            sent.append(bound.hwnd)
            if len(sent)==1:
                self.records.extend([window(2),window(3,'Instance B')])
                return dict(status='delivered_unverified', input_dispatched=True,
                            diagnostic='business_result_not_verified')
            return dict(status='verified', input_dispatched=True)
        with mock.patch.object(flow.live, 'execute', side_effect=execute):
            report = flow.run(self.recipe)
        self.assertEqual(sent, [self.owner.hwnd, 2, self.owner.hwnd])
        self.assertEqual(report['status'],'verified')
        self.assertEqual(report['steps'][0]['receipt']['verification_scope'],'recorded_window_opened')
        self.assertEqual(report['steps'][1]['receipt']['window_context'],self.child['window_context'])

    def test_double_click_step_alone_observes_its_popup_and_never_sends_again(self):
        from test_visual_target import image, visual, VisualTargets
        marker = visual.from_frame(VisualTargets.window, image((35,42)), (235,162))
        self.recipe['steps'][0] = dict(action='visual-click', label='Open result details',
                                       visual=dict(marker, gesture='double-click'))
        def click(bound, marker, guard, **options):
            self.assertEqual(marker['gesture'], 'double-click')
            self.assertEqual(options['first_match_timeout'], 0)
            self.records.append(window(2))
            return dict(status='delivered_unverified', input_dispatched=True,
                        diagnostic='visual_double_click_dispatched', physical_input=True)
        with (mock.patch.object(visual,'execute',side_effect=click) as send,
              mock.patch.object(flow.live,'execute') as child_action):
            report = flow.run(self.recipe, only=1)
            self.assertEqual(report['status'], 'verified', report)
            self.assertEqual(report['steps'][0]['receipt']['verification_scope'], 'recorded_window_opened')
            blocked = flow.run(self.recipe, only=1)
            self.assertEqual(blocked['diagnostic'], 'expected_new_window_already_open')
            self.assertFalse(blocked['input_dispatched'])
            send.assert_called_once()
            child_action.assert_not_called()

    def test_keyboard_opener_waits_for_its_window_in_full_and_single_step_runs(self):
        self.recipe['steps'][0] = dict(action='press-key', keys='Ctrl+F',
                                       selector=dict(automation_id='editor', role='textfield'))
        sent = []
        def execute(bound, uia, element, node, action, value, **kwargs):
            sent.append((bound.hwnd, action))
            if action == 'press-key':
                self.assertEqual(value, 'Ctrl+F')
                self.records.append(window(2))
                return dict(status='delivered_unverified', input_dispatched=True, diagnostic='key_chord_sent',
                            keys=value, keyboard_events_sent=4, physical_input=True)
            return dict(status='verified', input_dispatched=True)
        for only, expected in [(1, [(self.owner.hwnd, 'press-key')]),
                               (None, [(self.owner.hwnd, 'press-key'), (2, 'fill'), (self.owner.hwnd, 'invoke')])]:
            with self.subTest(only=only), mock.patch.object(flow.live, 'execute', side_effect=execute):
                self.records.clear()
                sent.clear()
                report = flow.run(self.recipe, only=only)
                self.assertEqual(report['status'], 'verified', report)
                self.assertEqual(report['steps'][0]['receipt']['verification_scope'], 'recorded_window_opened')
                self.assertEqual(sent, expected)
                blocked = flow.run(self.recipe, only=only)
                self.assertEqual(blocked['diagnostic'], 'expected_new_window_already_open')
                self.assertFalse(blocked['input_dispatched'])
                self.assertEqual(sent, expected)

    def test_duplicate_or_renamed_existing_window_never_receives_followup(self):
        for scenario in ('duplicate','renamed','missing'):
            with self.subTest(scenario=scenario):
                self.records[:] = [window(8,'Instance B')]
                sent = []
                def execute(bound, *args, **kwargs):
                    sent.append(bound.hwnd)
                    if scenario=='duplicate':
                        self.records.extend([window(2),window(3)])
                    elif scenario=='renamed':
                        self.records[:]=[window(8)]
                    return dict(status='delivered_unverified', input_dispatched=True,
                                diagnostic='business_result_not_verified')
                with mock.patch.object(flow.live,'execute',side_effect=execute):
                    report = flow.run(self.recipe)
                self.assertEqual(sent, [self.owner.hwnd])
                self.assertEqual(report['status'],'blocked')
                self.assertTrue(report['input_dispatched'])
                self.assertEqual(report['stopped_at'],2)

    def test_existing_window_is_pinned_before_input_and_cannot_be_substituted(self):
        self.child['window_context']=scope(mode='existing')
        self.records[:]=[window(3,'Instance B')]
        with mock.patch.object(flow.live, 'execute') as execute:
            missing = flow.run(self.recipe)
        self.assertEqual(missing['diagnostic'],'recorded_existing_window_missing')
        execute.assert_not_called()
        self.records[:]=[window(2)]
        def replace(*args, **kwargs):
            self.records[:]=[window(2,created='99999')]
            return dict(status='verified',input_dispatched=True)
        with mock.patch.object(flow.live,'execute',side_effect=replace) as execute:
            result=flow.run(self.recipe)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(result['diagnostic'],'recorded_window_lifetime_changed')

    def test_single_window_competitor_waits_for_all_steps_of_existing_linked_job(self):
        self.child['window_context'] = scope(mode='existing')
        self.records[:] = [window(0x77ff1234)]
        detail = binding(self.records[0])
        probes = []

        def probe():
            try:
                with flow.coordination.window(detail, 'Instance A'):
                    return 'available'
            except flow.coordination.CoordinationError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=1) as other, mock.patch.object(flow.coordination, 'QUEUE_SECONDS', 0):
            def send(bound, *args, **kwargs):
                probes.append((bound.hwnd, other.submit(probe).result(timeout=2)))
                return dict(status='verified', input_dispatched=True)
            with mock.patch.object(flow.live, 'execute', side_effect=send):
                self.assertEqual(flow.run(self.recipe)['status'], 'verified')
            self.assertEqual(other.submit(probe).result(timeout=2), 'available')
        self.assertEqual(probes, [(hwnd, 'window_queue_timeout') for hwnd in (self.owner.hwnd, detail.hwnd, self.owner.hwnd)])

    def test_only_opened_child_requires_opener_but_explicit_existing_child_can_be_used(self):
        self.records[:]=[window(2)]
        with mock.patch.object(flow.live, 'execute', return_value=dict(status='verified', input_dispatched=True)) as execute:
            self.assertEqual(flow.run(self.recipe, only=2)['diagnostic'],'window_origin_step_not_run')
            execute.assert_not_called()
            self.child['window_context']=scope(mode='existing')
            self.assertEqual(flow.run(self.recipe, only=2)['status'],'verified')
            self.assertEqual(execute.call_args.args[0].hwnd,2)

    def test_already_running_row_reuse_requires_recorded_identity_and_same_exact_lifetime(self):
        self.records[:]=[window(2)]
        self.recipe['steps'][0]=dict(action='row-launch',ordinal=1,label='첫번째 행 실행')
        for evidence,allowed in [('previously_observed_row',False),('recorded_row_hint',True)]:
            receipt=dict(status='verified', input_dispatched=False, observed_state='after', evidence=evidence)
            with (mock.patch.object(flow.row_intent, 'execute', return_value=receipt),
                  mock.patch.object(flow.live, 'execute', return_value=dict(status='verified',input_dispatched=True)) as send):
                result=flow.run(self.recipe)
            self.assertEqual(result['status'],'verified' if allowed else 'blocked')
            self.assertEqual(send.call_count, 2 if allowed else 0)

    def test_readonly_and_registration_defer_an_unopened_window_and_preserve_its_selector(self):
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(flow.live,'execute') as execute,
              mock.patch.object(flow.app.bridge,'UIA') as uia):
            uia.return_value.__enter__.return_value.actions.return_value=['Click']
            target=Path(directory)/'multi-window'
            result=flow.register(self.recipe,target)
            self.assertEqual(result['status'],'registered')
            self.assertEqual(flow.load(target/'workflow.json')['steps'][1],self.child)
            execute.assert_not_called()

    def test_automatic_child_must_still_be_related_to_the_nested_opener(self):
        self.command['window_context'] = dict(exe='Viewer.exe', title='Workspace', mode='existing')
        self.child['window_context'] = scope(after_step=1, related=True)
        for related_ok in (False, True):
            self.records[:] = [window(5,'Workspace')]
            sent = []
            def execute(bound, *args, **kwargs):
                sent.append(bound.hwnd)
                if len(sent) == 1:
                    self.records.append(window(2))
                return dict(status='verified', input_dispatched=True)
            def related(child, parent):
                self.assertEqual((child.hwnd, parent.hwnd), (2,5))
                return related_ok
            with (mock.patch('capture_windows.related', side_effect=related, create=True) as verify,
                  mock.patch.object(flow.live,'execute',side_effect=execute)):
                result = flow.run(self.recipe)
            self.assertTrue(verify.called)
            self.assertEqual(result['status'],'verified' if related_ok else 'blocked')
            self.assertEqual(sent, [5, 2, self.owner.hwnd] if related_ok else [5])

    def test_compile_preserves_source_numbers_and_translates_references_across_waits(self):
        raw=recording()
        first=raw['steps'][2]
        opener=deepcopy(first)
        raw['steps']=[dict(first,wait_after=1),opener,dict(raw['steps'][0],window_context=scope(after_step=2))]
        plan=recorded_plan.compile_plan(raw)
        self.assertEqual(plan['status'],'compiled')
        self.assertEqual(plan['envelope']['source_steps'],[1,1,2,3])
        self.assertEqual(plan['envelope']['recipe']['steps'][3]['window_context'],scope(after_step=3))
        self.assertEqual(raw['steps'][-1]['window_context']['after_step'],2)

    def test_two_launches_before_child_actions_require_each_exact_result_before_next_command(self):
        raw = recording()
        opener, field = raw['steps'][2], raw['steps'][0]
        raw['steps'] = [deepcopy(opener), deepcopy(opener), dict(field, window_context=scope(after_step=1)),
                        dict(field, window_context=dict(scope(after_step=2), title='Instance B'))]
        compiled = recorded_plan.compile_plan(raw)
        self.assertEqual(compiled['status'], 'compiled')
        recipe = compiled['envelope']['recipe']
        element = mock.Mock()
        element.property.return_value = opener['command_name']
        self.stack.enter_context(mock.patch.object(flow,'resolve_step',return_value=(element, {}, None)))
        self.stack.enter_context(mock.patch.object(flow.app,'field_label',
                                return_value=dict(label_source='labeled_by',semantic_label=field['label'])))
        owner = self.owner.hwnd
        for stop, expected in ((1, [owner]), (2, [owner, owner]), (0, [owner, owner, 2, 3])):
            with self.subTest(missing_window_after=stop):
                self.records.clear()
                sent = []
                def execute(bound, *args, **kwargs):
                    sent.append(bound.hwnd)
                    turn = len(sent)
                    if turn <= 2:
                        if turn != stop:
                            self.records.append(window(turn+1, 'Instance A' if turn==1 else 'Instance B'))
                        return dict(status='delivered_unverified',input_dispatched=True,
                                    diagnostic='business_result_not_verified')
                    return dict(status='verified',input_dispatched=True)
                with mock.patch.object(flow.live,'execute',side_effect=execute):
                    report = flow.run(recipe)
                self.assertEqual(sent, expected)
                self.assertEqual(report['status'], 'blocked' if stop else 'verified')
                if stop:
                    self.assertTrue(report['input_dispatched'])
                    self.assertEqual(report['stopped_at'], stop+1)
                else:
                    self.assertTrue(all(step['receipt']['verification_scope']=='recorded_window_opened'
                                        for step in report['steps'][:2]))
