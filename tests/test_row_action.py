import json
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import row_action


def sample(state, point=(40, 60)):
    return {'kind': state, 'point': list(point) if state == 'before' else None, 'row_bounds': [0, 40, 100, 50]}


class RowActionTests(unittest.TestCase):
    def setUp(self):
        self.vision = SimpleNamespace(observe_row=Mock(), confirm_point=Mock(), confirm_pixels=Mock(), validate_spec=Mock())
        self.enter(patch.dict(sys.modules, row_vision=self.vision))
        self.enter(patch.object(row_action, 'foreground'))
        self.enter(patch.object(app.bridge, '_combo_surface'))
        self.enter(patch.object(app.bridge, 'root_window', return_value=440))
        self.enter(patch.object(app.bridge, 'window_from_point', return_value=441))
        self.send = self.enter(patch.object(app.bridge, '_send_point', return_value={'ok': True, 'input_dispatched': True}))
        self.enter(patch.object(row_action.time, 'sleep'))
        self.guard = Mock()
        self.spec = {'test': 'private pixels'}

    def enter(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def execute(self, *observations, timeout=3):
        self.vision.observe_row.side_effect = observations
        return row_action.execute(SimpleNamespace(hwnd=440), self.spec, self.guard, timeout=timeout)

    def test_waits_for_two_same_row_confirmations_after_one_dispatch(self):
        first, final = sample('before'), sample('after')
        result = self.execute(first, first, final, first, final, final)
        self.assertEqual('verified', result['status'])
        self.assertEqual('matched', result['effect_verification'])
        self.assertTrue(result['input_dispatched'])
        self.assertEqual(6, self.vision.observe_row.call_count)
        self.send.assert_called_once_with(40, 60, 'click')

    def test_already_running_does_not_click(self):
        result = self.execute(sample('after'), sample('after'))
        self.assertEqual(('verified', False, 'after'),
                         (result['status'], result['input_dispatched'], result['initial_state']))
        self.send.assert_not_called()

    def test_ambiguous_and_moving_targets_have_no_click(self):
        for observations in [(sample('ambiguous'),), (sample('before'), sample('before', (43, 63)))]:
            with self.subTest(observations=observations):
                result = self.execute(*observations)
                self.assertEqual(('blocked', False), (result['status'], result['input_dispatched']))
        self.send.assert_not_called()

    def test_foreground_loss_or_replaced_row_never_repeats_input(self):
        first = sample('before')
        result = self.execute(first, first, app.bridge.BridgeError('business_window_changed'))
        self.assertEqual(('delivered_unverified', True, 'business_window_changed'),
                         (result['status'], result['input_dispatched'], result['diagnostic']))
        self.send.assert_called_once()

    def test_partial_input_stops_without_verification_or_retry(self):
        self.send.return_value = {'ok': False, 'input_dispatched': None, 'diagnostic': 'partial_input'}
        result = self.execute(sample('before'), sample('before'))
        self.assertEqual(('uncertain', None), (result['status'], result['input_dispatched']))
        self.assertEqual(2, self.vision.observe_row.call_count)
        self.send.assert_called_once()

    def test_changed_pixels_after_point_guard_block_dispatch(self):
        self.vision.confirm_pixels.side_effect = app.bridge.BridgeError('row_changed_before_action')
        with self.assertRaisesRegex(app.bridge.BridgeError, 'row_changed_before_action'):
            self.execute(sample('before'), sample('before'))
        self.send.assert_not_called()

    def test_verification_accepts_a_transient_relayout_without_redispatch(self):
        result = self.execute(sample('before'), sample('before'), app.bridge.BridgeError('row_layout_changed'),
                              sample('after'), sample('after'))
        self.assertEqual('verified', result['status'])
        self.send.assert_called_once()

    def test_plan_persists_evidence_but_user_receipts_contain_no_pixels(self):
        steps = [{'action': 'row-action', 'alias': f'기기 {n}', 'label': '시작', 'row': self.spec} for n in range(3)]
        plan = app.tasks.from_recording('시작', 'App.exe', 'App', steps)
        invoke = Mock(return_value={'status': 'verified', 'input_dispatched': True, 'effect_verification': 'matched'})
        receipt = app.tasks.replay(plan, 'App.exe', 'App', {}, invoke)
        self.assertEqual(('verified', 3), (receipt['status'], invoke.call_count))
        self.assertEqual(self.spec, app.tasks.get_step(plan, '기기 2')['row'])
        self.assertNotIn('private pixels', json.dumps(app.tasks.summary(plan)))

    def test_row_task_rejects_malformed_evidence(self):
        self.vision.validate_spec.side_effect = app.bridge.BridgeError('invalid_row_spec')
        with self.assertRaisesRegex(app.tasks.TaskError, 'invalid_row_spec'):
            app.tasks.from_recording('Test', 'App.exe', 'App', [{'action': 'row-action', 'label': 'click', 'row': {}}])

    def test_terminal_keeps_before_sample_on_transient_after_failure_without_activation(self):
        draft = {'role': 'row', 'scope': ['list'], 'key': 'row identity', 'before': True}
        self.vision.teach_before = Mock(return_value=draft)
        self.vision.teach_after = Mock(side_effect=[app.bridge.BridgeError('row_layout_changed'), self.spec])
        self.vision.transfer_after = Mock()
        bind = SimpleNamespace(hwnd=440, check=Mock(), exe='App.exe')
        args = SimpleNamespace(task='행 시연', row=[['첫 행', '5', '5']], key_box=[], button_box=[], state_box=[])
        with (patch.object(app.tasks, 'check_new_task'), patch.object(app.tasks, 'save_task') as save,
              patch.object(app.bridge, 'window_text', return_value='Demo'), patch('sys.stdin', io.StringIO('\n\nSAVE\n')),
              patch('sys.stderr', io.StringIO())):
            out = row_action.teach_rows(bind, 'Demo', args, None)
        self.assertEqual('recorded', out['status'])
        self.assertEqual(2, self.vision.teach_after.call_count)
        self.assertEqual(1, self.vision.teach_before.call_count)
        save.assert_called_once()
        row_action.foreground.assert_not_called()


class ForegroundTests(unittest.TestCase):
    def test_foreground_change_while_attached_always_detaches_before_input(self):
        owner = SimpleNamespace(hwnd=440, pid=33, exe='App.exe', created='start', check=Mock())
        attach = Mock(return_value=True)
        user = Mock()
        user.GetWindowThreadProcessId.return_value = 123
        def signature(dll, name, *args):
            return {'AttachThreadInput': attach, 'GetCurrentThreadId': lambda: 432}[name]
        guard = [None, None, app.bridge.BridgeError('foreground_or_input_changed')]
        with (patch.object(app.bridge, 'user32', user),
              patch.object(app.bridge, 'root_window', return_value=702), patch.object(app.bridge, 'foreground', return_value=702),
              patch.object(row_action, '_input_tick', return_value=100),
              patch.object(app.bridge, 'activate', return_value={'foreground_verified': False}),
              patch.object(row_action, '_idle_foreground', side_effect=guard), patch.object(app.bridge, '_sig', signature)):
            with self.assertRaisesRegex(app.bridge.BridgeError, 'foreground_or_input_changed'):
                row_action.foreground(owner)
        self.assertEqual(attach.call_args_list, [call(432, 123, True), call(432, 123, False)])
        user.SetForegroundWindow.assert_not_called()


if __name__ == '__main__':
    unittest.main()
