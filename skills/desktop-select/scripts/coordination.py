"""Session-local Windows input reservations and queue-aware worker deadlines."""
from contextlib import ExitStack, contextmanager
import ctypes as C
from ctypes import wintypes as W
from functools import lru_cache
from hashlib import sha256
import mmap
import os
import subprocess
import time


QUEUE_SECONDS = 600
_CLOCK_ENV = 'DESKTOP_SELECT_WORKER_CLOCK'


class CoordinationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@lru_cache(maxsize=1)
def _kernel():
    dll = C.WinDLL('kernel32', use_last_error=True)
    for name, arguments, result in (
        ('CreateMutexW', (C.c_void_p, W.BOOL, W.LPCWSTR), W.HANDLE),
        ('WaitForSingleObject', (W.HANDLE, W.DWORD), W.DWORD),
        ('ReleaseMutex', (W.HANDLE,), W.BOOL),
        ('CloseHandle', (W.HANDLE,), W.BOOL),
    ):
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, result
    return dll


def _mapping(token):
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise CoordinationError('coordination_clock_unavailable')
    return mmap.mmap(-1, 8, tagname='Local\\DesktopSelect.Clock.' + token)


@lru_cache(maxsize=1)
def _inherited_clock():
    token = os.environ.get(_CLOCK_ENV)
    return _mapping(token) if token else None


def _ticks():
    return time.monotonic_ns() // 1_000_000


def _queued(clock):
    if clock is None:
        return 0
    # One worker waits on behalf of a synchronous process tree at a time. An
    # aligned signed 64-bit word on Windows x64 is negative during that wait.
    counter = C.c_int64.from_buffer(clock).value
    return _ticks() + counter if counter < 0 else counter


@contextmanager
def child_clock(env=None):
    env = os.environ if env is None else env
    token = env.get(_CLOCK_ENV)
    if os.name != 'nt' or (token and token == clock_token()):
        yield env, _inherited_clock()
    elif token:
        with _mapping(token) as clock:
            yield env, clock
    else:
        token = os.urandom(16).hex()
        with _mapping(token) as clock:
            yield dict(env, **{_CLOCK_ENV: token}), clock


def clock_token():
    return os.environ.get(_CLOCK_ENV)


@contextmanager
def ticket_clock(token):
    if token:
        with _mapping(token) as clock:
            yield clock
    else:
        yield None


class ActiveDeadline:
    def __init__(self, seconds, clock=None):
        self.clock = _inherited_clock() if clock is None else clock
        self.start_wait = _queued(self.clock)
        self.end = time.monotonic() + seconds

    def remaining(self):
        paused = min(QUEUE_SECONDS, max(0, (_queued(self.clock) - self.start_wait) / 1000))
        return self.end - time.monotonic() + paused


def run_worker(argv, *, timeout, input=None, capture_output=True, **kwargs):
    """Keep the provider watchdog running except for time actually queued.

    The private, page-backed counter is shared with nested workers. An idle
    run blocks once in communicate and neither polls nor writes a file.
    """
    if os.name != 'nt':
        return subprocess.run(argv, timeout=timeout, input=input, capture_output=capture_output, **kwargs)
    with child_clock(kwargs.get('env')) as (env, clock):
        kwargs['env'] = env
        deadline = ActiveDeadline(timeout, clock)
        if input is not None:
            kwargs['stdin'] = subprocess.PIPE
        if capture_output:
            kwargs['stdout'] = kwargs['stderr'] = subprocess.PIPE
        with subprocess.Popen(argv, **kwargs) as process:
            try:
                while True:
                    try:
                        stdout, stderr = process.communicate(input, timeout=max(.001, deadline.remaining()))
                        completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                        completed.queue_seconds = max(0, _queued(clock) - deadline.start_wait) / 1000
                        return completed
                    except subprocess.TimeoutExpired:
                        input = None  # communicate retains its reader threads and pending input.
                        if deadline.remaining() <= 0:
                            raise
            except BaseException:
                process.kill()
                process.wait()
                raise


