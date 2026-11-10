import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop
import py_helper
import win_bridge as bridge


class PythonProtocolTests(unittest.TestCase):
    def test_alternate_stream_file_is_rejected_before_io(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'local_absolute_file_required'):
                py_helper.write_response(Path(folder) / 'response.json:hidden', {'ok': True})
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_duplicate_and_nonfinite_request_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'request.json'
            for content in ('{"pid": 1, "pid": 2}', '{"x": NaN}', '[]'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    py_helper.read_request(path)

    def test_invalid_response_does_not_replace_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'response.json'
            path.write_text('original', encoding='utf-8')
            with self.assertRaises(ValueError):
                py_helper.write_response(path, {'value': float('inf')})
            self.assertEqual(path.read_text(), 'original')
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['response.json'])

    def test_bad_request_never_calls_backend_or_claims_input(self):
        with tempfile.TemporaryDirectory() as folder:
            request, response = Path(folder) / 'request.json', Path(folder) / 'response.json'
            request.write_text('{"pid":1,"pid":2}', encoding='utf-8')
            with patch.object(py_helper, 'dispatch') as dispatch, contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = py_helper.main(['point', '--request', str(request), '--response', str(response)])
            self.assertEqual(code, 2)
            dispatch.assert_not_called()
            result = json.loads(response.read_text())
            self.assertFalse(result['input_dispatched'])
            self.assertEqual(stdout.getvalue(), '')

    def test_unknown_point_failure_is_not_retried_or_echoed(self):
        with tempfile.TemporaryDirectory() as folder:
            request, response = Path(folder) / 'request.json', Path(folder) / 'response.json'
            request.write_text('{}', encoding='utf-8')
            with patch.object(py_helper, 'dispatch', side_effect=RuntimeError('private app error')) as dispatch:
                code = py_helper.main(['point', '--request', str(request), '--response', str(response)])
            self.assertEqual(code, 2)
            dispatch.assert_called_once()
            result = json.loads(response.read_text())
            self.assertIsNone(result['input_dispatched'])
            self.assertEqual(result['retry'], 'unsafe')
            self.assertNotIn('private', json.dumps(result))

    def test_pinned_source_modified_after_packaging_blocks_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'engine').mkdir()
            (root / 'scripts').mkdir()
            hashes = {}
            for name in desktop.BRIDGE_SOURCES:
                (root / name).write_text('# transparent source\n', encoding='utf-8')
                hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
            (root / 'engine/bridge-manifest.json').write_text(json.dumps({'version': '0.1.6', 'files': hashes}))
            with patch.object(desktop, 'ROOT', root), patch.object(desktop, 'BRIDGE', root / 'scripts/py_helper.py'):
                self.assertEqual(desktop._bridge_script(), root / 'scripts/py_helper.py')
                (root / 'scripts/win_bridge.py').write_text('# altered\n', encoding='utf-8')
                with self.assertRaisesRegex(desktop.EngineError, 'bridge_source_hash_mismatch'):
                    desktop._bridge_script()


