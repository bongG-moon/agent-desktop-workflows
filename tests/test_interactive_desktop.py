"""Locked/secure desktops reject physical plans without blocking pure UIA."""
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import input_policy as policy
import python_workflow as flow


class InteractiveDesktopTests(unittest.TestCase):
    def native(self, *, input_name='Default', thread_name='Default', opened=7, thread=11, info_ok=True):
        def information(handle, index, buffer, size, needed):
            self.assertEqual(index, 2)
            self.assertEqual(size, 256 * C.sizeof(C.c_wchar))
            C.cast(needed, C.POINTER(W.DWORD))[0] = 16
            buffer.value = input_name if handle == 7 else thread_name
            return info_ok
        calls = dict(OpenInputDesktop=Mock(return_value=opened), CloseDesktop=Mock(return_value=True),
                     GetThreadDesktop=Mock(return_value=thread), GetCurrentThreadId=Mock(return_value=23),
                     GetUserObjectInformationW=Mock(side_effect=information))
        bridge = SimpleNamespace(user32=object(), kernel32=object(),
                                 _sig=lambda _dll, name, *_types: calls[name])
        return bridge, calls

    def test_same_desktop_only_reads_identity_and_closes_owned_handle(self):
        bridge, calls = self.native(input_name='Default', thread_name='DEFAULT')
        self.assertTrue(policy._interactive_desktop(bridge))
        calls['OpenInputDesktop'].assert_called_once_with(0, False, 1)
        calls['GetThreadDesktop'].assert_called_once_with(23)
        calls['CloseDesktop'].assert_called_once_with(7)

    def test_lock_or_secure_desktop_mismatch_is_unavailable(self):
        bridge, calls = self.native(input_name='Winlogon')
        self.assertFalse(policy._interactive_desktop(bridge))
        calls['CloseDesktop'].assert_called_once_with(7)

    def test_denied_input_desktop_does_not_close_borrowed_thread_desktop(self):
        bridge, calls = self.native(opened=0)
        evidence = {}
        self.assertFalse(policy._interactive_desktop(bridge, evidence=evidence))
        self.assertEqual(evidence['reason'], 'input_desktop_open_failed')
        calls['GetThreadDesktop'].assert_not_called()
        calls['CloseDesktop'].assert_not_called()

    def test_failed_identity_or_missing_thread_always_releases_opened_desktop(self):
        for options in ({'info_ok': False}, {'input_name': ''}, {'thread': 0}):
            with self.subTest(options=options):
                bridge, calls = self.native(**options)
                self.assertFalse(policy._interactive_desktop(bridge))
                calls['CloseDesktop'].assert_called_once_with(7)

    def test_provider_exception_still_releases_opened_desktop(self):
        bridge, calls = self.native()
        calls['GetUserObjectInformationW'].side_effect = OSError('unavailable')
        self.assertFalse(policy._interactive_desktop(bridge))
        calls['CloseDesktop'].assert_called_once_with(7)

    def test_pure_uia_never_queries_or_requires_interactive_desktop(self):
        requirements = policy.profile([dict(action=a) for a in ('set-value', 'invoke', 'read-value', 'verify-value')])
        with patch.object(policy, '_interactive_desktop', side_effect=AssertionError('not needed')):
            self.assertIsNone(policy.interactive_desktop_blocked(requirements))

    def test_mixed_plan_reports_exact_physical_steps_with_no_input(self):
        requirements = policy.profile([dict(action=a) for a in ('set-value', 'double-click-result', 'submit')], [1, 4, 6])
        with patch.object(policy, '_interactive_desktop', return_value=False) as check:
            result = policy.interactive_desktop_blocked(requirements)
        check.assert_called_once()
        self.assertEqual(result['diagnostic'], 'no_interactive_desktop')
        self.assertEqual(result['host_action'], 'check_interactive_session_before_retry')
        self.assertEqual(result['blocked_steps'], [4, 6])
        self.assertIs(result['input_dispatched'], False)
        self.assertEqual(result['steps'], [])

    def test_workflow_stops_before_any_binding_or_input(self):
        recipe = dict(schema=1, name='Example', exe='Example.exe', window='Example', steps=[
            dict(action='set-value', selector=dict(automation_id='Field', role='textfield'), value='A'),
            dict(action='submit', selector=dict(automation_id='Field', role='textfield'))])
        with patch.object(policy, '_interactive_desktop', return_value=False), \
                patch.object(flow, 'bind', side_effect=AssertionError('must not bind')):
            result = flow.run(recipe)
        self.assertEqual(result['diagnostic'], 'no_interactive_desktop')
        self.assertIs(result['input_dispatched'], False)

    def test_required_values_and_background_policy_precede_desktop_query(self):
        recipe = dict(schema=1, name='Example', exe='Example.exe', window='Example', steps=[
            dict(action='set-value', selector=dict(automation_id='Field', role='textfield'), parameter='Value'),
            dict(action='submit', selector=dict(automation_id='Field', role='textfield'))])
        with patch.object(policy, '_interactive_desktop', side_effect=AssertionError('premature query')):
            self.assertEqual(flow.run(recipe)['status'], 'needs_input')
            self.assertEqual(flow.run(recipe, background_only=True)['diagnostic'], 'background_requires_desktop_interaction')


if __name__ == '__main__':
    unittest.main()
