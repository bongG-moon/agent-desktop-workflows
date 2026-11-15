"""Single-use local replies to a live conditional workflow. Never restart its input sequence."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

import learned_tasks as tasks


ANSWER_TTL = 300
TOKEN = re.compile('[0-9a-f]{64}')
FILES = re.compile(r'(?:pending|answer|out)-[0-9a-f]{32}\.json|frame-[0-9a-f]{32}\.png|stage-[0-9a-f]{32}|worker\.json')


def fail(reason):
    raise tasks.TaskError(reason)


def _root():
    root = tasks._directory(Path(tempfile.gettempdir()) / 'desktop-select-scenes')
    root.mkdir(exist_ok=True)
    return root


def _publish(path, data):
    """Publish complete bytes once; competing answers cannot replace one another."""
    tasks._deny_redirect(path)
    tmp = path.with_name('stage-' + secrets.token_hex(16))
    try:
        with tmp.open('xb') as output:
            output.write(data)
        if os.name == 'nt':
            os.rename(tmp, path)
        else:
            os.link(tmp, path)
    except FileExistsError as exc:
        raise tasks.TaskError('scene_ticket_already_used') from exc
    finally:
        tmp.unlink(missing_ok=True)


def _json(path, payload):
    _publish(path, json.dumps(payload, ensure_ascii=False).encode('utf8'))


def _read(path):
    tasks._deny_redirect(path)
    with path.open('rb') as stream:
        data = stream.read(tasks.MAX_FILE_BYTES + 1)
    if len(data) > tasks.MAX_FILE_BYTES:
        fail('invalid_scene_packet')
    return json.loads(data.decode('utf8'), object_pairs_hook=tasks._unique_keys)


def _cleanup(root):
    for folder in root.iterdir():
        if not re.fullmatch('[0-9a-f]{32}', folder.name):
            continue
        try:
            tasks._deny_redirect(folder)
            if time.time() - folder.stat().st_mtime < 2 * ANSWER_TTL:
                continue
            worker = folder / 'worker.json'
            if worker.is_file() and _alive(_read(worker)['pid']):
                continue
            for item in folder.iterdir():
                if FILES.fullmatch(item.name):
                    tasks._deny_redirect(item)
                    item.unlink(missing_ok=True)
            folder.rmdir()
        except (OSError, tasks.TaskError):
            continue


def _paths(ticket):
    if not isinstance(ticket, str) or not TOKEN.fullmatch(ticket):
        fail('invalid_scene_ticket')
    folder = tasks._directory(_root() / ticket[:32])
    return folder, ticket[32:]


def _alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != 'nt':
        try:
            os.kill(pid,0)
            return True
        except ProcessLookupError:
            return False
    import ctypes as C
    from ctypes import wintypes as W
    import win_bridge
    kernel = win_bridge.kernel32
    handle = win_bridge._sig(kernel,'OpenProcess',W.HANDLE,W.DWORD,W.BOOL,W.DWORD)(0x100000,False,pid)
    if not handle:
        return False
    try:
        return win_bridge._sig(kernel,'WaitForSingleObject',W.DWORD,W.HANDLE,W.DWORD)(handle,0) == 258
    finally:
        kernel.CloseHandle(handle)


def _await(folder, cursor, timeout, child=None, pid=None):
    output = folder / ('out-' + cursor + '.json')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if output.exists():
            result = _read(output)
            output.unlink()
            return result
        if child is not None and child.poll() is not None and not output.exists():
            break
        if pid is not None and not _alive(pid) and not output.exists():
            break
        time.sleep(.1)
    if child is not None and child.poll() is None:
        child.kill()
        child.wait()
    return dict(status='uncertain', diagnostic='scene_worker_no_receipt', input_dispatched=None)


def start(workflow, recipe, options, timeout):
    root = _root()
    _cleanup(root)
    ticket = secrets.token_hex(32)
    folder, cursor = _paths(ticket)
    folder.mkdir()
    args = [sys.executable, '-X', 'utf8', '-B', str(Path(workflow).resolve()), '_scene_worker', ticket]
    child = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
    try:
        child.stdin.write(json.dumps(dict(recipe=recipe, options=options)).encode('utf8'))
    finally:
        child.stdin.close()
    return _await(folder, cursor, timeout, child)


class Session:
    def __init__(self, ticket, timeout):
        self.folder, self.cursor = _paths(ticket)
        self.timeout = timeout
        self.timer = None
        _json(self.folder/'worker.json',dict(pid=os.getpid()))

    def arm(self, seconds):
        self.disarm()
        self.timer = threading.Timer(seconds,os._exit,args=(124,))
        self.timer.daemon = True
        self.timer.start()

    def disarm(self):
        if self.timer is not None:
            self.timer.cancel()

    def send(self, result):
        _json(self.folder / ('out-' + self.cursor + '.json'), result)

    def ask(self, png, prompt, progress):
        token = secrets.token_hex(16)
        ticket = self.folder.name + token
        image = self.folder / ('frame-' + token + '.png')
        pending = self.folder / ('pending-' + token + '.json')
        answer = self.folder / ('answer-' + token + '.json')
        expires = time.time() + ANSWER_TTL
        _publish(image, png)
        try:
            _json(pending, dict(expires=expires, timeout=self.timeout, pid=os.getpid(),
                                sha256=hashlib.sha256(png).hexdigest()))
            self.arm(ANSWER_TTL + 5)
            self.send(dict(progress, **prompt, status='needs_scene', image=str(image), ticket=ticket,
                           expires_seconds=ANSWER_TTL))
            self.cursor = token
            deadline = time.monotonic() + ANSWER_TTL
            while time.monotonic() < deadline:
                if answer.is_file():
                    self.arm(max(5,self.timeout-5))
                    return _read(answer)
                time.sleep(.12)
            fail('scene_response_expired')
        finally:
            image.unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
            # Keep the exclusive answer marker until no submitter can claim it again.


def answer(ticket, decision, point):
    folder, cursor = _paths(ticket)
    pending = folder / ('pending-' + cursor + '.json')
    if not pending.is_file():
        fail('scene_ticket_expired_or_used')
    info = _read(pending)
    if time.time() >= info['expires']:
        fail('scene_response_expired')
    if not _alive(info['pid']):
        fail('scene_worker_unavailable')
    if decision not in ('absent','present','unknown') or (point is None) != (decision == 'unknown'):
        fail('scene_point_required_for_visible_target')
    image = folder / ('frame-' + cursor + '.png')
    tasks._deny_redirect(image)
    if hashlib.sha256(image.read_bytes()).hexdigest() != info['sha256']:
        fail('scene_preview_changed')
    _json(folder / ('answer-' + cursor + '.json'), dict(decision=decision, point=point))
    return _await(folder, cursor, info['timeout'],pid=info['pid'])


def worker(app, ticket):
    session = None
    try:
        packet = json.load(sys.stdin.buffer)
        recipe = app.validate(packet['recipe'])
        session = Session(ticket, app.worker_timeout(recipe))
        session.arm(max(5,session.timeout-5))
        report = app.run(recipe, **packet['options'], scene_session=session)
    except Exception as error:
        report = dict(status='uncertain', input_dispatched=None, diagnostic=getattr(error,'code','scene_worker_failure'))
    if session is not None:
        try:
            session.send(report)
        finally:
            session.disarm()
