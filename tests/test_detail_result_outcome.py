"""Distinguish a witnessed detail read and close from delivery of a query."""
from copy import deepcopy
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import result_cell
import window_context
from test_linked_window_replay import binding, window


def recipe():
    scope = dict(exe='Viewer.exe', title='Detail', after_step=5, owned=True)
    return dict(schema=1, name='Read result', exe='Viewer.exe', window='Search', steps=[
        dict(action='set-value', selector=dict(role='textfield', automation_id='filter'), parameter='ID'),
        dict(action='invoke', selector=dict(role='button', name='VIEW')),
        dict(action='wait', seconds=0),
        dict(action='verify-value', selector=dict(role='group', automation_id='results'), result_column='ID',
             parameter='ID'),
        dict(action='double-click-result'),
        dict(action='read-value', selector=dict(role='textfield', automation_id='memo'), window_context=scope),
        dict(action='invoke', selector=dict(role='button', name='Exit'), window_context=scope, verify_closed=True)])


def receipts():
    content = [
        dict(status='verified'),
        dict(status='delivered_unverified', diagnostic='business_result_not_verified', input_dispatched=True),
        dict(status='waited'),
        dict(status='verified', value='B', value_evidence=dict(status='observed')),
        dict(status='verified', verification_scope='recorded_window_opened'),
        dict(status='observed', value='Memo B', value_evidence=dict(status='observed')),
        dict(status='verified', diagnostic='recorded_window_closed')]
    return [dict(number=i, receipt=r) for i, r in enumerate(content, 1)]


class DetailOutcomeTests(TestCase):
    def test_named_header_and_verified_detail_field_do_not_assert_a_fresh_query(self):
        spec, rows = recipe(), receipts()
        del spec['steps'][2:4], rows[2:4]
        header = spec['steps'][2]
        header.update(result_row='header', result_column='Identifier', selector=dict(role='group', automation_id='ReportList'),
                      result_point=dict(x=10, y=8, width=80, height=30))
        scope = dict(spec['steps'][3]['window_context'], after_step=3)
        for step in spec['steps'][3:]:
            step['window_context'] = scope
        spec['steps'].insert(3, dict(action='verify-value', selector=dict(role='textfield', automation_id='actualIdentifier'),
                                     parameter='ID', window_context=scope))
        rows[2]['receipt'].update(result_row='header', value_comparison=False,
            target_resolution='recorded_column_header', result_observation=dict(strategy='uia_header', column='Identifier'))
        rows.insert(3, dict(number=4, receipt=dict(status='verified', value='B', value_evidence=dict(status='observed'))))
        flow.validate(spec)
        self.assertTrue(flow.verified_detail_read(spec['steps'], rows))
        rows[2]['receipt']['result_observation']['column'] = 'Other column'
        self.assertFalse(flow.verified_detail_read(spec['steps'], rows))
        rows[2]['receipt']['result_observation']['column'] = 'Identifier'
        rows[3]['receipt']['status'] = 'mismatch'
        self.assertFalse(flow.verified_detail_read(spec['steps'], rows))

    def test_plain_recorded_result_confirms_only_the_opened_read_and_closed_detail(self):
        spec = recipe()
        rows = receipts()
        del spec['steps'][2:4], rows[2:4]
        spec['steps'][2].update(result_row='recorded', selector=dict(role='group', automation_id='ReportList'))
        for step in spec['steps'][3:]:
            step['window_context']['after_step'] = 3
        flow.validate(spec)
        rows[2]['receipt'].update(result_row='recorded', value_comparison=False)
        self.assertTrue(flow.verified_detail_read(spec['steps'], rows))
        for index, key in ((2, 'verification_scope'), (3, 'value_evidence'), (4, 'diagnostic')):
            changed = deepcopy(rows)
            changed[index]['receipt'].pop(key)
            self.assertFalse(flow.verified_detail_read(spec['steps'], changed))

    def test_only_query_delivery_may_be_unverified(self):
        steps, rows = recipe()['steps'], receipts()
        self.assertTrue(flow.verified_detail_read(steps, rows))
        for index in (0, 2, 3, 4, 5, 6):
            with self.subTest(step=index+1):
                changed = deepcopy(rows)
                changed[index]['receipt']['status'] = 'delivered_unverified'
                self.assertFalse(flow.verified_detail_read(steps, changed))

    def test_query_must_target_same_window_as_matched_row(self):
        task = recipe()
        task['steps'][1]['window_context'] = dict(exe='Viewer.exe', title='Other search', mode='existing')
        flow.validate(task)
        self.assertFalse(flow.verified_detail_read(task['steps'], receipts()))

    def test_nested_popup_read_or_close_does_not_complete_detail_outcome(self):
        for index in (5, 6):
            with self.subTest(step=index+1):
                task = recipe()
                task['steps'][index]['popup'] = dict(title='Nested dialog')
                flow.validate(task)
                self.assertFalse(flow.verified_detail_read(task['steps'], receipts()))

    def test_partial_run_missing_observation_or_unconfirmed_close_do_not_complete(self):
        steps, rows = recipe()['steps'], receipts()
        self.assertFalse(flow.verified_detail_read(steps, rows[1:]))
        self.assertFalse(flow.verified_detail_read(steps, rows[:-1]))
        rows[5]['receipt']['value_evidence']['status'] = 'unavailable'
        self.assertFalse(flow.verified_detail_read(steps, rows))
        rows = receipts()
        rows[-1]['receipt']['diagnostic'] = 'window_close_unverified'
        self.assertFalse(flow.verified_detail_read(steps, rows))


