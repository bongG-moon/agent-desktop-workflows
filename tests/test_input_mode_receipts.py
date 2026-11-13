"""Input mechanisms remain visible without treating a planned path as delivery."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_context_menu as menu_fixture
import test_row_action as row_fixture
import test_select_live_action as live_fixture
import test_result_evidence as result_fixture


class InputModeReceiptTests(unittest.TestCase):
    def fixture(self, kind):
        instance = kind()
        self.addCleanup(instance.doCleanups)
        instance.setUp()
        return instance

    def assert_pointer(self, receipt, dispatched):
        self.assertEqual('physical_pointer', receipt['input_mode'])
        self.assertIs(receipt['physical_input'], True)
        self.assertIs(receipt['input_dispatched'], dispatched)

    def test_menu_records_pointer_path_and_each_dispatch(self):
        fixture = self.fixture(menu_fixture.MenuTests)
        receipt, send = fixture.run_path()
        self.assert_pointer(receipt, True)
        self.assertEqual(['right-click', 'click', 'click'],
                         [call.args[-1] for call in send.call_args_list])

    def test_blocked_menu_exposes_pointer_path_without_claiming_delivery(self):
        fixture = self.fixture(menu_fixture.MenuTests)
        receipt, send = fixture.run_path(guard=Mock(side_effect=menu_fixture.cm.win.BridgeError('target_changed')))
        self.assert_pointer(receipt, False)
        self.assertEqual('blocked', receipt['status'])
        send.assert_not_called()

    def test_partial_menu_keeps_unknown_delivery_separate_from_mechanism(self):
        fixture = self.fixture(menu_fixture.MenuTests)
        receipt, send = fixture.run_path(receipts=[{'ok': False, 'input_dispatched': None}])
        self.assert_pointer(receipt, None)
        self.assertEqual('uncertain', receipt['status'])
        self.assertEqual(1, send.call_count)

    def test_row_completion_without_click_still_identifies_row_input_path(self):
        fixture = self.fixture(row_fixture.RowActionTests)
        receipt = fixture.execute(row_fixture.sample('after'), row_fixture.sample('after'))
        self.assert_pointer(receipt, False)
        self.assertEqual('verified', receipt['status'])
        fixture.send.assert_not_called()

    def test_row_delivery_identifies_pointer_and_preserves_readback(self):
        fixture = self.fixture(row_fixture.RowActionTests)
        before, after = row_fixture.sample('before'), row_fixture.sample('after')
        receipt = fixture.execute(before, before, after, after)
        self.assert_pointer(receipt, True)
        self.assertEqual('matched', receipt['effect_verification'])
        fixture.send.assert_called_once_with(40, 60, 'click')

    def test_uia_write_stays_accessibility_for_success_and_unknown_delivery(self):
        fixture = self.fixture(live_fixture.LiveActionTests)
        fixture.after = lambda item, operation, value: fixture.combo.props.update({30045: value})
        receipt = fixture.run_action()
        self.assertEqual(('accessibility', False, True, 'verified'),
                         (receipt['input_mode'], receipt['physical_input'],
                          receipt['input_dispatched'], receipt['status']))
        fixture.after = Mock(side_effect=OSError('provider failed after entry'))
        receipt = fixture.run_action(value='next')
        self.assertEqual(('accessibility', False, None, 'uncertain'),
                         (receipt['input_mode'], receipt['physical_input'],
                          receipt['input_dispatched'], receipt['status']))
        self.assertEqual(['SetValue', 'SetValue'], [call[1] for call in fixture.calls])

    def test_result_query_failure_retains_accessibility_and_never_opens_menu(self):
        fixture = self.fixture(result_fixture.ResultTests)
        query = dict(status='uncertain', input_dispatched=None, diagnostic='provider_failed',
                     input_mode='accessibility', physical_input=False)
        result = result_fixture.result
        with patch.object(result, '_matches', return_value=False), \
                patch.object(result, '_query', return_value=query), \
                patch.object(result.context_menu, 'execute') as menu:
            receipt = result.execute(None, fixture.spec, lambda: None, 'App')
        self.assertEqual(('accessibility', False, None),
                         (receipt['input_mode'], receipt['physical_input'], receipt['input_dispatched']))
        menu.assert_not_called()

    def test_result_menu_propagates_pointer_without_claiming_file_verification(self):
        fixture = self.fixture(result_fixture.ResultTests)
        menu = dict(status='delivered_unverified', input_dispatched=True, leaf_dispatched=True,
                    menu_path_completed=True, input_mode='physical_pointer', physical_input=True)
        result = result_fixture.result
        with patch.object(result.context_menu, 'execute', return_value=menu):
            receipt = result.execute(None, dict(fixture.spec, trigger=None, ready=None), lambda: None, 'App')
        self.assert_pointer(receipt, True)
        self.assertNotEqual('matched', receipt['effect_verification'])
        self.assertEqual('menu_delivered_no_output_condition', receipt['diagnostic'])


if __name__ == '__main__':
    unittest.main()
