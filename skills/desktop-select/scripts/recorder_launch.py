"""One short launcher, one transient recorder, and durable local receipts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import traceback

import learned_tasks as tasks
import storage_paths


STARTUP_WAIT_SECONDS = 8.0
STARTUP_GRACE_SECONDS = 30.0
# The read-only provider worker started after a draft save can run for 90s.
SAVED_PROCESSING_WAIT_SECONDS = 95.0
MAX_REQUEST_BYTES = 65536
MAX_EVENT_BYTES = 4 * 1024 * 1024
LIVE_EVENTS = {'recorder_ready', 'recorder_selection_required', 'recorder_starting',
               'recorder_processing', 'recording'}


def _folder(token):
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('invalid_recorder_session')
    store = tasks._directory(storage_paths.default_store())
    return tasks._directory(store.parent / 'recording-sessions' / token)


def _path(folder, name):
    tasks._directory(folder)
    path = folder / name
    tasks._deny_redirect(path)
    return path


def _read(folder, name, limit, *, optional=False):
    try:
        path = _path(folder, name)
        if path.exists() and not path.is_file():
            raise ValueError('recorder_session_file_invalid')
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
    except FileNotFoundError:
        if optional:
            return b''
        raise
    if len(data) > limit:
        raise ValueError('recorder_session_file_too_large')
    return data


def _write(folder, name, payload):
    data = (json.dumps(payload, ensure_ascii=True, allow_nan=False) + '\n').encode('utf-8')
    with _path(folder, name).open('xb') as stream:
        stream.write(data)


def _request(folder):
    request = json.loads(_read(folder, 'request.json', MAX_REQUEST_BYTES))
    if (not isinstance(request, dict) or set(request) != {'app', 'task', 'created'}
            or type(request['created']) not in (int, float) or not math.isfinite(request['created'])):
        raise ValueError('invalid_recorder_request')
    _validate(request['app'], request['task'])
    return request


def _validate(app_hint, task):
    if app_hint is not None and not tasks._text(app_hint, 200):
        raise ValueError('invalid_recorder_request')
    if task is not None:
        tasks._name(task)


def _lock(stream, *, release=False):
    stream.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _lease(folder):
    # An OS lock disappears when the child dies, so PID reuse cannot fake readiness.
    with _path(folder, 'running.lock').open('r+b') as stream:
        _lock(stream)
        try:
            yield
        finally:
            _lock(stream, release=True)


def _running(folder):
    with _path(folder, 'running.lock').open('r+b') as stream:
        try:
            _lock(stream)
        except (BlockingIOError, PermissionError):
            return True
        _lock(stream, release=True)
        return False


def _events(folder):
    raw = _read(folder, 'events.jsonl', MAX_EVENT_BYTES, optional=True)
    events = []
    # A partially written final line is not a receipt. Keep earlier complete lines.
    for line in raw.split(b'\n')[:-1]:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and isinstance(event.get('status'), str):
            events.append(event)
    return events


def _receipt(result, token):
    receipt = dict(result, session=token, status_args=['recorder', '--session', token, '--wait', '8'])
    state = result['status']
    if state in ('recorder_ready', 'recording'):
        receipt['host_action'] = 'wait_for_user_to_save'
    elif state == 'recorder_selection_required':
        receipt['host_action'] = 'wait_for_user_to_select_window'
    elif state == 'cancelled':
        receipt['host_action'] = 'report_recording_cancelled'
    elif state == 'recorded':
        receipt['first_trial_args'] = ['try-recording', '--session', token]
        if result.get('next_action', {}).get('kind') == 'run_prepared_recording':
            receipt['next_action'] = dict(result['next_action'], args=receipt['first_trial_args'])
    return receipt


def _failure(error, token=None):
    result = dict(status='blocked', diagnostic=getattr(error, 'code', None)
                  or (str(error) if isinstance(error, ValueError) else 'recorder_session_unavailable'),
                  input_dispatched=False)
    return _receipt(result, token) if token else result


def _publish_saved(folder, result, *, editing=False):
    # Publish the durable result before the recorder child exits.
    temporary = 'save-' + secrets.token_hex(8) + '.tmp'
    try:
        _write(folder, temporary, dict(receipt=result, editing=editing))
        os.replace(_path(folder, temporary), _path(folder, 'saved.json'))
    finally:
        _path(folder, temporary).unlink(missing_ok=True)


def after_save(app, token, result):
    """Finish after publishing; later edits and retakes are explicit receipt commands."""
    from recording_followup import followup

    folder = _folder(token)
    saved = followup(result)
    try:
        _publish_saved(folder, saved)
    except OSError:
        pass  # The task is already durable; the normal terminal receipt remains usable.
    return saved


def _observe(folder, token, *, initial=False, exited=False):
    request = _request(folder)
    saved = _read(folder, 'saved.json', MAX_EVENT_BYTES, optional=True)
    if saved:
        checkpoint = json.loads(saved)
        result = checkpoint.get('receipt') if isinstance(checkpoint, dict) else None
        if (not isinstance(result, dict) or result.get('status') != 'recorded'
                or not isinstance(result.get('task'), dict) or not isinstance(result['task'].get('name'), str)
                or type(checkpoint.get('editing')) is not bool):
            raise ValueError('recorder_session_file_invalid')
        if checkpoint['editing']:
            if _running(folder):
                return _receipt(dict(status='recording', saved_task=result['task']['name'],
                                     input_dispatched=False), token)
            # A later task save can succeed even when its UI checkpoint fails.
            result = next((e for e in reversed(_events(folder)) if e['status'] == 'recorded'), result)
        return _receipt(result, token)
    events = _events(folder)
    terminal = [event for event in events if event['status'] not in LIVE_EVENTS]
    if terminal:
        return _receipt(terminal[-1], token)
    complete = _read(folder, 'complete.json', 1024, optional=True)
    alive = _running(folder)
    if complete or exited or (not alive and (events or time.time() - request['created'] > STARTUP_GRACE_SECONDS)):
        # The child can flush its final receipt after our first read, then exit.
        events = _events(folder)
        terminal = [event for event in events if event['status'] not in LIVE_EVENTS]
        if terminal:
            return _receipt(terminal[-1], token)
        try:
            saved = json.loads(complete).get('receipt') if complete else None
        except (ValueError, AttributeError):
            saved = None
        if isinstance(saved, dict) and saved.get('status') not in LIVE_EVENTS and isinstance(saved.get('status'), str):
            return _receipt(saved, token)
        return _receipt(dict(status='uncertain' if events else 'blocked',
                             diagnostic='recorder_exited_without_receipt',
                             input_dispatched=None if events else False), token)
    if events and alive:
        result = events[-1]
        if result['status'] == 'recorder_ready' and not initial:
            result = dict(result, status='recording')
        return _receipt(result, token)
    return _receipt(dict(status='recorder_starting', input_dispatched=False), token)


def wait_for_trial(token):
    """Keep a requested trial in the same call while its saved draft is being prepared."""
    return status(token, wait_seconds=STARTUP_WAIT_SECONDS, wait_for_saved=True)


def status(token, *, wait_seconds=0, wait_for_saved=False):
    """Read this project's session without ever reopening or replaying the recorder."""
    try:
        if (type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds)
                or not 0 <= wait_seconds <= 30):
            raise ValueError('invalid_recorder_wait')
        folder = _folder(token)
        started = time.monotonic()
        deadline = started + wait_seconds
        while True:
            result = _observe(folder, token)
            if wait_for_saved and result['status'] == 'recorder_processing' and result.get('saved') is True:
                deadline = time.monotonic() + SAVED_PROCESSING_WAIT_SECONDS
                wait_for_saved = False  # Bound the wait even if the worker stops making progress.
            now = time.monotonic()
            remaining = deadline - now
            if result['status'] not in ('recorder_starting', 'recorder_processing') or remaining <= 0:
                return result
            interval = 0.5 if now - started >= STARTUP_WAIT_SECONDS else 0.1
            time.sleep(min(interval, remaining))
    except (OSError, ValueError) as error:
        return _failure(error, token if isinstance(token, str) and re.fullmatch('[0-9a-f]{32}', token) else None)


