"""Policy preflight is pure and rejects desktop-dependent paths before any input."""
from pathlib import Path
import ctypes as C
from ctypes import wintypes as W
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import input_policy as policy


class InputPolicyTests(unittest.TestCase):
    def test_accessibility_and_observation_plan_needs_no_physical_input(self):
        actions = ['set-value', 'invoke', 'select', 'fill', 'open', 'wait', 'verify-file']
        requirements = policy.profile([dict(action=action) for action in actions])
        self.assertFalse(requirements['independent_desktop'])
        self.assertFalse(requirements['physical_input_required'])
        self.assertFalse(requirements['foreground_required'])
        self.assertTrue(requirements['background_eligible'])
        self.assertTrue(requirements['minimized_window_requires_restore'])
        self.assertEqual(['accessibility'] * 5 + ['observation'] * 2,
                         [row['input_mode'] for row in requirements['steps']])
        self.assertEqual(list(range(1, 8)), [row['number'] for row in requirements['steps']])
        self.assertIsNone(policy.blocked(requirements))

    def test_mixed_plan_is_rejected_before_the_first_accessibility_step(self):
        requirements = policy.profile([dict(action='fill'), dict(action='context-menu'),
                                       dict(action='submit'), dict(action='verify-file')],
                                      numbers=[2, 4, 8, 9])
        receipt = policy.blocked(requirements)
        self.assertEqual('blocked', receipt['status'])
        self.assertEqual('background_requires_desktop_interaction', receipt['diagnostic'])
        self.assertEqual('background_only', receipt['input_policy'])
        self.assertEqual([4, 8], receipt['blocked_steps'])
        self.assertEqual([], receipt['steps'])
        self.assertIs(receipt['input_dispatched'], False)
        self.assertIs(receipt['physical_input'], False)
        self.assertTrue(requirements['physical_input_required'])
        self.assertTrue(requirements['foreground_required'])
        self.assertEqual(requirements, receipt['input_requirements'])

    def test_each_pointer_action_requires_foreground(self):
        for action in ('context-menu', 'result-menu', 'row-action'):
            with self.subTest(action=action):
                requirements = policy.profile([dict(action=action)])
                self.assertEqual('physical_pointer', requirements['steps'][0]['input_mode'])
                self.assertTrue(requirements['foreground_required'])
                self.assertTrue(requirements['physical_input_required'])
                self.assertEqual([1], policy.blocked(requirements)['blocked_steps'])

    def test_enter_is_a_keyboard_path(self):
        requirements = policy.profile([dict(action='submit')])
        self.assertEqual('physical_keyboard', requirements['steps'][0]['input_mode'])
        self.assertTrue(requirements['foreground_required'])
        self.assertTrue(requirements['physical_input_required'])
        self.assertFalse(requirements['background_eligible'])

    def test_anchored_accessibility_needs_foreground_without_pointer_input(self):
        for action in ('set-value', 'invoke', 'select', 'fill', 'open'):
            with self.subTest(action=action):
                requirements = policy.profile([dict(action=action, selector={'anchor': {'name': 'Panel'}})])
                self.assertEqual('accessibility', requirements['steps'][0]['input_mode'])
                self.assertTrue(requirements['foreground_required'])
                self.assertFalse(requirements['physical_input_required'])
                self.assertFalse(requirements['background_eligible'])
                self.assertIsNotNone(policy.blocked(requirements))

    def test_unknown_manual_and_malformed_actions_fail_closed(self):
        for step in ({'action': 'manual'}, {'action': 'teach-target'}, {'action': 'future-command'},
                     {}, {'action': None}, {'action': []}, None):
            with self.subTest(step=step):
                requirements = policy.profile([step])
                self.assertEqual('unknown', requirements['steps'][0]['input_mode'])
                self.assertFalse(requirements['background_eligible'])
                self.assertEqual([1], policy.blocked(requirements)['blocked_steps'])

    def test_number_mapping_cannot_silently_drop_steps(self):
        for numbers in ([], [1, 2], [True], [0], ['1']):
            with self.subTest(numbers=numbers), self.assertRaises(ValueError):
                policy.profile([dict(action='submit')], numbers=numbers)

    def test_profile_and_blocked_do_not_mutate_source_or_share_receipt_details(self):
        step = dict(action='submit', selector={'automation_id': 'field'}, private_value='not in output')
        requirements = policy.profile([step])
        self.assertEqual({'automation_id': 'field'}, step['selector'])
        self.assertNotIn('private_value', str(requirements))
        receipt = policy.blocked(requirements)
        receipt['input_requirements']['steps'][0]['action'] = 'changed'
        self.assertEqual('submit', requirements['steps'][0]['action'])

    def test_empty_plan_is_pure_and_requires_no_input(self):
        requirements = policy.profile([])
        self.assertEqual([], requirements['steps'])
        self.assertTrue(requirements['background_eligible'])
        self.assertFalse(requirements['physical_input_required'])
        self.assertIsNone(policy.blocked(requirements))

    def test_window_check_rejects_minimized_target_without_activation(self):
        class BridgeError(Exception):
            pass
        user = Mock()
        user.IsIconic.return_value = True
        bridge = SimpleNamespace(user32=user, BridgeError=BridgeError)
        binding = Mock(hwnd=44)
        with patch.dict(sys.modules, win_bridge=bridge):
            with self.assertRaisesRegex(BridgeError, 'background_window_minimized'):
                policy.window_ready(binding)
        binding.check.assert_called_once_with()
        self.assertEqual([('IsIconic', (44,), {})], user.mock_calls)

    def test_window_check_leaves_visible_target_unchanged(self):
        user = Mock()
        user.IsIconic.return_value = False
        bridge = SimpleNamespace(user32=user)
        binding = Mock(hwnd=51)
        with patch.dict(sys.modules, win_bridge=bridge):
            self.assertIsNone(policy.window_ready(binding))
        binding.check.assert_called_once_with()
        self.assertEqual([('IsIconic', (51,), {})], user.mock_calls)

    def test_stale_binding_stops_before_any_window_call(self):
        user = Mock()
        binding = Mock(hwnd=51)
        binding.check.side_effect = RuntimeError('stale_binding')
        with patch.dict(sys.modules, win_bridge=SimpleNamespace(user32=user)):
            with self.assertRaisesRegex(RuntimeError, 'stale_binding'):
                policy.window_ready(binding)
        self.assertEqual([], user.mock_calls)


