"""Packaged dependency diagnostics stay explicit; edited compatibility is independent."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import runtime_integrity as integrity


class IntegrityTests(unittest.TestCase):
    def test_manifest_checks_core_and_reports_exact_missing_or_changed_file(self):
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp)
            scripts = skill/'scripts'
            scripts.mkdir()
            source=b'reviewed core\n'
            hashes={name:hashlib.sha256(source).hexdigest() for name in ('win_bridge.py','recorder.py')}
            (skill/'runtime-manifest.json').write_text(json.dumps(dict(schema=1,files=hashes)),encoding='utf8')
            with patch.object(integrity,'__file__',str(scripts/'runtime_integrity.py')):
                with self.assertRaisesRegex(integrity.RuntimeIntegrityError,'runtime_file_missing: win_bridge.py'):
                    integrity.checked_path('win_bridge.py')
                core=scripts/'win_bridge.py'
                core.write_bytes(source.replace(b'\n',b'\r\n'))
                self.assertEqual(core, integrity.checked_path('win_bridge.py'))
                (scripts/'provider_values.py').write_text('customized compatibility',encoding='utf8')
                self.assertEqual(core, integrity.checked_path('win_bridge.py'))
                core.write_bytes(source+b'wrong version')
                with self.assertRaisesRegex(integrity.RuntimeIntegrityError,'runtime_file_mismatch: win_bridge.py'):
                    integrity.checked_path('win_bridge.py')
                (skill/'runtime-manifest.json').write_text('{}',encoding='utf8')
                with self.assertRaisesRegex(integrity.RuntimeIntegrityError,'runtime_manifest_invalid'):
                    integrity.checked_path('win_bridge.py')

    def test_source_core_still_matches_manifest(self):
        for filename in ('win_bridge.py','recorder.py'):
            self.assertTrue(integrity.checked_path(filename).is_file())
