"""Release boundaries: transparent bridge source and one unchanged upstream EXE."""
import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workflow_build', ROOT / 'tools/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def contents(distribution='source'):
    result = {name: b'public source\n' for name in build.FILES}
    result[build.BRIDGE_MANIFEST] = json.dumps({'version': build.VERSION,
        'files': {name: build.digest(result[name]) for name in build.BRIDGE_FILES}}).encode()
    binary = b'pinned upstream synthetic binary'
    result['UPSTREAM.json'] = json.dumps({'binary_sha256': build.digest(binary)}).encode()
    if distribution == 'windows-x64':
        result['bin/agent-desktop.exe'] = binary
    return result


class PackagingTests(unittest.TestCase):
    def test_windows_archive_contains_only_pinned_upstream_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'windows.zip'
            build.archive(archive, contents('windows-x64'), 'windows-x64')
            with zipfile.ZipFile(archive) as reader:
                binaries = [name for name in reader.namelist() if Path(name).suffix.lower() in {'.exe', '.dll', '.sys', '.pyd', '.ps1'}]
                self.assertEqual(binaries, ['agent-desktop-workflows/bin/agent-desktop.exe'])
                self.assertTrue(all(not item.flag_bits & 1 for item in reader.infolist()))

    def test_source_archive_excludes_all_executables_and_build_powershell(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'source.zip'
            build.archive(archive, contents())
            with zipfile.ZipFile(archive) as reader:
                self.assertFalse(any(Path(name).suffix.lower() in {'.exe', '.dll', '.sys', '.pyd', '.ps1'} for name in reader.namelist()))
                self.assertIn('agent-desktop-workflows/scripts/recorder.py', reader.namelist())

    def test_unlisted_helper_and_private_recording_are_rejected(self):
        for name in ('bin/WorkflowNative.exe', 'tools/build_native.ps1', 'capture/private.json'):
            payload = contents('windows-x64')
            payload[name] = b'not allowed'
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'allowlist'):
                build.validate_contents(payload, 'windows-x64')

    def test_changed_bridge_source_requires_matching_manifest(self):
        payload = contents()
        payload['scripts/recorder.py'] += b'changed'
        with self.assertRaisesRegex(ValueError, 'bridge_source_manifest_stale'):
            build.validate_contents(payload, 'source')

    def test_modified_upstream_binary_is_rejected(self):
        payload = contents('windows-x64')
        payload['bin/agent-desktop.exe'] += b'modified'
        with self.assertRaisesRegex(ValueError, 'binary_pin_mismatch'):
            build.validate_contents(payload, 'windows-x64')

    def test_runtime_source_has_no_compiler_or_encoded_executable(self):
        # Test tools may compile their own synthetic fixture; installed runtime must not.
        for name in build.BRIDGE_FILES:
            path = ROOT / name
            if not path.is_file():
                self.fail('missing_runtime_source:' + name)
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            with self.subTest(name=name):
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        modules = [item.name.split('.')[0] for item in node.names] if isinstance(node, ast.Import) else [(node.module or '').split('.')[0]]
                        self.assertFalse({'clr', 'pythonnet', 'comtypes', 'win32api', 'win32com'} & set(modules))
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and not isinstance(parents.get(node), ast.Expr):
                        for token in ('add-type', 'csc.exe', 'powershell.exe', 'frombase64string', 'pyinstaller'):
                            self.assertNotIn(token, node.value.lower())
                    if isinstance(node, ast.Call):
                        function = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
                        self.assertNotIn(function, {'b64decode', 'standard_b64decode', 'urlsafe_b64decode'})


if __name__ == '__main__':
    unittest.main()
