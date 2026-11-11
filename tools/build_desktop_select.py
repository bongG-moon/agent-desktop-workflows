"""Build the independent source-only Claude Code skill, without a network or compiler."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'desktop-select'
VERSION = '0.4.1'
FILES = {name: SKILL / name for name in ('SKILL.md', '사용가이드.md', 'scripts/desktop_select.py',
                                         'scripts/live_action.py', 'scripts/targeting.py',
                                         'scripts/teach_capture.py', 'scripts/learned_tasks.py',
                                         'scripts/row_vision.py', 'scripts/row_teach.py', 'scripts/row_action.py',
                                         'scripts/context_teach.py', 'scripts/context_menu.py',
                                         'scripts/result_menu.py', 'scripts/export_evidence.py',
                                         'scripts/provider_visibility.py', 'scripts/provider_values.py',
                                         'scripts/provider_labels.py', 'scripts/runtime_integrity.py',
                                         'runtime-manifest.json')}
FILES.update({'scripts/win_bridge.py': ROOT / 'scripts/win_bridge.py', 'scripts/recorder.py': ROOT / 'scripts/recorder.py',
              'LICENSE': ROOT / 'LICENSE',
              'NOTICE.txt': SKILL / 'NOTICE.txt'})


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    out = destination / f'desktop-select-{VERSION}-windows-x64.zip'
    # The distribution contains only text. Match committed bytes on both
    # Windows checkouts with autocrlf and on checkouts that preserve LF.
    contents = {name: path.read_bytes().replace(b'\r\n', b'\n') for name, path in FILES.items()}
    manifest = json.loads(contents['runtime-manifest.json'])
    if manifest.get('schema') != 1 or set(manifest.get('files', {})) != {'win_bridge.py', 'recorder.py'}:
        raise ValueError('runtime_manifest_invalid')
    for name, expected in manifest['files'].items():
        if sha256(contents['scripts/' + name]).hexdigest() != expected:
            raise ValueError('runtime_manifest_stale: ' + name)
    with zipfile.ZipFile(out, 'w') as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo('desktop-select/' + name)
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(out) as archive:
        if archive.testzip() is not None or any(archive.read('desktop-select/' + name) != data
                                                for name, data in contents.items()):
            raise ValueError('archive_verification_failed')
    guide = destination / f'desktop-select-{VERSION}-guide.md'
    guide.write_bytes(contents['사용가이드.md'])
    digest = sha256(out.read_bytes()).hexdigest()
    out.with_suffix('.zip.sha256').write_text(digest + '  ' + out.name + '\n', encoding='ascii')
    (destination / 'SHA256SUMS.txt').write_bytes(
        f'{digest}  {out.name}\n{sha256(guide.read_bytes()).hexdigest()}  {guide.name}\n'.encode('ascii'))
    print(out)
    print(digest)
    return out


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, default=ROOT / '.build/desktop-select')
    build(p.parse_args().out)