def launch(app_hint, task=None):
    """Return on an actual UI event, a final receipt, or the short startup deadline."""
    started = time.monotonic()
    token = None
    folder = child = None
    created = False
    try:
        _validate(app_hint, task)
        token = secrets.token_hex(16)
        folder = _folder(token)
        folder.mkdir(parents=True, exist_ok=False)
        created = True
        _write(folder, 'request.json', dict(app=app_hint, task=task, created=time.time()))
        _request(folder)
        with _path(folder, 'running.lock').open('xb') as stream:
            stream.write(b'0')
        options = dict(stdin=subprocess.DEVNULL, close_fds=True)
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        with (_path(folder, 'events.jsonl').open('xb') as output,
              _path(folder, 'diagnostic.log').open('xb') as diagnostic):
            child = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).absolute()),
                                      '--child', token], stdout=output, stderr=diagnostic, **options)
        while True:
            result = _observe(folder, token, initial=True, exited=child.poll() is not None)
            elapsed = time.monotonic() - started
            if result['status'] != 'recorder_starting' or elapsed >= STARTUP_WAIT_SECONDS:
                return dict(result, startup_wait_seconds=round(elapsed, 3))
            time.sleep(min(0.05, STARTUP_WAIT_SECONDS - elapsed))
    except (OSError, ValueError) as error:
        result = _failure(error, token)
        if created and child is None:
            try:
                _write(folder, 'complete.json', dict(exit_code=2, receipt=result))
            except (OSError, ValueError):
                pass
        return result


def _child(token):
    folder = _folder(token)
    code = 3
    with _lease(folder):
        try:
            request = _request(folder)
            import desktop_select as app
            code = app.worker(argparse.Namespace(command='_record_app', app=request['app'],
                                                 task=request['task'], session=token))
            sys.stdout.flush()
        except BaseException as error:
            traceback.print_exc(file=sys.stderr)
            ready = any(event['status'] == 'recorder_ready' for event in _events(folder))
            print(json.dumps(dict(status='uncertain' if ready else 'blocked',
                                  diagnostic=getattr(error, 'code', 'recorder_worker_failed'),
                                  input_dispatched=None if ready else False)), flush=True)
            code = 3 if ready else 2
        finally:
            _write(folder, 'complete.tmp', dict(exit_code=code))
            os.replace(_path(folder, 'complete.tmp'), _path(folder, 'complete.json'))
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', required=True)
    raise SystemExit(_child(parser.parse_args().child))
