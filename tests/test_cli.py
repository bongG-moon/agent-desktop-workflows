"""Read-only CLI projections refresh explicit outputs without replacing state."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import cli
from storage import Store


def workflow():
    return {'schema_version': 1, 'id': 'a' * 32, 'revision': 1, 'name': 'Synthetic CLI test',
            'target': {'exe_name': 'Fixture.exe', 'title': 'Synthetic fixture', 'class_name': 'Fixture'},
            'parameters': {}, 'steps': [{'id': '1' * 32, 'label': 'Manual', 'action': 'manual',
                'target': {'kind': 'manual', 'locator': {}}, 'expect': {'kind': 'manual'},
                'recognition': {'target': 'manual', 'action': 'manual', 'result': 'manual'}}]}


class CliOutputTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / '.build/test-cli'
        folder.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.store = Store(self.folder / 'store')
        self.saved = self.store.save(workflow())

    def call(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            code = cli.main([*args, '--store', str(self.store.root)])
        return code, json.loads(stream.getvalue())

    def test_list_and_show_replace_requested_output_with_current_revision(self):
        for command, extra in [('list', []), ('show', ['--workflow', self.saved['id']])]:
            with self.subTest(command=command):
                path = self.folder / (command + '.json')
                code, result = self.call(command, *extra, '--output', str(path))
                self.assertEqual((code, result['status']), (0, 'written'))
                latest = self.store.load(self.saved['id'])
                revised = copy.deepcopy(latest)
                revised['name'] = 'Refreshed ' + command
                revised = self.store.save(revised, expected_revision=latest['revision'])
                code, result = self.call(command, *extra, '--output', str(path))
                self.assertEqual((code, result['status']), (0, 'written'))
                value = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual((value[0] if command == 'list' else value)['revision'], revised['revision'])

    def test_windows_and_status_refresh_same_output(self):
        for command in ('windows', 'status'):
            with self.subTest(command=command):
                path = self.folder / (command + '.json')
                extra = ['--run', 'b' * 32] if command == 'status' else []
                name = 'cli.Store.load_run' if command == 'status' else 'cli.desktop.inventory'
                with patch(name, side_effect=[{'version': 1}, {'version': 2}]):
                    for _ in range(2):
                        code, result = self.call(command, *extra, '--output', str(path))
                        self.assertEqual((code, result['status']), (0, 'written'))
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['version'], 2)

    def test_source_and_active_store_outputs_are_rejected_without_change(self):
        paths = [ROOT / 'UPSTREAM.json', ROOT / 'scripts/cli.py',
                 self.store.root / 'workflows' / self.saved['id'] / '000001.json']
        for path in paths:
            with self.subTest(path=path.name):
                before = path.read_bytes()
                code, result = self.call('list', '--output', str(path))
                self.assertEqual(code, 2)
                self.assertIn(result['diagnostic'], {'output_inside_workflow_store', 'output_overwrites_product_file'})
                self.assertEqual(path.read_bytes(), before)

    def test_other_store_and_profile_paths_are_rejected(self):
        other = Store(self.folder / 'other-store')
        profile = self.folder / '.claude'
        profile.mkdir()
        for path, reason in [(other.root / 'result.json', 'output_inside_workflow_store'),
                             (profile / 'settings.json', 'output_reserved_location')]:
            with self.subTest(reason=reason):
                code, result = self.call('list', '--output', str(path))
                self.assertEqual((code, result['diagnostic']), (2, reason))
                self.assertFalse(path.exists())

    def test_failed_refresh_preserves_prior_output(self):
        path = self.folder / 'list.json'
        path.write_text('original', encoding='utf-8')
        with patch('desktop.os.replace', side_effect=OSError('synthetic write failure')):
            code, result = self.call('list', '--output', str(path))
        self.assertEqual(code, 2)
        self.assertEqual(path.read_text(encoding='utf-8'), 'original')
        self.assertNotIn('synthetic', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
