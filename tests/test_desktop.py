import copy
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop
import cli


def combo_postcondition_failure():
    # Reduced from the real blank-parent UIA provider's agent-desktop receipt.
    disposition = {'delivery': 'delivered_unverified', 'retry': 'unsafe'}
    return {'version': '2.6', 'ok': False, 'command': 'set-value', 'error': {
        'code': 'ACTION_FAILED', 'disposition': dict(disposition), 'details': {
            'kind': 'post_action_verification',
            'after_action': {'action': 'set-value', 'disposition': dict(disposition), 'steps': [
                {'label': 'ValuePattern.SetValue', 'mechanism': 'semantic_api', 'outcome': 'succeeded', 'verified': False}]},
            'post_state': {'enabled': True, 'offscreen': False, 'role': 'combobox', 'states': ['focused'], 'value': ''}}}}


class TargetResolutionTests(unittest.TestCase):
    def test_combo_postcondition_only_error_requires_all_delivery_evidence(self):
        node = {'path': '0', 'role': 'combobox', 'name': 'Status', 'native_id': 'stable'}
        after = {'ok': True, 'data': {'tree': dict(node, value=''), 'complete': True}}
        args = dict(raw=combo_postcondition_failure(), node=node, after=after, verification={'node': node},
                    action='set-value', value='N', dispatched=True, delivery='delivered_unverified')
        self.assertTrue(desktop.interact._combo_postcondition_only_failure(**args))
        mutations = [
            (('command',), 'select'), (('ok',), True), (('error', 'code'), 'PROVIDER_FAILED'),
            (('error', 'details', 'kind'), 'action_execution'),
            (('error', 'details', 'after_action', 'action'), 'click'),
            (('error', 'disposition', 'delivery'), 'possibly_dispatched'),
            (('error', 'details', 'after_action', 'disposition', 'delivery'), 'not_delivered'),
            (('error', 'details', 'after_action', 'disposition', 'retry'), 'safe'),
            (('error', 'details', 'after_action', 'steps', 0, 'label'), 'Select'),
            (('error', 'details', 'after_action', 'steps', 0, 'mechanism'), 'physical_synthetic'),
            (('error', 'details', 'after_action', 'steps', 0, 'outcome'), 'attempted'),
            (('error', 'details', 'after_action', 'steps', 0, 'verified'), True),
            (('error', 'details', 'after_action', 'steps', 0, 'verified'), 0),
            (('error', 'details', 'after_action', 'steps'), []),
            (('error', 'details', 'post_state', 'value'), None),
            (('error', 'details', 'post_state', 'role'), 'textfield')]
        for path, replacement in mutations:
            with self.subTest(path=path):
                changed = copy.deepcopy(args)
                parent = changed['raw']
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = replacement
                self.assertFalse(desktop.interact._combo_postcondition_only_failure(**changed))
        for change in [dict(value=''), dict(action='type'), dict(dispatched=None), dict(delivery='possibly_dispatched'),
                       dict(node=dict(node, role='textfield')), dict(verification={}),
                       dict(verification={'node': dict(node, native_id='different')}),
                       dict(after={'ok': True, 'data': {'tree': dict(node, value='')}}),
                       dict(after={'ok': True, 'data': {'tree': dict(node, value='', truncated=True)}})]:
            with self.subTest(change=change):
                self.assertFalse(desktop.interact._combo_postcondition_only_failure(**dict(args, **change)))
        changed = copy.deepcopy(args)
        changed['raw']['error']['details']['after_action']['steps'] *= 2
        self.assertFalse(desktop.interact._combo_postcondition_only_failure(**changed))
        del changed['raw']['error']['details']['after_action']
        self.assertFalse(desktop.interact._combo_postcondition_only_failure(**changed))

    def test_real_combo_postcondition_receipt_is_delivered_unverified_not_uncertain(self):
        args = SimpleNamespace(session='a'*32, ref='@snew:e1', action='set-value', value='N', headed=False)
        node = {'path': '0', 'role': 'combobox', 'name': 'Status', 'native_id': 'stable'}
        after = {'ok': True, 'data': {'tree': dict(node, value=''), 'complete': True}}
        for valid in (True, False):
            raw = combo_postcondition_failure()
            if not valid:
                raw['error']['details']['after_action']['steps'][0]['outcome'] = 'attempted'
            saved = {}
            with self.subTest(valid=valid), \
                 patch.object(desktop.interact, 'validate_local_directory', return_value=Path('a'*32)), \
                 patch.object(desktop.interact, '_load_session', return_value=({'created_at': time.time()}, {}, node)), \
                 patch.object(desktop.interact, '_call', side_effect=[{'ok': True, 'data': {'ref': args.ref, 'value': []}}, raw]) as call, \
                 patch.object(desktop.interact, '_same_identity'), patch.object(desktop.interact.observe, 'verify_binary'), \
                 patch.object(desktop.interact, '_environment'), patch.object(desktop.interact, 'snapshot', return_value=after), \
                 patch.object(desktop.interact, '_write', side_effect=lambda folder, name, data: saved.update({name: copy.deepcopy(data)})):
                result = desktop.interact.run_act(args)
            self.assertIs(result['input_dispatched'], True)
            self.assertEqual(result['status'], 'executed_unverified' if valid else 'uncertain')
            self.assertEqual(result['effect_verification'], 'unavailable')
            self.assertEqual(saved['action.json'], raw)
            self.assertEqual(call.call_count, 2)
            if valid:
                self.assertEqual(result['diagnostic'], 'combo_parent_value_unavailable')

    def test_legacy_blank_combo_write_is_blocked_before_dispatch(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        node = dict(path='0', name='Status', role='combobox', ref='@snew:e1', value='')
        for action in ('set-value', 'type', 'select'):
            with self.subTest(action=action):
                step = {'action': action, 'target': {'kind': 'element', 'locator': {'name': 'Status'}}}
                with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
                     patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'a'*32}), \
                     patch.object(desktop, 'read_json', return_value={'nodes': [node]}), \
                     patch.object(desktop.interact, 'run_act') as act:
                    result = adapter.execute(step, '')
                self.assertEqual(result['diagnostic'], 'empty_combo_value_requires_review')
                self.assertIs(result['input_dispatched'], False)
                act.assert_not_called()

    def test_combo_check_requires_current_nonempty_selection_evidence(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        node = dict(path='0', name='Status', role='combobox', value='N')
        step = {'target': {'locator': {'name': 'Status'}}, 'expect': {'kind': 'value', 'value': 'N'}}
        for evidence, actual, wanted, matched in [
                (None, 'N', 'N', None),
                ({'status': 'unavailable', 'source': 'none'}, '', '', None),
                ({'status': 'conflict', 'source': 'none'}, 'N', 'N', None),
                ({'status': 'observed', 'source': 'selection'}, 'N', 'N', True),
                ({'status': 'observed', 'source': 'editor'}, 'W', 'N', False),
                ({'status': 'observed', 'source': 'parent'}, '', '', None),
                ({'status': 'observed', 'source': 'none'}, 'N', 'N', None),
                ({'status': 'observed', 'source': 'corroborated'}, 'N', '', None)]:
            with self.subTest(evidence=evidence, actual=actual, wanted=wanted):
                observed = dict(node, value=actual, value_evidence=evidence)
                check_step = dict(step, expect={'kind': 'value', 'value': wanted})
                with patch.object(adapter, '_window', return_value=({}, {'nodes': [observed], 'complete': True})):
                    self.assertIs(adapter.check(check_step)['matched'], matched)
        for complete in (None, False):
            partial = dict(node, value_evidence={'status': 'observed', 'source': 'selection'})
            with patch.object(adapter, '_window', return_value=({}, {'nodes': [partial], 'complete': complete})):
                self.assertIsNone(adapter.check(step)['matched'])
        text = dict(node, role='textfield', value='')
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [text]})):
            self.assertTrue(adapter.check(dict(step, expect={'kind': 'value', 'value': ''}))['matched'])

    def test_combo_semantic_readback_overrides_raw_parent_mismatch_without_second_input(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'set-value', 'target': {'kind': 'element', 'locator': {'name': 'Status', 'role': 'combobox'}}}
        engine = {'status': 'verification_failed', 'input_dispatched': True, 'effect_verification': 'mismatch',
                  'receipt': 'b'*32, 'delivery': 'delivered_unverified', 'after_snapshot_available': True}
        native = {'complete': True, 'nodes': [dict(path='0', name='Status', role='combobox', value='N',
                                value_evidence={'status': 'observed', 'source': 'selection'})]}
        with patch.object(adapter, '_window', return_value=(adapter.binding, native)), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'a'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(path='0', name='Status', role='combobox', ref='@snew:e1', value='')]}), \
             patch.object(desktop.interact, 'run_act', return_value=engine) as act:
            result = adapter.execute(step, 'N')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['engine_status'], 'verification_failed')
        self.assertEqual(result['receipt'], engine['receipt'])
        self.assertEqual(engine['status'], 'verification_failed')
        self.assertTrue(result['value_readback']['matched'])
        self.assertEqual(act.call_count, 1)

    def test_combo_engine_success_does_not_override_ignored_write_or_unobserved_selection(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        step = {'action': 'set-value', 'target': {'kind': 'element', 'locator': {'name': 'Status', 'role': 'combobox'}}}
        engine = {'status': 'completed', 'input_dispatched': True, 'effect_verification': 'matched', 'receipt': 'a'*32}
        for actual, evidence, outcome in [
                ('W', {'status': 'observed', 'source': 'editor'}, 'verification_failed'),
                (None, {'status': 'unavailable', 'source': 'none'}, 'executed_unverified'),
                (None, {'status': 'conflict', 'source': 'none'}, 'executed_unverified')]:
            node = dict(path='0', name='Status', role='combobox', value=actual, value_evidence=evidence)
            with self.subTest(evidence=evidence), patch.object(adapter, '_window', return_value=({}, {'nodes': [node], 'complete': True})):
                result = adapter._combo_readback(engine, step, 'N')
                self.assertEqual(result['status'], outcome)
                self.assertEqual(result['engine_status'], 'completed')
        with patch.object(adapter, 'check', side_effect=KeyboardInterrupt):
            result = adapter._combo_readback(engine, step, 'N')
        self.assertIs(result['input_dispatched'], True)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['diagnostic'], 'combo_readback_interrupted')

    def test_uncertain_combo_delivery_is_not_promoted_by_already_matching_value(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        step = {'action': 'set-value', 'target': {'kind': 'element', 'locator': {'name': 'Status', 'role': 'combobox'}}}
        for status, dispatched in [('uncertain', None), ('uncertain', True), ('interrupted', True), ('not_delivered', False)]:
            engine = {'status': status, 'input_dispatched': dispatched, 'effect_verification': 'matched',
                      'delivery': 'delivered_unverified', 'after_snapshot_available': True}
            with self.subTest(status=status, dispatched=dispatched), \
                 patch.object(adapter, 'check', return_value={'matched': True}) as check:
                self.assertIs(adapter._combo_readback(engine, step, 'N'), engine)
                check.assert_not_called()

    def test_engine_raw_blank_combo_never_verifies_selection_or_clear(self):
        before = {'path': '0', 'name': 'Status', 'role': 'combobox', 'native_id': 'stable'}
        raw = {'ok': True, 'data': {'tree': dict(before, value='')}}
        for action, value in [('set-value', ''), ('set-value', 'N'), ('clear', None)]:
            with self.subTest(action=action, value=value):
                self.assertEqual(desktop.interact.verify_effect(before, raw, action, value)['status'], 'unavailable')
        text = dict(before, role='textfield')
        raw['data']['tree'] = dict(text, value='')
        self.assertEqual(desktop.interact.verify_effect(text, raw, 'clear', None)['status'], 'matched')

    def test_engine_fresh_get_blank_combo_is_still_unknown(self):
        node = {'path': '0', 'name': 'Status', 'role': 'combobox', 'ref': '@snew:e1', 'value': None}
        verification = {'status': 'unavailable', 'node': node}
        raw = {'ok': True, 'data': {'ref': '@snew:e1', 'property': 'value', 'value': ''}}
        with patch.object(desktop.interact, '_read', return_value=({}, 'hash')), \
             patch.object(desktop.interact, '_node_and_map'), patch.object(desktop.interact, '_same_identity'), \
             patch.object(desktop.interact, '_call', return_value=raw), patch.object(desktop.interact, '_write'):
            result = desktop.interact.read_missing_value(verification, {}, {}, Path('run'), 'set-value', '')
        self.assertEqual(result['status'], 'unavailable')

    def test_engine_direct_empty_combo_action_is_blocked_without_input(self):
        args = SimpleNamespace(session='a'*32, ref='@snew:e1', action='set-value', value='', headed=False)
        node = {'role': 'combobox'}
        with patch.object(desktop.interact, 'validate_local_directory', return_value=Path('a'*32)), \
             patch.object(desktop.interact, '_load_session', return_value=({}, {}, node)), \
             patch.object(desktop.interact, '_call') as call, patch.object(desktop.interact, '_write'):
            result = desktop.interact.run_act(args)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['diagnostic'], 'empty_combo_value_requires_review')
        self.assertIs(result['input_dispatched'], False)
        call.assert_not_called()

    def test_uia_recorded_list_and_item_match_engine_roles_with_parent_identity(self):
        nodes = [dict(path='0', role='window', name='Main'),
                 dict(path='0/0', parent_path='0', role='listbox', name='Results', native_id={'kind': 'automation_id', 'value': 'ResultsList'}),
                 dict(path='0/0/0', parent_path='0/0', role='option', name='Row B', ref='@snew:e2'),
                 dict(path='0/1', parent_path='0', role='listbox', name='Other'),
                 dict(path='0/1/0', parent_path='0/1', role='option', name='Row B', ref='@snew:e3')]
        locator = {'role': 'listitem', 'name': 'Row B', 'ancestors': [
            {'role': 'list', 'automation_id': 'ResultsList', 'name': 'Results'}]}
        self.assertEqual(desktop.resolve(nodes, locator)['ref'], '@snew:e2')

    def test_direct_list_select_blocks_offscreen_or_empty_current_bounds_before_dispatch(self):
        args = SimpleNamespace(session='a'*32, ref='@snew:e1', action='select', value='N', headed=False)
        def response(prop, value):
            return {'ok': True, 'data': {'ref': args.ref, 'property': prop, 'value': value}}
        for live in [[response('states', ['offscreen'])],
                     [response('states', []), response('bounds', {'width': 0, 'height': 12})]]:
            with self.subTest(live=live), \
                 patch.object(desktop.interact, 'validate_local_directory', return_value=Path('a'*32)), \
                 patch.object(desktop.interact, '_load_session', return_value=({}, {}, {'role': 'option'})), \
                 patch.object(desktop.interact, '_write'), patch.object(desktop.interact, '_call', side_effect=live) as call:
                result = desktop.interact.run_act(args)
            self.assertEqual(result['diagnostic'], 'list_option_not_visible')
            self.assertIs(result['input_dispatched'], False)
            self.assertTrue(all(entry.args[0][0] == 'get' for entry in call.call_args_list))

    def test_visible_replay_uses_runtime_parameter_and_independent_readback(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.lifetime, adapter.run_dir = {'pid': 8, 'hwnd': 19, 'exe_name': 'Test.exe'}, '23', Path('run')
        node = {'role': 'combobox', 'automation_id': 'Choice', 'secure': False, 'enabled': True, 'offscreen': True}
        step = {'action': 'set-value', 'target': {'kind': 'element', 'locator': {'automation_id': 'Choice', 'role': 'combobox'}},
                'combo_replay': 'visible_option', 'headed': True,
                'selection': {'source': 'option_click', 'value': 'Old', 'result': 'matched', 'observed_value': 'Old'}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {'complete': True, 'nodes': [node]})), \
             patch.object(desktop, '_native') as native, patch.object(desktop.interact, 'run_act') as act, \
             patch.object(adapter, 'check', return_value={'matched': None, 'diagnostic': 'combo_value_unavailable'}) as check:
            native.return_value = {'ok': True, 'status': 'executed_unverified', 'input_dispatched': True, 'option_dispatched': True}
            result = adapter.execute(step, 'New')
            self.assertEqual(native.call_args.args[0], 'combo-option')
            self.assertEqual(native.call_args.args[1]['value'], 'New')
            self.assertEqual(result['status'], 'executed_unverified')
            self.assertEqual(result['effect_verification'], 'unavailable')
            check.assert_called_once()
            check.reset_mock()
            native.return_value = {'ok': False, 'status': 'blocked', 'input_dispatched': True, 'option_dispatched': False}
            self.assertEqual(adapter.execute(step, 'New'), native.return_value)
            self.assertEqual(native.call_count, 2)
            check.assert_not_called()
            act.assert_not_called()
            native.reset_mock()
            self.assertFalse(adapter.execute(dict(step, headed=False), 'New')['input_dispatched'])
            self.assertFalse(adapter.execute(step, '')['input_dispatched'])
            native.assert_not_called()

    def test_nested_owned_menus_use_unique_current_contents(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}
        adapter.run_dir, adapter.lifetime = Path('run'), 'same'
        main = {'process_started_utc_ticks': 'same', 'title': 'Main', 'class_name': 'MainClass'}
        menus = [dict(adapter.binding, hwnd=n, title='', class_name='Popup', owner_hwnd=22, root_owner_hwnd=22) for n in (30, 31)]
        snapshots = {22: main, 30: {'complete': True, 'nodes': [{'name': 'Export', 'role': 'menuitem'}]},
                     31: {'complete': True, 'nodes': [{'name': 'Export to Excel', 'role': 'menuitem'}]}}
        target = {'kind': 'element', 'window': {'title': '', 'class_name': 'Popup'},
                  'locator': {'name': 'Export to Excel', 'role': 'menuitem'}}
        with patch.object(desktop, 'inventory', return_value=menus), \
             patch.object(desktop, 'inspect', side_effect=lambda binding, _: snapshots[binding['hwnd']]):
            binding, observed = adapter._window({'target': target})
            self.assertEqual(binding['hwnd'], 31)
            snapshots[30] = snapshots[31]
            with self.assertRaisesRegex(desktop.EngineError, 'related_window_unproven'):
                adapter._window({'target': target})
            snapshots[30] = {'complete': False, 'nodes': []}
            with self.assertRaisesRegex(desktop.EngineError, 'related_window_unproven'):
                adapter._window({'target': target})

    def test_report_replacement_is_explicit_atomic_and_keeps_default_exclusive(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            desktop.write_json(path, {'version': 1})
            with self.assertRaises(FileExistsError):
                desktop.write_json(path, {'version': 2})
            desktop.write_json(path, {'version': 2}, replace=True)
            self.assertEqual(json.loads(path.read_text()), {'version': 2})
            with patch.object(desktop.os, 'replace', side_effect=OSError('test write failure')):
                with self.assertRaises(OSError):
                    desktop.write_json(path, {'version': 3}, replace=True)
            self.assertEqual(json.loads(path.read_text()), {'version': 2})
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['report.json'])

    def test_report_invalid_value_or_stream_path_never_changes_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            desktop.write_json(path, {'version': 1})
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                desktop.write_json(path, {'value': float('nan')}, replace=True)
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaisesRegex(desktop.EngineError, 'invalid_output_file'):
                desktop.write_json(Path(folder) / 'report.json:hidden', {}, replace=True)

    def test_report_cannot_replace_directory_or_hardlinked_file(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with self.assertRaisesRegex(desktop.EngineError, 'invalid_output_file'):
                desktop.write_json(directory, {}, replace=True)
            original, alias = directory / 'original.json', directory / 'alias.json'
            original.write_text('original', encoding='utf-8')
            desktop.os.link(original, alias)
            with self.assertRaisesRegex(desktop.EngineError, 'invalid_output_file'):
                desktop.write_json(alias, {}, replace=True)
            self.assertEqual(original.read_text(), 'original')

    def test_same_names_use_parent_context(self):
        nodes = [dict(path='0', name='Main', role='window'),
                 dict(path='0/0', parent_path='0', name='Conditions', role='group'),
                 dict(path='0/1', parent_path='0', name='Details', role='group'),
                 dict(path='0/0/0', parent_path='0/0', name='Status', role='combobox'),
                 dict(path='0/1/0', parent_path='0/1', name='Status', role='combobox')]
        with self.assertRaisesRegex(desktop.EngineError, 'ambiguous'):
            desktop.resolve(nodes, {'name': 'Status', 'role': 'combobox'})
        found = desktop.resolve(nodes, {'name': 'Status', 'role': 'combobox',
                                       'ancestors': [{'name': 'Conditions', 'role': 'group'}]})
        self.assertEqual(found['path'], '0/0/0')

    def test_label_not_combo_and_native_id_mapping(self):
        nodes = [dict(path='0', name='MES Setup', role='statictext'),
                 dict(path='1', name='MES Setup', role='combobox',
                      native_id={'kind': 'automation_id', 'value': 'SetupSelector'})]
        found = desktop.resolve(nodes, {'automation_id': 'SetupSelector', 'role': 'combobox'})
        self.assertEqual(found['path'], '1')

    def test_path_indices_and_bounds_are_not_identity(self):
        with self.assertRaisesRegex(desktop.EngineError, 'incomplete'):
            desktop.resolve([dict(path='0', role='group')], {'role': 'group', 'path': '0'})

    def test_parameter_value_data_not_command(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'set-value', 'target': {'kind': 'element', 'locator': {'name': 'Field', 'role': 'textfield'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'a'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(path='0', name='Field', role='textfield', ref='@snew:e2')]}), \
             patch.object(desktop.interact, 'run_act', return_value={'input_dispatched': True}) as act:
            adapter.execute(step, '--headed --app Other.exe')
        self.assertEqual(act.call_args.args[0].ref, '@snew:e2')
        self.assertEqual(act.call_args.args[0].value, '--headed --app Other.exe')
        self.assertFalse(act.call_args.args[0].headed)

    def test_no_ref_does_not_invoke_or_guess_root(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'right-click', 'target': {'kind': 'element', 'locator': {'name': 'Grid'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'b'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(path='0', name='Grid', role='group', ref=None)]}), \
             patch.object(desktop.interact, 'run_act') as act:
            result = adapter.execute(step)
        self.assertEqual(result['diagnostic'], 'target_has_no_action_ref')
        self.assertFalse(result['input_dispatched'])
        act.assert_not_called()

    def test_old_result_file_is_not_new_export(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        before = {'exists': True, 'size': 200, 'mtime_ns': 1}
        step = {'expect': {'kind': 'file', 'path': 'unused'}, '_prepared': {'file_before': before}}
        with patch.object(desktop, '_local_file_state', return_value=before):
            self.assertFalse(adapter.check(step)['matched'])
        with patch.object(desktop, '_local_file_state', return_value=dict(before, mtime_ns=2)):
            self.assertTrue(adapter.check(step)['matched'])
        step.pop('_prepared')
        with patch.object(desktop, '_local_file_state', return_value=before):
            self.assertIsNone(adapter.check(step)['matched'])

    def test_unchecked_must_be_explicit_not_absent(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        step = {'target': {'locator': {'name': 'Option'}}, 'expect': {'kind': 'checked', 'value': False}}
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [dict(name='Option', path='0', states=[])]})):
            self.assertIsNone(adapter.check(step)['matched'])
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [dict(name='Option', path='0', checked=False)]})):
            self.assertTrue(adapter.check(step)['matched'])

    def test_explicit_postcondition_is_separate_from_action_value(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        step = {'target': {'locator': {'name': 'Field'}}, 'expect': {'kind': 'value', 'value': 'expected'}}
        with patch.object(adapter, '_window', return_value=({}, {'nodes': [dict(name='Field', path='0', value='sent')]})):
            self.assertFalse(adapter.check(step, 'sent')['matched'])

    def test_unreviewed_region_never_activates_or_dispatches(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}
        step = {'action': 'right-click', 'target': {'kind': 'region', 'locator': {'name': 'Grid'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), patch.object(desktop, '_native') as native:
            result = adapter.execute(step)
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['diagnostic'], 'recorded_region_requires_review')
        native.assert_not_called()

    def test_cli_receipt_cannot_echo_arbitrary_business_fields(self):
        receipt = cli._safe({'status': 'private status text', 'name': 'private workflow',
                             'id': 'C:/private/path', 'error': 'private provider error',
                             'input_dispatched': True}, diagnostic='private provider error')
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['diagnostic'], 'local_operation_failed')
        self.assertNotIn('private', json.dumps(receipt))

    def test_unsupported_select_is_known_unsent_and_repairable(self):
        adapter = object.__new__(desktop.DesktopAdapter)
        adapter.binding, adapter.run_dir = {'pid': 11, 'hwnd': 22, 'exe_name': 'App.exe'}, Path('run')
        step = {'action': 'select', 'target': {'kind': 'element', 'locator': {'name': 'Setup'}}}
        with patch.object(adapter, '_window', return_value=(adapter.binding, {})), \
             patch.object(desktop.interact, 'run_inspect', return_value={'status': 'ready', 'receipt': 'b'*32}), \
             patch.object(desktop, 'read_json', return_value={'nodes': [dict(name='Setup', ref='@snew:e1', available_actions=['SetValue'])]}), \
             patch.object(desktop.interact, 'run_act') as act:
            result = adapter.execute(step, 'N')
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['diagnostic'], 'selection_action_not_supported')
        act.assert_not_called()


if __name__ == '__main__':
    unittest.main()
