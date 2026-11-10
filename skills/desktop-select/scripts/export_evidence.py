"""Read-only evidence for a new or changed local Excel workbook after one menu click."""
import ntpath
from hashlib import sha256
import os
import posixpath
from pathlib import Path
import stat
import time
import zipfile
from xml.etree import ElementTree as ET

from learned_tasks import TaskError


def validate(spec):
    if not isinstance(spec, dict) or set(spec) != {'kind', 'path'} or spec['kind'] not in ('file', 'directory'):
        raise TaskError('invalid_export_output')
    s = spec['path']
    if not isinstance(s, str) or len(s) > 1000 or not s or any(ord(c) < 32 for c in s):
        raise TaskError('export_local_path_required')
    drive, tail = ntpath.splitdrive(s)
    parts = tail.replace('/', '\\').split('\\')
    if (len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha() or drive[1] != ':' or
            not tail.startswith(('\\', '/')) or ':' in tail or any(
                p in ('.', '..') or p.endswith((' ', '.')) or any(c in p for c in '<>"|?*')
                or (p and ntpath.isreserved(p) if hasattr(ntpath, 'isreserved') else p and Path(p).is_reserved())
                for p in parts)):
        raise TaskError('export_local_path_required')
    if spec['kind'] == 'file' and Path(s).suffix.lower() not in ('.xlsx', '.xlsm'):
        raise TaskError('export_workbook_type_unsupported')


def _plain(path, directory):
    s = path.lstat()
    if (s.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT or
            not (stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode))):
        raise TaskError('export_path_redirected')
    return s


def inventory(spec):
    validate(spec)
    path = Path(spec['path'])
    folder = path if spec['kind'] == 'directory' else path.parent
    # Exclude mapped network drives as well as UNC paths, even if mapped with a drive letter.
    import ctypes
    if ctypes.windll.kernel32.GetDriveTypeW(str(folder.anchor)) not in (2, 3, 6):
        raise TaskError('export_local_path_required')
    for parent in [*reversed(folder.parents), folder]:
        _plain(parent, True)
    def signature(file):
        s = _plain(file, False)
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
    if spec['kind'] == 'file':
        try:
            return {path: signature(path)}
        except FileNotFoundError:
            return {}
    found = {}
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries):
            if index >= 2048:
                raise TaskError('export_directory_too_large')
            p = Path(entry.path)
            if not p.name.startswith('~$') and p.suffix.lower() in ('.xlsx', '.xlsm'):
                found[p] = signature(p)
    return found


def workbook(path):
    """Validate small metadata parts without extracting anything or reading business cells."""
    try:
        if path.stat().st_size > 268_435_456:
            return False
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if len(names) > 20000 or len(names) != len(set(names)):
                return False
            def xml(name):
                i = z.getinfo(name)
                if i.file_size > 2_000_000:
                    raise ValueError('metadata_too_large')
                data = z.read(i)
                if b'<!DOCTYPE' in data.upper():
                    raise ValueError('invalid_xml')
                return ET.fromstring(data)
            types = xml('[Content_Types].xml')
            rels = xml('_rels/.rels')
            if not any(r.get('Type','').endswith('/officeDocument') and
                       r.get('TargetMode', 'Internal') == 'Internal' and
                       r.get('Target','').lstrip('/') == 'xl/workbook.xml' for r in rels):
                return False
            if not any(c.get('PartName') == '/xl/workbook.xml' and c.get('ContentType') in (
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
                    'application/vnd.ms-excel.sheet.macroEnabled.main+xml') for c in types):
                return False
            book = xml('xl/workbook.xml')
            if not book.tag.endswith('}workbook'):
                return False
            rels = xml('xl/_rels/workbook.xml.rels')
            ids = {r.get('Id'):r for r in rels}
            if len(ids) != len(rels):
                return False
            for sheet in book.findall('./{*}sheets/{*}sheet'):
                rid = next((v for k,v in sheet.attrib.items() if k.endswith('}id')), None)
                r = ids.get(rid)
                if r is None or r.get('TargetMode','Internal') != 'Internal' or not r.get('Type','').endswith('/worksheet'):
                    continue
                target = r.get('Target','')
                member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                if not member.startswith('xl/worksheets/') or member not in names:
                    continue
                # Confirm the referenced part is a worksheet without extracting any cells.
                parser = ET.XMLPullParser(events=('start',))
                with z.open(member) as stream:
                    for _ in range(16):
                        prefix = stream.read(512)
                        if not prefix:
                            break
                        parser.feed(prefix)
                        for _, root in parser.read_events():
                            return root.tag.endswith('}worksheet')
            return False
    except (OSError, KeyError, ValueError, RuntimeError, ET.ParseError, zipfile.BadZipFile):
        return False


def _digest(path):
    h = sha256()
    with path.open('rb') as stream:
        if os.fstat(stream.fileno()).st_size > 268_435_456:
            raise TaskError('export_workbook_too_large')
        for block in iter(lambda: stream.read(1_048_576), b''):
            h.update(block)
    return h.digest()


class Baseline(dict):
    prior_digest = None


def baseline(spec):
    result = Baseline(inventory(spec))
    if spec['kind'] == 'file' and result:
        result.prior_digest = _digest(next(iter(result)))
        if inventory(spec) != result:
            raise TaskError('export_baseline_unstable')
    return result


def wait(spec, before, *, timeout=26, pause=.3):
    deadline, previous = time.monotonic() + timeout, None
    ignored = None
    while True:
        try:
            after = inventory(spec)
            candidates = [(p, sig) for p, sig in after.items() if
                          (p not in before if spec['kind'] == 'directory' else before.get(p) != sig)]
            if len(candidates) > 1:
                return False, 'multiple_new_workbooks'
            if candidates and previous == candidates[0] and ignored != previous and workbook(candidates[0][0]):
                path, signature = previous
                old = before.get(path)
                prior_digest = getattr(before, 'prior_digest', None)
                # Touching an old file without exporting it must not verify an export.
                if prior_digest and old[:2] == signature[:2] and _digest(path) == prior_digest:
                    ignored = previous
                # Final stat revalidates the exact file we opened after its ZIP metadata was parsed.
                elif inventory(spec) == after:
                    return True, 'export_workbook_verified'
            previous = candidates[0] if candidates else None
        except (OSError, zipfile.BadZipFile):
            previous = None
        if time.monotonic() >= deadline:
            return False, 'export_workbook_not_observed'
        time.sleep(pause)
