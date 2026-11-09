import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import desktop
import py_helper


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
            (root / 'engine/bridge-manifest.json').write_text(json.dumps({'version': '0.1.5', 'files': hashes}))
            with patch.object(desktop, 'ROOT', root), patch.object(desktop, 'BRIDGE', root / 'scripts/py_helper.py'):
                self.assertEqual(desktop._bridge_script(), root / 'scripts/py_helper.py')
                (root / 'scripts/win_bridge.py').write_text('# altered\n', encoding='utf-8')
                with self.assertRaisesRegex(desktop.EngineError, 'bridge_source_hash_mismatch'):
                    desktop._bridge_script()


if __name__ == '__main__':
    unittest.main()
