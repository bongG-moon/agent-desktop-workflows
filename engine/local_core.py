"""Bounded child process and read-only Windows identity primitives."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import threading
import time

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024

class EngineError(RuntimeError):
    """Fixed diagnostic codes only: no private provider strings in exceptions."""


def _integer(value, low, high, code):
    if type(value) is not int or not low <= value <= high:
        raise EngineError(code)
    return value


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _identity(pid, hwnd, expected_process_exe):
    """Read process identity and HWND ownership only; no UIA or window activation."""
    _integer(pid, 1, 0xFFFFFFFF, "invalid_pid")
    if hwnd is not None:
        _integer(hwnd, 1, 0x7FFFFFFFFFFFFFFF, "invalid_hwnd")
    if os.name != "nt":
        raise EngineError("windows_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    actual, own_session, target_session = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if hwnd is not None and (not user.GetWindowThreadProcessId(hwnd, ctypes.byref(actual)) or actual.value != pid):
        raise EngineError("window_process_mismatch")
    if (not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(own_session))
            or not kernel.ProcessIdToSessionId(pid, ctypes.byref(target_session))
            or own_session.value != target_session.value):
        raise EngineError("different_windows_session")
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise EngineError("process_identity_unavailable")
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise EngineError("process_identity_unavailable")
        observed_exe = _canonical(name.value)
        if observed_exe != _canonical(expected_process_exe):
            raise EngineError("process_executable_mismatch")
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise EngineError("process_lifetime_unavailable")
        return (pid, hwnd, observed_exe, created.dwHighDateTime, created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


class _Child:
    """Bound output in reader threads; a hung provider cannot block the deadline."""
    def __init__(self, command, *, env, cwd, timeout):
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue(maxsize=256)
        self.failure = None
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env, cwd=cwd,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.threads = [threading.Thread(target=self._stdout, daemon=True), threading.Thread(target=self._stderr, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _fail(self, code):
        self.failure = code
        try:
            self.proc.kill()
        except OSError:
            pass

    def _stdout(self):
        try:
            while True:
                line = self.proc.stdout.readline(MAX_OUTPUT_BYTES + 1)
                if not line:
                    break
                self.stdout_bytes += len(line)
                if self.stdout_bytes > MAX_OUTPUT_BYTES or len(line) > MAX_OUTPUT_BYTES:
                    self._fail("stdout_limit_exceeded")
                    break
                try:
                    self.messages.put_nowait(line)
                except queue.Full:
                    self._fail("message_limit_exceeded")
                    break
        except (OSError, ValueError):
            self.failure = self.failure or "stdout_read_failed"

    def _stderr(self):
        try:
            while True:
                data = self.proc.stderr.read(4096)
                if not data:
                    break
                self.stderr_bytes += len(data)
                if self.stderr_bytes > MAX_STDERR_BYTES:
                    self._fail("stderr_limit_exceeded")
                    break
        except (OSError, ValueError):
            self.failure = self.failure or "stderr_read_failed"

    def receive(self):
        while True:
            if self.failure:
                raise EngineError(self.failure)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise EngineError("observation_timeout")
            try:
                line = self.messages.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if self.proc.poll() is not None and not self.threads[0].is_alive() and self.messages.empty():
                    raise EngineError("child_exited_without_response")
                continue
            try:
                value = json.loads(line.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (UnicodeError, ValueError):
                raise EngineError("invalid_json_response") from None
            if not isinstance(value, dict):
                raise EngineError("invalid_response_shape")
            return value

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            raise EngineError("child_cleanup_unconfirmed") from None
        finally:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            for thread in self.threads:
                thread.join(timeout=0.5)


def validate_local_directory(value):
    """Require an existing local fixed-drive directory, without reparse ancestors."""
    if os.name != "nt":
        raise EngineError("windows_required")
    text = os.fspath(value)
    if text.startswith(("\\\\", "//")) or not Path(text).is_absolute():
        raise EngineError("local_absolute_directory_required")
    path = Path(os.path.abspath(text))
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if kernel.GetDriveTypeW(path.anchor) != 3:
        raise EngineError("local_fixed_drive_required")
    try:
        for part in [path, *path.parents]:
            metadata = part.lstat()
            if (part.is_symlink() or getattr(metadata, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                raise EngineError("reparse_directory_rejected")
        if not path.is_dir():
            raise EngineError("existing_output_directory_required")
    except OSError:
        raise EngineError("existing_output_directory_required") from None
    return path


def process_path(pid):
    _integer(pid, 1, 0xFFFFFFFF, "invalid_pid")
    if os.name != "nt":
        raise EngineError("windows_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise EngineError("process_identity_unavailable")
    try:
        name, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise EngineError("process_identity_unavailable")
        return name.value
    finally:
        kernel.CloseHandle(handle)


def foreground():
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    return int(user.GetForegroundWindow() or 0)
