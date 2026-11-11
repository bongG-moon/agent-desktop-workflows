"""Versioned core dependency checks, separate from editable provider compatibility."""
import json
from hashlib import sha256
from pathlib import Path
import re


class RuntimeIntegrityError(RuntimeError):
    def __init__(self, code, filename):
        self.code, self.filename = code, filename
        super().__init__(f'{code}: {filename}. Reinstall the complete Desktop Select ZIP; do not mix versions.')


def checked_path(filename):
    here = Path(__file__).resolve()
    manifest = here.parent.parent / 'runtime-manifest.json'
    try:
        data = json.loads(manifest.read_text(encoding='utf8'))
        if data.get('schema') != 1 or set(data.get('files', {})) != {'win_bridge.py', 'recorder.py'}:
            raise ValueError()
        expected = data['files'][filename]
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise RuntimeIntegrityError('runtime_manifest_invalid', 'runtime-manifest.json') from error
    path = here.with_name(filename)
    if (not path.exists() and here.parents[2].name == 'skills'
            and (here.parents[3] / 'engine/bridge-manifest.json').is_file()):
        path = here.parents[3] / 'scripts' / filename
    if not path.is_file():
        raise RuntimeIntegrityError('runtime_file_missing', filename)
    if sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != expected:
        raise RuntimeIntegrityError('runtime_file_mismatch', filename)
    return path