class _GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _BridgeError(Exception):
    input_dispatched = False


class AutoFocusPolicyTests(unittest.TestCase):
    def fixture(self, *, query_result=-2147467262, query_pointer=None,
                create_result=0, fail_slot=None, readback=0, walker_pointer=404):
        events = []
        uia = SimpleNamespace(_thread=Mock(), _objects=set())
        class Com:
            def __init__(self, pointer, owner):
                self.ptr, self.uia = C.c_void_p(pointer.value), owner
                owner._objects.add(self)

            def call(self, slot, argtypes, *args):
                events.append(('call', self.ptr.value, slot))
                if slot == fail_slot:
                    raise RuntimeError('native_call_failed')
                if slot == 59:
                    self_arg = args[0]
                    if argtypes != (W.BOOL,) or self_arg is not False:
                        raise AssertionError('expected BOOL false setter')
                elif slot == 58:
                    if readback is not None:
                        C.cast(args[0], C.POINTER(W.BOOL))[0] = readback
                elif slot == 14:
                    C.cast(args[0], C.POINTER(C.c_void_p))[0] = walker_pointer
                else:
                    raise AssertionError('unexpected COM call')

            def close(self):
                events.append(('close', self.ptr.value))
                self.ptr = C.c_void_p()
        uia.client, uia.walker = Com(C.c_void_p(101), uia), Com(C.c_void_p(202), uia)
        original = (uia.client, uia.walker)
        def query(client, iid, pointer):
            self.assertIs(client, original[0])
            self.assertEqual(uuid.UUID('34723aff-0c9d-49d0-9896-7ab52df8cd8a').bytes_le, bytes(iid))
            pointer.value = query_pointer
            return query_result
        def create(clsid, outer, context, iid, pointer):
            self.assertEqual(uuid.UUID('e22ad333-b25f-460c-83d0-0581107395c9').bytes_le, C.string_at(clsid, 16))
            self.assertEqual(uuid.UUID('34723aff-0c9d-49d0-9896-7ab52df8cd8a').bytes_le, C.string_at(iid, 16))
            self.assertEqual((None, 1), (outer, context))
            if create_result == 0:
                C.cast(pointer, C.POINTER(C.c_void_p))[0] = 303
            return create_result
        bridge = SimpleNamespace(GUID=_GUID, _Com=Com, BridgeError=_BridgeError,
                                 ole32=SimpleNamespace(CoCreateInstance=Mock(side_effect=create)))
        for patcher in (patch.dict(sys.modules, win_bridge=bridge),
                        patch.object(policy, '_query_interface', side_effect=query)):
            patcher.start()
            self.addCleanup(patcher.stop)
        return uia, bridge, original, events

    def test_existing_modern_client_is_configured_and_query_reference_released(self):
        uia, bridge, original, events = self.fixture(query_result=0, query_pointer=303)
        policy.configure_uia(uia)
        self.assertEqual(original, (uia.client, uia.walker))
        self.assertEqual([('call', 303, 59), ('call', 303, 58), ('close', 303)], events)
        bridge.ole32.CoCreateInstance.assert_not_called()

    def test_legacy_client_and_walker_replaced_only_after_false_readback(self):
        uia, bridge, original, events = self.fixture()
        policy.configure_uia(uia)
        self.assertEqual((303, 404), (uia.client.ptr.value, uia.walker.ptr.value))
        self.assertEqual((None, None), (original[0].ptr.value, original[1].ptr.value))
        self.assertEqual([('call', 303, 59), ('call', 303, 58), ('call', 303, 14),
                          ('close', 202), ('close', 101)], events)
        bridge.ole32.CoCreateInstance.assert_called_once()

    def test_other_query_errors_and_null_success_never_create_replacement(self):
        for result in (-2147467259, 0):
            with self.subTest(result=result):
                uia, bridge, original, events = self.fixture(query_result=result)
                with self.assertRaisesRegex(_BridgeError, 'background_autofocus_unavailable') as failure:
                    policy.configure_uia(uia)
                self.assertIs(failure.exception.input_dispatched, False)
                bridge.ole32.CoCreateInstance.assert_not_called()
                self.assertEqual([], events)
                self.assertEqual(original, (uia.client, uia.walker))

    def test_modern_creation_failure_is_no_input_policy_failure(self):
        uia, bridge, original, events = self.fixture(create_result=-2147467262)
        with self.assertRaisesRegex(_BridgeError, 'background_autofocus_unavailable'):
            policy.configure_uia(uia)
        self.assertEqual([], events)
        self.assertEqual((101, 202), (original[0].ptr.value, original[1].ptr.value))

    def test_setting_reading_or_walker_failure_closes_candidate_without_swapping(self):
        for slot in (59, 58, 14):
            with self.subTest(slot=slot):
                uia, bridge, original, events = self.fixture(fail_slot=slot)
                with self.assertRaisesRegex(_BridgeError, 'background_autofocus_unavailable'):
                    policy.configure_uia(uia)
                self.assertEqual(('close', 303), events[-1])
                self.assertEqual(original, (uia.client, uia.walker))
                self.assertEqual((101, 202), (original[0].ptr.value, original[1].ptr.value))

    def test_true_or_unwritten_focus_readback_cannot_enable_execution(self):
        for readback in (1, None):
            with self.subTest(readback=readback):
                uia, bridge, original, events = self.fixture(readback=readback)
                with self.assertRaisesRegex(_BridgeError, 'background_autofocus_unavailable'):
                    policy.configure_uia(uia)
                self.assertEqual([('call', 303, 59), ('call', 303, 58), ('close', 303)], events)
                self.assertEqual(original, (uia.client, uia.walker))

    def test_missing_walker_releases_new_client_and_preserves_old_one(self):
        uia, bridge, original, events = self.fixture(walker_pointer=None)
        with self.assertRaisesRegex(_BridgeError, 'background_autofocus_unavailable'):
            policy.configure_uia(uia)
        self.assertEqual(('close', 303), events[-1])
        self.assertEqual(original, (uia.client, uia.walker))

    def test_existing_targets_prohibit_replacing_their_client(self):
        uia, bridge, original, events = self.fixture()
        extra = bridge._Com(C.c_void_p(505), uia)
        with self.assertRaisesRegex(_BridgeError, 'background_autofocus_unavailable'):
            policy.configure_uia(uia)
        bridge.ole32.CoCreateInstance.assert_not_called()
        self.assertEqual([], events)
        self.assertEqual(505, extra.ptr.value)

    def test_raw_query_uses_iunknown_slot_zero_and_retains_hresult(self):
        factory = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)
        seen = []
        @factory(C.c_int32, C.c_void_p, C.POINTER(_GUID), C.POINTER(C.c_void_p))
        def query(this, iid, pointer):
            seen.append(bytes(iid.contents))
            pointer[0] = 303
            return 0
        table = (C.c_void_p * 1)(C.cast(query, C.c_void_p).value)
        instance = (C.c_void_p * 1)(C.addressof(table))
        client = SimpleNamespace(ptr=C.cast(instance, C.c_void_p), uia=SimpleNamespace(_thread=Mock()))
        pointer, iid = C.c_void_p(), _GUID.parse(policy._UIA2_IID)
        with patch.object(C, 'WINFUNCTYPE', factory, create=True):
            self.assertEqual(0, policy._query_interface(client, iid, pointer))
        self.assertEqual(303, pointer.value)
        self.assertEqual([uuid.UUID(policy._UIA2_IID).bytes_le], seen)
        client.uia._thread.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