class VisibleOptionTests(unittest.TestCase):
    """Exercise the helper including resolution, expansion, hit testing and one-click boundary."""
    def setUp(self):
        self.binding = Mock(spec=bridge.Binding)
        self.binding.hwnd, self.binding.pid = 11, 22
        self.binding.contains_window.side_effect = lambda hwnd: hwnd in (11, 12)
        self.binding.recording_candidate.side_effect = self.binding.contains_window.side_effect
        self.root, self.combo, self.editor, self.item = [Mock(spec=bridge.Element) for _ in range(4)]
        self.open = False
        self.root.property.return_value = 50032
        self.combo.property.side_effect = lambda k: {30003: 50003, 30010: True, 30022: True, 30028: True, 30070: int(self.open)}.get(k)
        self.item.property.side_effect = lambda k: {30003: 50007, 30010: True, 30022: False}.get(k)
        def get_pattern(slot, types, pattern_id, iid, pointer):
            self.assertEqual((slot, pattern_id), (14, 10005))
            bridge.C.cast(pointer, bridge.C.POINTER(bridge.C.c_void_p))[0] = 1
        self.combo.call.side_effect = get_pattern
        self.locator = {'automation_id': 'Opaque', 'role': 'combobox'}
        self.request = {'pid': 22, 'hwnd': 11, 'exe_name': 'Fixture.exe', 'process_started_utc_ticks': '123',
                        'locator': self.locator, 'value': 'N'}
        self.combo_rect = {'x': 30, 'y': 10, 'width': 40, 'height': 20}
        self.item_rect = {'x': 30, 'y': 40, 'width': 40, 'height': 20}
        self.node = {'name': 'Provider row 1', 'value': 'N', 'role': 'listitem', 'enabled': True, 'secure': False,
                     'offscreen': False, 'bounds_valid': True, 'bounds': self.item_rect}
        self.choice = {'element': self.item, 'root': self.root, 'hit_window': 12, 'node': self.node}
        self.uia = MagicMock(spec=bridge.UIA)
        self.uia.from_handle.return_value = self.root
        self.uia.pid.return_value = 22
        self.uia.children.side_effect = lambda element: [self.combo] if element is self.root else []
        self.uia.secure.return_value = False
        self.uia.locator.return_value = self.locator
        self.uia.bounds.side_effect = lambda element: self.combo_rect if element is self.combo else self.item_rect
        self.uia.from_point.side_effect = lambda x, y: self.editor if y == 20 else self.item
        self.uia.point_descendant.side_effect = lambda element, *args: element
        self.uia._option_hit.side_effect = lambda element, *args: element
        self.uia.within.side_effect = lambda a, b: (a is self.editor and b is self.combo) or a is b
        self.uia.same.side_effect = lambda a, b: a is b
        self.uia.combo_options.side_effect = lambda *args, **kwargs: [self.choice] if self.open else []
        self.uia.node.return_value = self.node
        self.pattern = Mock()
        self.pattern.call.side_effect = lambda *args: setattr(self, 'open', True)
        self.send = Mock(return_value={'ok': True, 'input_dispatched': True, 'status': 'executed_unverified'})
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for attr, replacement in [('Binding', Mock(return_value=self.binding)), ('UIA', MagicMock()),
                 ('_Com', Mock(return_value=self.pattern)), ('_send_point', self.send),
                 ('window_from_point', lambda x, y: 11 if y == 20 else 12), ('foreground', lambda: 11)]:
            self.stack.enter_context(patch.object(bridge, attr, replacement))
        bridge.UIA.return_value.__enter__.return_value = self.uia
        windows = self.stack.enter_context(patch.object(bridge, 'user32', create=True))
        windows.GetAsyncKeyState.return_value = 0
        windows.IsIconic.return_value = 0
        windows.IsWindowVisible.return_value = 1

    def test_offscreen_combo_with_real_visible_editor_and_value_bearing_option(self):
        result = bridge.combo_option(self.request)
        self.assertTrue(result['popup_expanded'])
        self.assertTrue(result['option_dispatched'])
        self.assertEqual(result['status'], 'executed_unverified')
        self.pattern.call.assert_called_once_with(3, ())
        self.send.assert_called_once_with(50, 50, 'click')

    def test_hidden_ambiguous_and_replaced_popup_do_not_click(self):
        hidden = dict(self.choice, node=dict(self.node, offscreen=True))
        for choices, diagnostic in [([[hidden]], 'combo_option_not_actionable'),
                                   ([[self.choice, self.choice]], 'combo_option_ambiguous'),
                                   ([[self.choice], []], 'combo_option_changed')]:
            with self.subTest(diagnostic=diagnostic):
                self.open = False
                self.uia.combo_options.side_effect = [[], *choices]
                result = bridge.combo_option(self.request)
                self.assertEqual(result['diagnostic'], diagnostic)
                self.assertTrue(result['input_dispatched'])  # Only Expand happened.
                self.assertIs(result['option_dispatched'], False)
                self.send.assert_not_called()

    def test_open_popup_uses_runtime_value_and_rechecks_identity(self):
        self.open = True
        self.node['value'] = self.request['value'] = 'Alternate'
        self.uia.node.return_value = dict(self.node, value='Changed')
        result = bridge.combo_option(self.request)
        self.assertEqual(result['diagnostic'], 'combo_option_changed')
        self.assertFalse(result['input_dispatched'])
        self.pattern.call.assert_not_called()
        self.send.assert_not_called()

    def test_expansion_exception_and_partial_click_preserve_uncertainty(self):
        self.pattern.call.side_effect = RuntimeError('provider failed')
        result = bridge.combo_option(self.request)
        self.assertIsNone(result['input_dispatched'])
        self.assertIsNone(result['popup_expanded'])
        self.assertIs(result['option_dispatched'], False)
        self.pattern.call.assert_called_once()
        self.send.assert_not_called()
        self.open = True
        self.send.return_value = {'ok': False, 'status': 'uncertain', 'input_dispatched': None, 'diagnostic': 'partial_input'}
        result = bridge.combo_option(self.request)
        self.assertIsNone(result['option_dispatched'])
        self.assertEqual(result['diagnostic'], 'partial_input')
        self.send.assert_called_once()

    def test_wrong_editor_or_foreign_overlay_never_get_input(self):
        self.uia.from_point.side_effect = lambda x, y: self.root
        result = bridge.combo_option(self.request)
        self.assertEqual(result['diagnostic'], 'point_element_mismatch')
        self.pattern.call.assert_not_called()
        self.open = True
        # A same-process detached HWND may be considered during recording,
        # but its UIA relationship alone does not authorize physical input.
        self.binding.recording_candidate.side_effect = None
        self.binding.recording_candidate.return_value = True
        with patch.object(bridge, 'window_from_point', return_value=99):
            result = bridge.combo_option(self.request)
        self.assertEqual(result['diagnostic'], 'point_occluded')
        self.send.assert_not_called()

    def test_incomplete_popup_enumeration_does_not_open_or_click(self):
        self.uia.combo_options.side_effect = bridge.BridgeError('combo_options_incomplete')
        result = bridge.combo_option(self.request)
        self.assertFalse(result['input_dispatched'])
        self.assertIs(result['option_dispatched'], False)
        self.assertEqual(result['diagnostic'], 'combo_options_incomplete')
        self.assertTrue(self.uia.combo_options.call_args.kwargs['strict'])
        self.pattern.call.assert_not_called()
        self.send.assert_not_called()

    def test_actionable_child_that_replaces_option_center_never_receives_click(self):
        self.open = True
        nested_button = Mock(spec=bridge.Element)
        self.uia.from_point.side_effect = lambda x, y: nested_button
        self.uia.within.side_effect = lambda a, b: a is nested_button and b is self.item
        result = bridge.combo_option(self.request)
        self.assertEqual(result['diagnostic'], 'point_element_mismatch')
        self.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
