"""Literal menu, owned-popup and workbook stages compose without implicit input retries."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow


def context():
    return dict(action='context-menu', spec=dict(target=dict(name='Results', role='table'),
                point=[.4, .4], path=['Export', 'Workbook']), label='Open export')


def popup_step(action='set-value'):
    step = dict(action=action, popup=dict(title='Export settings', timeout=0),
                selector=dict(name='Output' if action == 'set-value' else 'Finish',
                              role='textfield' if action == 'set-value' else 'button'))
    if action == 'set-value':
        step['parameter'] = 'Output'
    return step


def output():
    return dict(action='verify-file', output=dict(kind='file', path=r'C:\DesktopSelectTest\export.xlsx'), timeout=0)


def recipe(*steps):
    return dict(schema=1, name='Export workflow', exe='Example.exe', window='Example window', steps=list(steps))


def delivered_menu():
    return dict(status='delivered_unverified', diagnostic='menu_leaf_sent', input_dispatched=True,
                leaf_dispatched=True, menu_path_completed=True)


class SchemaTests(unittest.TestCase):
    def test_composition_is_literal_and_strict(self):
        data = recipe(context(), popup_step(), popup_step('invoke'), output())
        self.assertEqual(flow.validate(data), data)
        for change in (dict(popup=dict(title='Export settings', timeout=11)),
                       dict(popup=dict(title='Export settings', timeout=True)),
                       dict(popup=dict(title='Export settings', timeout=float('nan'))),
                       dict(popup=dict(title='Export settings', hwnd=200)),
                       dict(popup=dict(title=' Export settings'))):
            with self.subTest(change=change), self.assertRaises(ValueError):
                flow.validate(recipe(dict(popup_step(), **change)))
        for timeout in (-1, 121, True, float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                flow.validate(recipe(dict(output(), timeout=timeout)))
        for step in (dict(context(), selector={'name': 'Results'}),
                     dict(output(), command='export'), dict(context(), popup={'title': 'Other'})):
            with self.subTest(step=step), self.assertRaises(ValueError):
                flow.validate(recipe(step))

    def test_missing_variable_stops_before_baseline_binding_or_input(self):
        with patch.object(flow, 'bind') as bind, patch.object(flow.export_evidence, 'baseline') as baseline:
            result = flow.run(recipe(context(), popup_step(), output()))
        self.assertEqual(result['missing_parameters'], ['Output'])
        self.assertIs(result['input_dispatched'], False)
        bind.assert_not_called()
        baseline.assert_not_called()


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.owner = MagicMock(pid=11, hwnd=100)
        self.popup = MagicMock(pid=11, hwnd=200)
        self.uia = MagicMock()
        self.uia.__enter__.return_value = self.uia
        self.uia.same.return_value = True
        self.uia.actions.return_value = ['SetValue', 'Click']
        self.element = MagicMock()
        self.element.property.return_value = ''
        self.popups, self.events = [], []
        self.disabled = False
        patches = {
            'bind': dict(return_value=self.owner),
            'app.bridge.UIA': dict(return_value=self.uia),
            'app.guard': dict(side_effect=self.guard),
            'app.owned_popups': dict(side_effect=lambda _: list(self.popups)),
            'app.bind_surface': dict(return_value=self.popup),
            'app.bridge.window_text': dict(return_value='Export settings'),
            'app.bridge.user32.IsWindowVisible': dict(return_value=True),
            'app.visibility.restore_for_action': dict(side_effect=self.restore),
            'lookup.resolve': dict(return_value=self.element),
            'context_menu._region': dict(return_value=self.element),
            'context_menu.execute': dict(side_effect=self.menu),
            'live.execute': dict(side_effect=self.execute),
            'live.text_readback': dict(return_value=dict(value='old')),
            'export_evidence.baseline': dict(side_effect=self.baseline),
            'export_evidence.inventory': dict(return_value={}),
            'export_evidence.wait': dict(side_effect=self.file_check),
        }
        for target, options in patches.items():
            p = patch('python_workflow.' + target, **options)
            p.start()
            self.addCleanup(p.stop)

    def guard(self, owner, surface, title, mutate=False):
        self.assertEqual(title, 'Example window')
        if mutate and surface is self.owner and self.disabled:
            raise flow.WorkflowError('window_disabled')

    def restore(self, *_):
        self.events.append('restore')
        return False

    def baseline(self, spec):
        self.events.append('baseline')
        return {'before': 'original'}

    def file_check(self, spec, before, **options):
        self.assertEqual(before, {'before': 'original'})
        self.events.append('file')
        return True, 'export_workbook_verified'

    def menu(self, owner, spec, guard):
        guard()
        self.events.append('menu')
        self.open_popup()
        return delivered_menu()

    def open_popup(self):
        self.popups = [dict(hwnd=200, title='Export settings')]
        self.disabled = True

    def execute(self, surface, uia, element, node, operation, value, *, still_valid):
        still_valid()
        self.events.append((operation, surface.hwnd, value))
        if operation in ('fill', 'select'):
            return dict(status='verified', input_dispatched=True)
        if surface is self.popup:
            self.popups = []
            self.disabled = False
        return dict(status='delivered_unverified', diagnostic='business_result_not_verified', input_dispatched=True)

    def test_menu_popup_and_fresh_file_use_baseline_before_first_mutation(self):
        result = flow.run(recipe(context(), popup_step(), popup_step('invoke'), output()), {'Output': 'report'})
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual(self.events, ['baseline', 'restore', 'menu', ('fill', 200, 'report'), ('invoke', 200, None), 'file'])
        self.assertTrue(result['steps'][-1]['receipt']['file_verified'])
        self.assertEqual(result['steps'][-1]['receipt']['baseline_scope'], 'before_workflow')
        self.assertEqual(flow.app.bridge.UIA.call_count, 1)
        self.assertTrue(all('elapsed_seconds' in row for row in result['steps']))
        self.assertIn('lookup_seconds', result['steps'][1]['receipt'])

    def test_missing_or_ambiguous_popup_does_not_restore_or_input_owner(self):
        for popups, diagnostic in (([], 'popup_not_found'),
                ([dict(hwnd=200, title='Export settings'), dict(hwnd=201, title='Export settings')], 'ambiguous_popup')):
            self.popups = popups
            with self.subTest(diagnostic=diagnostic):
                result = flow.run(recipe(popup_step()), {'Output': 'report'})
                self.assertEqual(result['diagnostic'], diagnostic)
                self.assertFalse(result['input_dispatched'])
        flow.app.visibility.restore_for_action.assert_not_called()
        flow.live.execute.assert_not_called()

    def test_anchored_field_is_exposed_before_mutation_but_never_in_read_only(self):
        step = dict(action='set-value', selector=dict(anchor=dict(name='Form', role='group'),
                    point=[.5, .3], role='textfield', class_name='Edit'), value='value')
        proof = MagicMock(element=self.element)
        proof.revalidate.return_value = self.element
        with patch('row_action.foreground', side_effect=lambda surface: self.events.append('foreground')) as expose, \
                patch.object(flow.lookup, 'bind_anchored', return_value=proof, create=True) as bind_anchor:
            result = flow.run(recipe(step))
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(self.events, ['restore', 'foreground', ('fill', 100, 'value')])
            expose.assert_called_once_with(self.owner)
            bind_anchor.assert_called_once()
            proof.revalidate.assert_called_once()
            proof.close.assert_called_once()
            expose.reset_mock()
            bind_anchor.reset_mock()
            result = flow.run(recipe(step), read_only=True)
            self.assertEqual(result['status'], 'observed')
            expose.assert_not_called()
            bind_anchor.assert_not_called()

    def test_unknown_unowned_popup_never_falls_back_to_owner(self):
        self.open_popup()
        flow.app.bind_surface.side_effect = flow.WorkflowError('popup_not_owned_or_closed')
        result = flow.run(recipe(popup_step()), {'Output': 'report'})
        self.assertEqual(result['diagnostic'], 'popup_not_owned_or_closed')
        flow.live.execute.assert_not_called()
        flow.app.visibility.restore_for_action.assert_not_called()

    def test_foreign_pid_or_changed_title_is_blocked_before_input(self):
        self.open_popup()
        self.popup.pid = 22
        result = flow.run(recipe(popup_step()), {'Output': 'report'})
        self.assertEqual(result['diagnostic'], 'popup_owner_mismatch')
        self.popup.pid = 11
        flow.app.bridge.window_text.return_value = 'Different popup'
        result = flow.run(recipe(popup_step()), {'Output': 'report'})
        self.assertEqual(result['diagnostic'], 'popup_title_changed')
        flow.live.execute.assert_not_called()

    def test_popup_may_disable_owner_and_main_scope_is_revalidated_after_close(self):
        self.open_popup()
        main = dict(action='invoke', selector=dict(name='Continue', role='button'))
        result = flow.run(recipe(popup_step(), popup_step('invoke'), main), {'Output': 'report'})
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual(self.events, [('fill', 200, 'report'), ('invoke', 200, None), 'restore', ('invoke', 100, None)])

    def test_uncertain_or_incomplete_menu_never_advances(self):
        for fields in (dict(status='uncertain'), dict(status='verified'), dict(leaf_dispatched=None), dict(menu_path_completed=False),
                       dict(input_dispatched=None), dict(diagnostic='other')):
            with self.subTest(fields=fields):
                flow.context_menu.execute.side_effect = None
                flow.context_menu.execute.return_value = dict(delivered_menu(), **fields)
                result = flow.run(recipe(context(), popup_step(), output()), {'Output': 'report'})
                self.assertEqual(result['stopped_at'], 1)
                self.assertEqual(len(result['steps']), 1)
        flow.live.execute.assert_not_called()
        flow.export_evidence.wait.assert_not_called()

    def test_failed_file_stops_without_retrying_or_next_button(self):
        flow.export_evidence.wait.side_effect = None
        flow.export_evidence.wait.return_value = False, 'export_workbook_not_observed'
        main = dict(action='invoke', selector=dict(name='After', role='button'))
        result = flow.run(recipe(context(), output(), main))
        self.assertEqual((result['status'], result['stopped_at']), ('blocked', 2))
        flow.context_menu.execute.assert_called_once()
        flow.live.execute.assert_not_called()

    def test_baseline_failure_stops_before_restore_and_every_input(self):
        flow.export_evidence.baseline.side_effect = flow.WorkflowError('export_path_redirected')
        result = flow.run(recipe(context(), output()))
        self.assertEqual(result['diagnostic'], 'export_path_redirected')
        self.assertFalse(result['input_dispatched'])
        flow.app.visibility.restore_for_action.assert_not_called()
        flow.context_menu.execute.assert_not_called()

    def test_register_preserves_future_popup_steps_and_reports_pending(self):
        data = recipe(context(), dict(action='wait', seconds=0), popup_step(), popup_step('invoke'), output())
        before = deepcopy(data)
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'export-skill'
            result = flow.register(data, destination)
            self.assertEqual(flow.load(destination / 'workflow.json'), before)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual(result['target_validation'], 'pending_scopes')
            self.assertEqual([row['number'] for row in result['pending_scopes']], [3, 4])
        self.assertEqual(data, before)
        flow.context_menu.execute.assert_not_called()
        flow.live.execute.assert_not_called()
        flow.export_evidence.baseline.assert_not_called()
        flow.export_evidence.wait.assert_not_called()
        flow.app.visibility.restore_for_action.assert_not_called()

    def test_registration_validates_currently_visible_popup_patterns(self):
        self.open_popup()
        self.uia.actions.return_value = ['Click']
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(flow.WorkflowError, 'workflow_pattern_unavailable'):
                flow.register(recipe(popup_step()), Path(temporary) / 'export-skill')
        flow.live.execute.assert_not_called()

    def test_registered_guides_explain_mouse_and_anchor_visibility_without_submit(self):
        anchored = dict(action='set-value', value='N', selector=dict(
            anchor=dict(name='Conditions', role='group'), point=[.5, .2],
            role='textfield', class_name='Edit'))
        normal = dict(action='set-value', value='N', selector=dict(name='Status', role='textfield'))
        for name, step, expected, excluded in (
                ('menu', context(), '실제 마우스로 표를 우클릭', '상위 영역과 상대 위치로 찾는 입력란'),
                ('anchor', anchored, '상위 영역과 상대 위치로 찾는 입력란', '실제 마우스로 표를 우클릭'),
                ('normal', normal, '값 확인 실패', '대상 창을 앞으로 가져')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / ('registered-' + name)
                result = flow.register(recipe(step), destination)
                self.assertEqual(result['status'], 'registered')
                guide = (destination / 'SKILL.md').read_text(encoding='utf8')
                self.assertIn(expected, guide)
                self.assertNotIn(excluded, guide)
                self.assertNotIn('submit 단계', guide)
                if name != 'normal':
                    self.assertIn('마우스와 키보드를 사용하지 않는다', guide)
                if name == 'anchor':
                    self.assertIn('같은 종류 입력란의 배치가 바뀌면', guide)
                    self.assertIn('등록과 --read-only는 창을 앞으로 가져오지 않으므로', guide)
        flow.live.execute.assert_not_called()
        flow.context_menu.execute.assert_not_called()
        flow.app.visibility.restore_for_action.assert_not_called()

    def test_popup_wait_is_bounded_and_readonly(self):
        with patch.object(flow.time, 'monotonic', side_effect=[0, 0, .2]), patch.object(flow.time, 'sleep') as sleep:
            with self.assertRaisesRegex(flow.WorkflowError, 'popup_not_found'):
                flow.popup_surface(self.owner, 'Example window', dict(title='Export settings', timeout=.2))
        sleep.assert_called_once_with(.1)
        flow.live.execute.assert_not_called()
        flow.app.visibility.restore_for_action.assert_not_called()

    def test_discover_preserves_named_controls_and_omits_unstable_numeric_ids(self):
        def control(identifier, name, role):
            item = MagicMock()
            item.property.side_effect = lambda prop: {30011: identifier, 30005: name, 30003: role}.get(prop)
            return item
        controls = [control(None, 'Run', 50031), control('123456', 'Finish', 50000),
                    control('123457', '', 50000), control('queryField', 'Query', 50003)]
        with patch.object(flow.lookup, 'find_all', return_value=controls) as find, patch.object(flow.app, 'field_label', return_value={}):
            result = flow.discover('Example.exe', 'Example window')
        self.assertIn('splitbutton', find.call_args.kwargs['roles'])
        self.assertEqual([row['selector'] for row in result['targets']],
                         [dict(name='Run', role='splitbutton'), dict(name='Finish', role='button'),
                          dict(automation_id='queryField', role='combobox')])
        self.assertNotIn('automation_id', result['targets'][1])

    def test_submit_continues_only_after_known_enter_pair_delivery(self):
        submit = dict(action='submit', selector=dict(name='Filter', role='textfield'))
        self.uia.actions.return_value = ['SetValue', 'Click', 'SetFocus']
        accepted = dict(status='delivered_unverified', diagnostic='submit_enter_sent', input_dispatched=True,
                        keyboard_events_sent=2, physical_input=True)
        def send(*args, **kwargs):
            kwargs['still_valid']()
            return accepted
        flow.live.execute.side_effect = send
        result = flow.run(recipe(submit, context()))
        self.assertEqual(len(result['steps']), 2)
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertTrue(result['physical_input'])
        self.assertEqual(flow.live.execute.call_args.args[4], 'submit')
        flow.context_menu.execute.reset_mock()
        for bad in (dict(accepted, keyboard_events_sent=1), dict(accepted, status='uncertain'),
                    dict(accepted, status='verified'), dict(accepted, input_dispatched=None)):
            self.disabled = False
            flow.live.execute.side_effect = None
            flow.live.execute.return_value = bad
            with self.subTest(bad=bad):
                result = flow.run(recipe(submit, context()))
                self.assertEqual(result['stopped_at'], 1)
        flow.context_menu.execute.assert_not_called()

    def test_register_submit_checks_capability_without_focusing(self):
        submit = dict(action='submit', selector=dict(name='Filter', role='textfield'))
        self.uia.actions.return_value = ['SetValue', 'SetFocus']
        with patch.object(flow.live, 'submit_ready', return_value=self.element) as readiness:
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / 'submit-skill'
                result = flow.register(recipe(submit), destination)
                self.assertEqual(result['status'], 'registered')
                self.assertEqual(flow.load(destination / 'workflow.json')['steps'], [submit])
            readiness.assert_called_once()
        flow.live.execute.assert_not_called()
        self.element.call.assert_not_called()
        flow.app.visibility.restore_for_action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
