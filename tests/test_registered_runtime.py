"""Registration removes snapshots, not target checks or truthful delivery receipts."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import registered_runtime as runtime


def profile():
    return dict(schema=1, name='Query export', exe='Fixture.exe', window='Business', steps=[
        dict(action='set-value', selector=dict(automation_id='status', role='combobox'), value='N'),
        dict(action='set-value', selector=dict(automation_id='family', role='combobox'), parameter='Family'),
        dict(action='invoke', selector=dict(automation_id='query', role='button')),
        dict(action='wait', seconds=0),
        dict(action='invoke', selector=dict(automation_id='excel', role='button'))])


class Item:
    def __init__(self, identifier):
        self.identifier, self.value, self.enabled = identifier, '', True

    def property(self, key):
        return self.enabled if key == 30010 else None


class RegisteredRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.items = {k: Item(k) for k in ('status', 'family', 'query', 'excel')}
        self.calls, self.sessions = [], 0
        self.binding = Mock(pid=17)
        self.uia = Mock()
        self.uia.__enter__ = Mock(return_value=self.uia)
        self.uia.__exit__ = Mock(return_value=False)
        self.uia.actions = lambda item: ['SetValue'] if item.identifier in ('status', 'family') else ['Click']
        self.uia.same = lambda a, b: a is b
        self.patches = []
        def replace(obj, name, value):
            p = patch.object(obj, name, value)
            self.patches.append(p)
            p.start()
        replace(runtime.app.bridge, 'set_dpi_awareness', Mock())
        replace(runtime.app.bridge, 'Binding', Mock(return_value=self.binding))
        replace(runtime.app, 'windows', Mock(return_value=[]))
        replace(runtime.app, 'pick_window', Mock(return_value={}))
        replace(runtime.app, 'guard', Mock())
        replace(runtime.app, 'restore_for_action', Mock(return_value=False))
        replace(runtime.app, 'observe', Mock(side_effect=AssertionError('full tree scan')))
        replace(runtime.app, 'visible', Mock(return_value=True))
        replace(runtime.live, '_combo_visible', Mock(return_value=True))
        replace(runtime, 'READBACK_DELAY', 0)
        replace(runtime.native_targets, 'resolve', Mock(side_effect=lambda b, u, s, **kw: self.items[s['automation_id']]))
        replace(runtime.app.bridge, 'UIA', Mock(return_value=self.uia))
        replace(runtime.live, 'combo_readback', Mock(side_effect=lambda b,u,e: {
            'value': e.value, 'value_evidence': {'status': 'observed' if e.value else 'unavailable',
                                             'source': 'corroborated' if e.value else 'none'}}))
        replace(runtime.live, '_call', Mock(side_effect=self.dispatch))
        replace(runtime.live, '_option_rows', Mock(side_effect=AssertionError('option inventory')))
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def dispatch(self, item, method, value, authorize, dispatching):
        authorize()
        dispatching(method)
        self.calls.append((item.identifier, method, value))
        if method == 'SetValue':
            item.value = value

    def run_profile(self, overrides=None, **kw):
        return runtime.run(profile(), {'Family':'Hybrid'} if overrides is None else overrides, **kw)

    def test_one_session_four_dispatches_and_repeat_variable(self):
        first = self.run_profile()
        self.assertEqual(first['status'], 'completed_unverified')
        self.assertEqual([c[1] for c in self.calls], ['SetValue','SetValue','Invoke','Invoke'])
        self.assertEqual(runtime.app.bridge.UIA.call_count, 1)
        self.assertEqual(first['metrics']['full_tree_snapshots'], 0)
        second = self.run_profile({'Family':'DRAM'})
        self.assertEqual(second['status'], 'completed_unverified')
        self.assertEqual(self.items['family'].value, 'DRAM')
        self.assertEqual(self.calls[4:], [('family','SetValue','DRAM'),('query','Invoke',None),('excel','Invoke',None)])

    def test_missing_variable_has_no_window_lookup_or_restore(self):
        result = self.run_profile({})
        self.assertEqual(result['status'], 'needs_input')
        runtime.app.windows.assert_not_called()
        runtime.app.restore_for_action.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_read_only_does_not_need_variable_or_restore_or_wait(self):
        with patch.object(runtime, '_wait', side_effect=AssertionError('readonly wait')):
            result = self.run_profile({}, read_only=True)
        self.assertEqual(result['status'], 'observed')
        self.assertFalse(result['input_dispatched'])
        runtime.app.restore_for_action.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_preflight_missing_or_duplicate_blocks_before_first_write(self):
        for diagnostic in ('registered_target_missing', 'registered_target_ambiguous'):
            runtime.native_targets.resolve.side_effect = runtime.app.bridge.BridgeError(diagnostic)
            result = self.run_profile()
            self.assertEqual(result['diagnostic'], diagnostic)
            self.assertFalse(result['input_dispatched'])
            self.assertEqual(self.calls, [])

    def test_write_outcome_uncertain_does_not_retry_or_invoke(self):
        def uncertain(item, method, value, authorize, dispatching):
            self.dispatch(item, method, value, authorize, dispatching)
            raise runtime.app.bridge.BridgeError('pattern_delivery_unknown')
        runtime.live._call.side_effect = uncertain
        result = self.run_profile()
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['input_dispatched'])
        self.assertEqual(len(self.calls), 1)

    def test_native_oserror_after_dispatch_preserves_uncertainty_in_worker(self):
        def uncertain(item, method, value, authorize, dispatching):
            self.dispatch(item, method, value, authorize, dispatching)
            raise OSError('native exception after input')
        runtime.live._call.side_effect = uncertain
        with patch.object(runtime.profiles, 'load', return_value=profile()), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            code = runtime.main('workflow.json', ['--_worker', '--set', 'Family=Hybrid'])
        receipt = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(receipt['status'], 'uncertain')
        self.assertIsNone(receipt['input_dispatched'])
        self.assertEqual(receipt['diagnostic'], 'provider_error')
        self.assertEqual(len(self.calls), 1)

    def test_oserror_during_readback_keeps_confirmed_dispatch(self):
        def read(binding, uia, item):
            if self.calls:
                raise OSError('read failed after confirmed write')
            return {'value':None, 'value_evidence':{'status':'unavailable'}}
        runtime.live.combo_readback.side_effect = read
        result = self.run_profile()
        self.assertEqual(result['status'], 'delivered_unverified')
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(len(self.calls), 1)

    def test_unavailable_or_conflicting_readback_stops_after_one_write(self):
        for evidence in ('unavailable', 'conflict'):
            self.calls.clear()
            runtime.live.combo_readback.side_effect = lambda *a: {'value':None, 'value_evidence':{'status':evidence}}
            result = self.run_profile()
            self.assertEqual(result['status'], 'delivered_unverified')
            self.assertEqual(result['diagnostic'], 'value_not_verified')
            self.assertEqual(len(self.calls), 1)

    def test_disabled_query_never_calls_excel(self):
        self.items['query'].enabled = False
        result = self.run_profile()
        self.assertEqual(result['diagnostic'], 'target_disabled')
        self.assertEqual([c[0] for c in self.calls], ['status','family'])
        self.assertTrue(result['input_dispatched'])

    def test_invisible_target_does_not_send(self):
        runtime.live._combo_visible.return_value = False
        result = self.run_profile()
        self.assertEqual(result['diagnostic'], 'target_offscreen')
        self.assertFalse(result['input_dispatched'])

    def test_target_changes_in_final_authorize_prevents_input(self):
        count = 0
        def resolve(binding, uia, selector, **kw):
            nonlocal count
            count += 1
            return Item('replacement') if count == 7 else self.items[selector['automation_id']]
        runtime.native_targets.resolve.side_effect = resolve
        result = self.run_profile()
        self.assertEqual(result['diagnostic'], 'registered_target_changed')
        self.assertFalse(result['input_dispatched'])

    def test_step_defaults_preserved_and_override_wins(self):
        plan = profile()
        plan['steps'] = [dict(action='set-value',selector=plan['steps'][0]['selector'],parameter='Code',default='N'),
                         dict(action='set-value',selector=plan['steps'][1]['selector'],parameter='Code',default='Hybrid')]
        self.assertEqual(runtime.run(plan, {})['status'], 'verified')
        self.assertEqual([c[2] for c in self.calls], ['N','Hybrid'])
        self.calls.clear()
        runtime.run(plan, {'Code':'W'})
        self.assertEqual([c[2] for c in self.calls], ['W','W'])

    def test_bad_child_receipt_after_launch_is_uncertain_not_zero_input(self):
        with patch.object(runtime.profiles, 'load', return_value=profile()), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(stdout='broken', returncode=1)), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            code = runtime.main('workflow.json', ['--set', 'Family=Hybrid'])
        receipt = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertIsNone(receipt['input_dispatched'])
        self.assertEqual(receipt['status'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