@contextmanager
def _lease(name, timeout_code):
    kernel = _kernel()
    mutex = kernel.CreateMutexW(None, False, 'Local\\DesktopSelect.' + name)
    if not mutex:
        raise CoordinationError('coordination_unavailable')
    acquired = False
    try:
        result = kernel.WaitForSingleObject(mutex, 0)
        if result == 258:  # WAIT_TIMEOUT. Only a contending caller touches the clock.
            clock = _inherited_clock()
            previous = _queued(clock)
            start = _ticks()
            if clock is not None:
                C.c_int64.from_buffer(clock).value = previous - start
            try:
                result = kernel.WaitForSingleObject(mutex, max(0, QUEUE_SECONDS * 1000 - previous))
            finally:
                if clock is not None:
                    C.c_int64.from_buffer(clock).value = previous + _ticks() - start
        if result == 258:
            raise CoordinationError(timeout_code)
        if result not in (0, 0x80):  # WAIT_ABANDONED also transfers ownership.
            raise CoordinationError('coordination_unavailable')
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


def desktop_input():
    return _lease('Input', 'desktop_input_queue_timeout')


def _window_key(value):
    from desktop_select import bridge
    hwnd = bridge.normalize_hwnd(value)
    if not hwnd:
        raise CoordinationError('invalid_target')
    return hwnd


def handle(binding):
    value = binding['hwnd'] if isinstance(binding, dict) else binding.hwnd
    return _lease('Window.' + str(_window_key(value)), 'window_queue_timeout')


def reserve_handles(stack, held, hwnds):
    """Keep already-open secondary windows reserved before the first action."""
    for hwnd in sorted({_window_key(value) for value in hwnds} - held):
        stack.enter_context(handle({'hwnd': hwnd}))
        held.add(hwnd)


def _scope(exe, title):
    return sha256((str(exe).casefold() + '\0' + title).encode('utf8')).hexdigest()


@contextmanager
def window(binding, title=None, contexts=()):
    """Reserve declared windows before input, including titles opened by later steps."""
    if not contexts:
        # An ordinary job owns only one actual HWND and cannot create a cycle.
        # Two different HWNDs may have the same title and run independently.
        with handle(binding):
            yield
        return
    if isinstance(binding, dict):
        exe, hwnd = binding['exe_name'], binding['hwnd']
    else:
        exe, hwnd = binding.exe, binding.hwnd
    if title is None:
        from win_bridge import window_text
        title = window_text(hwnd)
    names = {_scope(exe, title), *(_scope(item['exe'], item['title']) for item in contexts)}
    with ExitStack() as stack:
        for name in sorted(names):
            stack.enter_context(_lease('Scope.' + name, 'window_queue_timeout'))
        stack.enter_context(handle(binding))
        yield


def scopes_for(steps, main_exe):
    scopes = []
    for step in steps:
        scope = step.get('window_context')
        if scope:
            scopes.append(scope)
        if step.get('popup'):
            scopes.append(dict(exe=scope['exe'] if scope else main_exe, title=step['popup']['title']))
    return scopes


def recipe(path):
    key = sha256(os.path.normcase(str(path.resolve())).encode('utf8')).hexdigest()
    return _lease('Recipe.' + key, 'registered_workflow_queue_timeout')


@contextmanager
def reserve_outputs(steps, *, verify_steps=()):
    """Reserve declared export folders from before production until verification ends."""
    outputs = [step['output'] for step in verify_steps if step['action'] == 'verify-file']
    for step in steps:
        if step['action'] == 'verify-file':
            outputs.append(step['output'])
        elif step['action'] == 'result-menu' and step['context'].get('output'):
            outputs.append(step['context']['output'])
    if not outputs:
        yield
        return
    # Directory evidence accepts any immediate workbook. A named workbook
    # must share its parent's reservation with directory observers as well.
    folders = {spec['path'] if spec['kind'] == 'directory' else os.path.dirname(spec['path'])
               for spec in outputs}
    keys = {os.path.normcase(os.path.realpath(folder)) for folder in folders}
    with ExitStack() as stack:
        for key in sorted(keys):
            name = sha256(key.encode('utf8')).hexdigest()
            stack.enter_context(_lease('Output.' + name, 'export_queue_timeout'))
        yield