class OwnedWindowTests(TestCase):
    def test_linked_window_wait_accepts_only_a_bounded_seconds_value(self):
        spec = dict(exe='Example.exe', title='Detail', related=True, after_step=2)
        for seconds in (0, .25, 3, 30):
            self.assertEqual(window_context.validate_context(dict(spec, timeout=seconds))['timeout'], seconds)
        for seconds in (-1, 31, True, float('nan')):
            with self.assertRaisesRegex(window_context.ContextError, 'invalid_window_context'):
                window_context.validate_context(dict(spec, timeout=seconds))

    def setUp(self):
        self.task = recipe()
        self.owner = binding(window(101, title='Search'))
        self.rows = []
        self.owned = set()
        self.enterContext(patch.object(flow.app, 'windows', side_effect=lambda exe: list(self.rows)))
        self.enterContext(patch.object(flow.app.bridge, 'owned', side_effect=lambda c,p: (c,p) in self.owned))
        self.enterContext(patch.object(flow.app.bridge, 'Binding', side_effect=binding))
        self.enterContext(patch.object(flow.app, 'guard'))
        self.enterContext(patch.object(window_context, 'OPEN_TIMEOUT', 0))

    def detail(self, hwnd):
        return dict(window(hwnd, title='Detail'), pid=self.owner.pid)

    def test_registered_opener_ticket_continues_on_exact_window_and_cannot_replay(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        import tempfile
        import workflow_repair

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(workflow_repair.tempfile, 'gettempdir', return_value=str(directory)))
        path = directory / 'workflow.json'
        (directory / 'scripts').mkdir()
        (directory / 'scripts/run.py').touch()
        scope = dict(exe=self.task['exe'], title='Detail', after_step=2, owned=True)
        self.task['steps'] = [self.task['steps'][1], dict(action='double-click-result', result_row='first',
                             selector=dict(role='group', automation_id='results')),
                             *[dict(s, window_context=scope) for s in self.task['steps'][-2:]]]
        path.write_text(json.dumps(self.task), encoding='utf8')
        detail = self.detail(111)
        self.owned.add((111, self.owner.hwnd))
        cookies = {self.owner.hwnd: 114, 111: 115}
        actions = []
        memo_ready = True
        element = Mock()
        uia = self.enterContext(patch.object(flow.app.bridge, 'UIA')).return_value.__enter__.return_value
        uia.pid.return_value = self.owner.pid

        def run(*args):
            with redirect_stdout(StringIO()) as stream:
                flow.main(['--_worker', 'run', '--recipe', str(path), *args])
            return json.loads(stream.getvalue())

        def observe(*args, on_match, **kwargs):
            on_match(element)
            return dict(status='observed', result_observation=dict(strategy='uia'), input_dispatched=False)

        def open_row(*args, **kwargs):
            self.rows.append(detail)
            actions.append('double-click')
            return dict(status='delivered_unverified', diagnostic='visual_double_click_dispatched', input_dispatched=True)

        def read_note(*args, **kwargs):
            if not memo_ready:
                raise flow.WorkflowError('target_missing')
            actions.append('read')
            return dict(status='observed', value='note', value_evidence=dict(status='observed'), input_dispatched=False)

        def close(bound, *args, **kwargs):
            if bound.hwnd == self.owner.hwnd:
                actions.append('query')
                return dict(status='delivered_unverified', input_dispatched=True, diagnostic='business_result_not_verified')
            self.assertEqual(bound.hwnd, detail['hwnd'])
            self.rows.remove(detail)
            actions.append('close')
            return dict(status='delivered_unverified', diagnostic='business_result_not_verified', input_dispatched=True)

        for module, name, options in (
                (flow, 'bind', dict(side_effect=lambda exe,title,hwnd=None: self.owner if title=='Search' else binding(detail))),
                (flow.app.visibility, 'restore_for_action', dict(return_value=False)),
                (flow.app.bridge.user32, 'IsIconic', dict(return_value=False)),
                (flow.app.bridge.user32, 'IsWindowVisible', dict(side_effect=lambda hwnd: hwnd in [r['hwnd'] for r in self.rows])),
                (flow.input_policy, 'interactive_desktop_blocked', dict(return_value=None)),
                (flow, 'resolve_step', dict(return_value=(element, {}, None))),
                (flow, 'observe_result_row', dict(side_effect=observe)), (result_cell, 'execute_row', dict(side_effect=open_row)),
                (window_context, 'window_cookie', dict(side_effect=lambda bridge,hwnd,**kw: cookies[hwnd])),
                (flow, 'lookup_observation', dict(side_effect=read_note)), (flow.live,'execute',dict(side_effect=close))):
            self.enterContext(patch.object(module, name, **options))

        opened = run('--allow-physical', '--only', '2')
        self.assertIn('next_action', opened, opened)
        args = opened['next_action']['args']
        self.assertEqual(run('--only', '3')['next_action']['args'], args)
        resumed = run(*args)
        self.assertEqual(resumed['status'], 'verified', resumed)
        self.assertEqual(resumed['run_id'], opened['run_id'])
        self.assertEqual([r['number'] for r in resumed['steps']], [3,4])
        self.assertEqual(resumed['steps'][1]['receipt']['diagnostic'], 'recorded_window_closed')
        self.assertFalse(resumed['prior_steps_replayed'])
        self.assertEqual(run(*args)['diagnostic'], 'detail_resume_expired')
        self.assertEqual(actions, ['double-click', 'read', 'close'])

        reopened = run('--allow-physical', '--only', '2')
        cookies[111] += 1  # The process and numeric HWND were reused for a replacement window.
        stale = run(*reopened['next_action']['args'])
        self.assertEqual(stale['diagnostic'], 'detail_window_lifetime_changed', stale)
        self.assertFalse(stale['input_dispatched'])
        self.assertEqual(actions, ['double-click', 'read', 'close', 'double-click'])

        self.rows.clear()
        memo_ready = False
        paused = run('--allow-physical', '--new-attempt')
        self.assertEqual(paused.get('stopped_at'), 3, paused)
        self.assertTrue(paused['input_dispatched'])
        memo_ready = True
        resumed = run(*paused['next_action']['args'])
        self.assertEqual([r['number'] for r in resumed['steps']], [3,4])
        self.assertEqual(actions[-4:], ['query', 'double-click', 'read', 'close'])
        self.assertEqual(resumed['run_id'], paused['run_id'])

    def test_same_title_without_owner_cannot_satisfy_the_opener(self):
        routes = window_context.WindowRoutes(self.task['steps'], self.owner, self.task['window'])
        self.rows.append(self.detail(110))
        routes.before(5, self.owner)
        routes.after(5, dict(status='delivered_unverified', input_dispatched=True))
        self.rows.append(self.detail(111))
        scope = self.task['steps'][5]['window_context']
        with self.assertRaisesRegex(window_context.ContextError, 'recorded_window_not_open'):
            routes.resolve(scope)
        self.owned.add((111, self.owner.hwnd))
        self.assertEqual(routes.resolve(scope).hwnd, 111)
        self.owned.clear()
        with self.assertRaises(window_context.ContextError):
            routes.resolve(scope)

    def test_public_run_reads_and_closes_only_owned_detail_and_keeps_query_receipt(self):
        other, detail = self.detail(110), self.detail(111)
        self.rows.append(other)
        self.owned.add((detail['hwnd'], self.owner.hwnd))
        observed = []

        def send(bound, uia, element, node, action, value, **kwargs):
            observed.append((bound.hwnd, action))
            if action == 'fill':
                return dict(status='verified', input_dispatched=True)
            if bound.hwnd == detail['hwnd']:
                self.rows.remove(detail)
            return dict(status='delivered_unverified', input_dispatched=True, diagnostic='business_result_not_verified')

        def open_row(bound, uia, table, expected, guard, **kwargs):
            guard()
            self.assertEqual(expected, 'B')
            self.rows.append(detail)
            return dict(status='delivered_unverified', physical_input=True,
                        input_dispatched=True, diagnostic='visual_double_click_dispatched')

        def read_memo(bound, uia, step, expected, guard, **kwargs):
            guard()
            observed.append((bound.hwnd, step['action']))
            return dict(status='observed', value='Memo B', value_evidence=dict(status='observed'), input_dispatched=False)

        uia = self.enterContext(patch.object(flow.app.bridge, 'UIA')).return_value.__enter__.return_value
        uia.pid.return_value = self.owner.pid
        element = Mock()
        for module, attribute, options in (
                (flow, 'bind', dict(return_value=self.owner)),
                (flow.input_policy, 'interactive_desktop_blocked', dict(return_value=None)),
                (flow.app.bridge.user32, 'IsIconic', dict(return_value=False)),
                (flow.app.bridge.user32, 'IsWindowVisible', dict(side_effect=lambda hwnd: hwnd in {r['hwnd'] for r in self.rows})),
                (flow.app.visibility, 'restore_for_action', dict(return_value=False)),
                (flow, 'result_surface', dict(return_value=element)),
                (flow, 'resolve_step', dict(return_value=(element, {}, None))),
                (result_cell, 'find', dict(return_value=element)),
                (result_cell, 'execute', dict(side_effect=open_row)),
                (flow.live, 'execute', dict(side_effect=send)),
                (flow, 'lookup_observation', dict(side_effect=read_memo))):
            self.enterContext(patch.object(module, attribute, **options))
        report = flow.run(self.task, {'ID':'B'})
        self.assertEqual(report['status'], 'verified', report)
        self.assertEqual(report['outcome'], 'current_result_detail_read_and_closed')
        self.assertEqual(report['query_freshness'], 'not_asserted')
        rows = [item['receipt'] for item in report['steps']]
        self.assertEqual(rows[1]['status'], 'delivered_unverified')
        self.assertEqual(rows[5]['value'], 'Memo B')
        self.assertEqual(rows[-1]['diagnostic'], 'recorded_window_closed')
        self.assertEqual(self.rows, [other])
        self.assertEqual(observed, [(101, 'fill'), (101, 'invoke'), (111, 'read-value'), (111, 'invoke')])
