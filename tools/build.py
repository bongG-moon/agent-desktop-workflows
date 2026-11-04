"""Offline allowlisted source/Windows packaging with integrity verification."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.0'
FILES = '''.gitignore
.gitattributes
.github/workflows/ci.yml
LICENSE
NOTICE.txt
THIRD_PARTY_NOTICES.txt
UPSTREAM.json
README.md
SKILL.md
Start.cmd
사용가이드.html
references/usage.md
references/validation.md
scripts/cli.py
scripts/desktop.py
scripts/storage.py
scripts/workflows.py
scripts/ui.py
engine/observe.py
engine/interact.py
engine/local_core.py
engine/recognition.py
native/WorkflowNative.cs
native/PROTOCOL.md
tools/build.py
tools/build_native.ps1
tools/smoke_native.py
tools/smoke_workflows.py
tests/test_desktop.py
tests/test_workflows.py
tests/fixtures/ActionFixture.cs
tests/fixtures/NativeFixture.cs'''.splitlines()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_files():
    result = {}
    for name in FILES:
        path = ROOT / name
        if not path.is_file() or path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 1024:
            raise ValueError('missing_or_redirected_source:' + name)
        data = path.read_bytes()
        text = data.decode('utf-8-sig')
        patterns = [r'gh[pousr]_[A-Za-z0-9]{25,}', r'github_pat_[A-Za-z0-9_]{30,}',
                    r'sk-[A-Za-z0-9_-]{25,}', r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                    r'(?i)C:[\\/]Users[\\/](?!Public[\\/])[^\\/\s]+[\\/]']
        if any(re.search(pattern, text) for pattern in patterns):
            raise ValueError('publication_scan_failed:' + name)
        result[name] = data
    return result


def archive(path, contents):
    manifest = {'product': 'Agent Desktop Workflows', 'version': VERSION,
                'files': {name: digest(data) for name, data in sorted(contents.items())}}
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in sorted(contents.items()):
            entry = zipfile.ZipInfo('agent-desktop-workflows/' + name, (2026, 11, 4, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(entry, data)
        z.writestr('agent-desktop-workflows/PACKAGE-MANIFEST.json', json.dumps(manifest, indent=2))
    verify(path)


def verify(path):
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise ValueError('corrupt_archive')
        prefix = 'agent-desktop-workflows/'
        manifest = json.loads(z.read(prefix + 'PACKAGE-MANIFEST.json'))
        if len(z.namelist()) != len(set(z.namelist())):
            raise ValueError('duplicate_member')
        if set(z.namelist()) != {prefix + name for name in manifest['files']} | {prefix + 'PACKAGE-MANIFEST.json'}:
            raise ValueError('unexpected_member')
        for name, expected in manifest['files'].items():
            if name.startswith('/') or '..' in Path(name).parts or digest(z.read(prefix + name)) != expected:
                raise ValueError('manifest_mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.build/release')
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
        print(json.dumps({'verified': True}))
        return
    sources = source_files()
    args.output.mkdir(parents=True, exist_ok=True)
    base = 'agent-desktop-workflows-' + VERSION
    source = args.output / (base + '-source.zip')
    archive(source, sources)
    assets = [source]
    if not args.source_only:
        upstream = json.loads(sources['UPSTREAM.json'])
        binary = (ROOT / 'bin/agent-desktop.exe').read_bytes()
        native = (ROOT / 'bin/WorkflowNative.exe').read_bytes()
        native_manifest_data = (ROOT / 'bin/native-manifest.json').read_bytes()
        native_manifest = json.loads(native_manifest_data.decode('utf-8-sig'))
        if digest(binary) != upstream['binary_sha256'] or digest(native) != native_manifest['sha256']:
            raise ValueError('binary_pin_mismatch')
        if digest(sources['native/WorkflowNative.cs']) != native_manifest['source_sha256']:
            raise ValueError('native_source_manifest_stale')
        windows = args.output / (base + '-windows-x64.zip')
        archive(windows, dict(sources, **{'bin/agent-desktop.exe': binary,
            'bin/WorkflowNative.exe': native, 'bin/native-manifest.json': native_manifest_data}))
        assets.append(windows)
    guide = args.output / (base + '-guide.html')
    shutil.copyfile(ROOT / '사용가이드.html', guide)
    assets.append(guide)
    sums = ''.join(digest(path.read_bytes()) + '  ' + path.name + '\n' for path in assets)
    (args.output / 'SHA256SUMS.txt').write_text(sums, encoding='ascii')
    print(json.dumps({'version': VERSION, 'assets': [p.name for p in assets] + ['SHA256SUMS.txt'],
                      'source_files': len(sources), 'private_data_included': False}))


if __name__ == '__main__':
    main()
