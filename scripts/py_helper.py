"""Local file protocol for the transparent Python Windows backend.

The caller starts this script in an isolated interpreter with a deadline.
It neither compiles nor downloads executable code. stdout and stderr remain quiet.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import struct
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
# Python -I deliberately removes the working directory and PYTHONPATH. These
# exact package-owned directories supply the two other backend source modules.
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'engine'))
from local_core import EngineError, validate_local_directory

COMMANDS = {'windows', 'inspect', 'probe', 'activate', 'point', 'record', 'combo-option'}
MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid_arguments')


def safe_file(value, *, exists=False):
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')) or ':' in path.name:
        raise ValueError('local_absolute_file_required')
    validate_local_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if exists:
            raise ValueError('local_file_missing')
        return path
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or not path.is_file():
        raise ValueError('local_file_redirected')
    return path


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def read_request(path):
    path = safe_file(path, exists=True)
    if path.stat().st_size > MAX_REQUEST:
        raise ValueError('request_too_large')
    with path.open('rb') as stream:
        payload = stream.read(MAX_REQUEST + 1)
    if len(payload) > MAX_REQUEST:
        raise ValueError('request_too_large')
    result = json.loads(payload.decode('utf-8-sig'), object_pairs_hook=_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_json')))
    if not isinstance(result, dict):
        raise ValueError('invalid_request')
    return result


def write_response(path, value):
    path = safe_file(path)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(data) > MAX_RESPONSE:
        raise ValueError('response_too_large')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        safe_file(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def dispatch(command, request):
    import win_bridge
    win_bridge.configure_dpi()
    if command == 'record':
        from recorder import record
        return record(request)
    if command == 'windows':
        return win_bridge.inventory()
    return {'inspect': win_bridge.inspect, 'probe': win_bridge.probe,
            'activate': win_bridge.activate, 'point': win_bridge.point, 'combo-option': win_bridge.combo_option}[command](request)


def main(argv=None):
    response = None
    command = None
    backend_entered = False
    try:
        parser = Parser(add_help=False)
        parser.add_argument('command', choices=sorted(COMMANDS))
        parser.add_argument('--request', required=True)
        parser.add_argument('--response', required=True)
        args = parser.parse_args(argv)
        response = safe_file(args.response)
        command = args.command
        if os.name != 'nt' or struct.calcsize('P') != 8:
            raise ValueError('windows_x64_python_required')
        request = read_request(args.request)
        backend_entered = True
        result = dispatch(command, request)
        if not isinstance(result, dict) or type(result.get('ok')) is not bool:
            raise ValueError('invalid_backend_response')
        write_response(response, result)
        return 0 if result['ok'] else 2
    except (Exception, KeyboardInterrupt) as error:
        if response is None:
            return 2
        # Backend errors carry only fixed codes and explicit dispatch evidence.
        code = getattr(error, 'code', None)
        dispatched = getattr(error, 'input_dispatched', None)
        if not isinstance(code, str):
            code = str(error) if isinstance(error, (ValueError, EngineError)) else 'python_backend_failed'
            dispatched = None if backend_entered and command in {'point', 'activate', 'combo-option'} else False
        if not re.fullmatch('[a-z_]{1,80}', code):
            code = 'python_backend_failed'
        if type(dispatched) not in (bool, type(None)):
            dispatched = None
        try:
            write_response(response, {'ok': False, 'status': 'blocked' if dispatched is False else 'uncertain',
                'diagnostic': code, 'input_dispatched': dispatched,
                'delivery': 'not_delivered' if dispatched is False else 'possibly_dispatched',
                'retry': 'inspect_again' if dispatched is False else 'unsafe'})
        except Exception:
            pass
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
