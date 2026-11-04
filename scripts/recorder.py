"""Scoped Windows recording in Python source, with no generated executable.

The hook thread observes primitive input events only. A separate UIA worker owns
all COM objects; the Tk thread displays serializable snapshots. Nothing persists
outside the explicitly selected local recording directory.
"""
from __future__ import annotations

import copy
import ctypes
from ctypes import wintypes
import datetime
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

MAX_STEPS = 300
MAX_VALUE_BYTES = 10000


class RecorderError(RuntimeError):
    pass


def _clip(text, size=500):
    return str(text).encode('utf-8')[:size].decode('utf-8', errors='ignore')


def _inside(bounds, x, y):
    try:
        values = [bounds[k] for k in ('x', 'y', 'width', 'height')]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            return False
        bx, by, width, height = values
        return width > 0 and height > 0 and bx <= x < bx + width and by <= y < by + height
    except (KeyError, TypeError):
        return False


def _locator_valid(locator):
    if not isinstance(locator, dict) or not (locator.get('automation_id') or locator.get('name')):
        return False
    if set(locator) - {'automation_id', 'name', 'role', 'ancestors'}:
        return False
    for key in ('automation_id', 'name', 'role'):
        if key in locator and (not isinstance(locator[key], str) or not locator[key]
                               or '\0' in locator[key] or len(locator[key].encode('utf-8')) > 1024):
            return False
    ancestors = locator.get('ancestors', [])
    return (isinstance(ancestors, list) and len(ancestors) <= 16
            and all(isinstance(item, dict) and not item.get('ancestors') and _locator_valid(item)
                    for item in ancestors))


def _double_pair(previous, current, interval, width, height):
    """Use the current Windows double-click interval and rectangle, in pixels."""
    return bool(previous and not previous.get('double')
                and current['action'] == previous['action'] == 'click'
                and current['window'] == previous['window']
                and current['hit_window'] == previous['hit_window']
                and 0 <= current['at'] - previous['at'] <= interval
                and abs(current['x'] - previous['x']) <= width / 2
                and abs(current['y'] - previous['y']) <= height / 2)


def _abort_recording(pump, worker):
    """Fail closed even when a UI callback or hook teardown raises."""
    try:
        if pump:
            pump.stop()
    except Exception:
        pass
    worker.enabled.clear()
    worker.abort.set()
    return {'ok': False, 'status': 'blocked', 'diagnostic': 'recording_ui_failed',
            'input_dispatched': False}


class RecordingModel:
    """Pure data model. Each update is appended; draft replaces never erase events."""
    def __init__(self, request, target_info):
        engine = Path(__file__).resolve().parents[1] / 'engine'
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        from local_core import validate_local_directory
        self._validate_directory = validate_local_directory
        self.output = validate_local_directory(Path(request['output_dir']))
        self.request = copy.deepcopy(request)
        self.target_info = dict(target_info)
        self.progress = self._path(request['progress_path'])
        self.draft = self._path(request['draft_path'])
        self.stop_path = self._path(request['stop_path']) if request.get('stop_path') else None
        self.events_path = self.output / ('recording-' + uuid.uuid4().hex + '.events.jsonl')
        self.steps = []
        self.lock = threading.RLock()
        self.state = 'ready'
        self.partial = False
        self.diagnostic = None
        self.hover_target = None
        self.closed = False

    def _path(self, value):
        path = Path(value)
        if not path.is_absolute() or path.parent != self.output or ':' in path.name:
            raise RecorderError('recording_path_outside_output')
        self._validate_directory(self.output)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or not path.is_file():
                raise RecorderError('invalid_recording_file')
        return path

    def write(self, path, data):
        self._path(path)
        temporary = self.output / ('.recording-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def set_state(self, state, diagnostic=None, partial=False):
        with self.lock:
            self.state = state
            self.diagnostic = diagnostic
            self.partial = self.partial or partial

    def add(self, step):
        step = copy.deepcopy(step)
        with self.lock:
            if self.closed:
                return
            index = next((i for i, old in enumerate(self.steps) if old['id'] == step['id']), None)
            if index is None and len(self.steps) >= MAX_STEPS:
                self.state, self.partial, self.diagnostic = 'paused', True, 'recording_step_limit'
                return
            event = {'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     'kind': 'step_revision' if index is not None else 'step_recorded', 'step': step}
            self._path(self.events_path)
            with self.events_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            if index is None:
                self.steps.append(step)
            else:
                self.steps[index] = step

    def manual(self, label, reason):
        self.add({'id': uuid.uuid4().hex, 'label': _clip(label), 'action': 'manual',
                  'target': {'kind': 'manual', 'locator': {}}, 'expect': {'kind': 'manual'},
                  'recognition': {'target': 'unrecognized', 'action': 'manual',
                                  'result': 'manual', 'reason': reason}})

    def snapshot(self):
        with self.lock:
            warnings = sum(step['recognition'].get('action') != 'supported'
                           or step['recognition'].get('result') == 'manual' for step in self.steps)
            data = {'state': self.state, 'event_count': len(self.steps),
                    'warning_count': warnings, 'partial': self.partial}
            if self.steps:
                data['last_step'] = copy.deepcopy(self.steps[-1])
            if self.hover_target:
                data['hover_target'] = copy.deepcopy(self.hover_target)
            if self.diagnostic:
                data['diagnostic'] = self.diagnostic
            return data

    def finish(self, cancelled=False):
        with self.lock:
            if self.closed:
                raise RecorderError('recording_already_finished')
            if not cancelled and not self.steps:
                self.manual('기록된 동작 확인 필요', 'empty_recording_requires_review')
            self.state = 'cancelled' if cancelled else 'review'
            snapshot = self.snapshot()
            self.write(self.progress, snapshot)
            if cancelled:
                self.closed = True
                return {'ok': True, 'status': 'cancelled', 'event_count': len(self.steps)}
            workflow = {'schema_version': 1, 'id': uuid.uuid4().hex,
                        'name': _clip(self.request.get('name', '새 녹화 작업'), 300), 'revision': 1,
                        'target': {key: self.target_info[key] for key in ('exe_name', 'title', 'class_name')},
                        'parameters': {}, 'steps': copy.deepcopy(self.steps)}
            self.write(self.draft, workflow)
            self.closed = True
            return {'ok': True, 'status': 'recorded', 'draft_path': str(self.draft),
                    'recording_path': str(self.events_path), 'event_count': len(self.steps),
                    'warning_count': snapshot['warning_count'], 'partial': self.partial}


class HookPump:
    """Low-level hooks live only for one explicit recording interval."""
    def __init__(self, binding, bridge, events, notices):
        self.binding, self.bridge = binding, bridge
        self.events, self.notices = events, notices
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.enabled = threading.Event()
        self.thread = None
        self.thread_id = 0
        self.error = None
        self.pending = {}
        self.sequence = 0
        self.last_click = None

    def _emit(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.enabled.clear()
            self.notices.put(('fatal', 'recording_queue_full'))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.enabled.set()
        self.thread = threading.Thread(target=self._run, name='WorkflowInputHooks', daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            self.stop()
            raise RecorderError(self.error or 'hook_start_timeout')

    def stop(self):
        self.enabled.clear()
        if self.thread_id:
            try:
                self._user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            except (AttributeError, OSError):
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)

    def _run(self):
        mouse_hook = keyboard_hook = None
        try:
            user = ctypes.WinDLL('user32', use_last_error=True)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self._user = user
            LRESULT = ctypes.c_ssize_t
            HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
            class Point(ctypes.Structure):
                _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]
            class Mouse(ctypes.Structure):
                _fields_ = [('point', Point), ('data', wintypes.DWORD), ('flags', wintypes.DWORD),
                            ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
            class Keyboard(ctypes.Structure):
                _fields_ = [('code', wintypes.DWORD), ('scan', wintypes.DWORD), ('flags', wintypes.DWORD),
                            ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
            user.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
            user.SetWindowsHookExW.restype = wintypes.HANDLE
            user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
            user.UnhookWindowsHookEx.restype = wintypes.BOOL
            user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user.CallNextHookEx.restype = LRESULT
            user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user.GetMessageW.restype = wintypes.BOOL
            user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user.PeekMessageW.restype = wintypes.BOOL
            user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            user.DispatchMessageW.restype = LRESULT
            user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user.PostThreadMessageW.restype = wintypes.BOOL
            user.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user.GetAsyncKeyState.restype = ctypes.c_short
            user.GetDoubleClickTime.argtypes = []
            user.GetDoubleClickTime.restype = wintypes.UINT
            user.GetSystemMetrics.argtypes = [ctypes.c_int]
            user.GetSystemMetrics.restype = ctypes.c_int
            kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel.GetModuleHandleW.restype = wintypes.HMODULE
            kernel.GetCurrentThreadId.restype = wintypes.DWORD
            self.thread_id = kernel.GetCurrentThreadId()
            message = wintypes.MSG()
            user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)

            @HOOKPROC
            def mouse_callback(code, message, data):
                try:
                    if code >= 0 and self.enabled.is_set():
                        m = ctypes.cast(data, ctypes.POINTER(Mouse)).contents
                        x, y = int(m.point.x), int(m.point.y)
                        hit = self.bridge.window_from_point(x, y)
                        scope = self.binding.contains_window(hit)
                        button = 'right' if message in (0x204, 0x205) else 'left'
                        if message in (0x202, 0x205) and button in self.pending:
                            previous = self.pending.pop(button)
                            if scope and abs(x - previous['x']) <= 8 and abs(y - previous['y']) <= 8:
                                self.last_click = previous
                            else:
                                self.last_click = None
                            self._emit({'kind': 'up', 'token': previous['token'], 'x': x, 'y': y, 'inside': scope,
                                        'at': time.monotonic()})
                        elif not scope:
                            if message in (0x201, 0x204, 0x20A, 0x20E):
                                self.last_click = None
                                self.notices.put(('outside', None))
                        elif message in (0x201, 0x204):
                            self.sequence += 1
                            event = {'kind': 'down', 'token': self.sequence, 'x': x, 'y': y,
                                     'action': 'right-click' if button == 'right' else 'click',
                                     'window': self.bridge.root_window(hit), 'hit_window': hit,
                                     'at': time.monotonic()}
                            if _double_pair(self.last_click, event, user.GetDoubleClickTime() / 1000,
                                            user.GetSystemMetrics(36), user.GetSystemMetrics(37)):
                                event.update(double=True, previous_token=self.last_click['token'])
                            self.pending[button] = event
                            self._emit(event)
                        elif message in (0x20A, 0x20E):
                            self.last_click = None
                            self._emit({'kind': 'scroll', 'at': time.monotonic()})
                except Exception:
                    self.notices.put(('fatal', 'input_observation_failed'))
                return user.CallNextHookEx(mouse_hook, code, message, data)

            @HOOKPROC
            def keyboard_callback(code, message, data):
                try:
                    if (code >= 0 and self.enabled.is_set() and message in (0x100, 0x104)
                            and self.binding.contains_window(self.bridge.foreground())):
                        key = ctypes.cast(data, ctypes.POINTER(Keyboard)).contents
                        # Character codes are never sent to the worker or persisted.
                        control = bool(user.GetAsyncKeyState(0x11) & 0x8000)
                        alternate = any(user.GetAsyncKeyState(k) & 0x8000 for k in (0x12, 0x5b, 0x5c))
                        if key.code in (16, 17, 18, 0x5b, 0x5c, 0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5):
                            return user.CallNextHookEx(keyboard_hook, code, message, data)
                        kind = ('escape' if key.code == 27 else 'enter' if key.code == 13 else
                                'tab' if key.code == 9 else 'shortcut' if alternate or (control and key.code not in (65, 86, 88))
                                or key.code in range(0x21, 0x29) or key.code in range(0x70, 0x88)
                                else 'typing')
                        self._emit({'kind': kind, 'at': time.monotonic()})
                except Exception:
                    self.notices.put(('fatal', 'input_observation_failed'))
                return user.CallNextHookEx(keyboard_hook, code, message, data)

            self._callbacks = (mouse_callback, keyboard_callback)
            module = kernel.GetModuleHandleW(None)
            mouse_hook = user.SetWindowsHookExW(14, mouse_callback, module, 0)
            keyboard_hook = user.SetWindowsHookExW(13, keyboard_callback, module, 0)
            if not mouse_hook or not keyboard_hook:
                raise RecorderError('hook_unavailable')
            self.ready.set()
            while self.enabled.is_set():
                status = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status <= 0:
                    break
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as error:
            self.error = str(error) if isinstance(error, RecorderError) else 'hook_unavailable'
        finally:
            if mouse_hook:
                user.UnhookWindowsHookEx(mouse_hook)
            if keyboard_hook:
                user.UnhookWindowsHookEx(keyboard_hook)
            self.enabled.clear()
            self.closed.set()
            self.ready.set()


class CaptureWorker(threading.Thread):
    def __init__(self, binding, bridge, model, events, notices):
        super().__init__(name='WorkflowUIARecorder', daemon=True)
        self.binding, self.bridge, self.model = binding, bridge, model
        self.events, self.notices = events, notices
        self.enabled = threading.Event()
        self.finished = threading.Event()
        self.abort = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.busy_since = None
        self.watched = None
        self.watched_value = None
        self.watched_step = None
        self.hovered = None
        self.hover_at = 0
        self.last_key = 0
        self.last_watch = 0
        self.last_hover = 0
        self.last_identity = 0
        self.captures = {}
        self.invalidated = False
        self.last_unreadable_input = 0
        self.recent_click = None

    def check_binding(self):
        if self.invalidated:
            return False
        try:
            self.binding.check()
            return True
        except Exception:
            self.invalidated = True
            self.enabled.clear()
            self.watched = self.watched_step = self.hovered = None
            self.captures.clear()
            self.notices.put(('fatal', 'target_lifetime_changed'))
            return False

    def run(self):
        try:
            with self.bridge.UIA() as self.uia:
                self.ready.set()
                while not self.abort.is_set():
                    try:
                        event = self.events.get(timeout=.08)
                    except queue.Empty:
                        event = None
                    self.busy_since = time.monotonic()
                    try:
                        if event:
                            if event['kind'] == 'finish':
                                if self.check_binding():
                                    self.finalize_watch(True)
                                if self.captures:
                                    self.model.manual('끝나지 않은 마우스 동작', 'pending_capture_requires_review')
                                break
                            self.handle(event)
                        if self.enabled.is_set():
                            self.tick()
                    except Exception:
                        self.model.set_state(self.model.state, partial=True)
                        self.model.manual('요소를 읽지 못한 동작', 'accessibility_unavailable')
                    finally:
                        self.busy_since = None
                self.watched = self.hovered = None
                self.captures.clear()
        except Exception:
            self.error = 'accessibility_worker_unavailable'
            self.notices.put(('fatal', self.error))
        finally:
            self.ready.set()
            self.finished.set()

    def observe_down(self, event):
        x, y = event['x'], event['y']
        cached = self.hovered
        if (cached and cached['window'] == event['window'] and 0 <= event['at'] - self.hover_at < 1.2
                and _inside(cached['node'].get('bounds'), x, y)):
            return cached
        try:
            if self.bridge.window_from_point(x, y) == event['hit_window']:
                return self.uia.observe_point(self.binding, x, y, event['window'])
        except Exception:
            pass
        hit, window = event['hit_window'], event['window']
        if hit == window or self.bridge.hwnd_pid(hit) != self.binding.pid or not self.binding.contains_window(hit):
            return None
        self.binding.check()
        root, element = self.uia.from_handle(window), self.uia.from_handle(hit)
        if not self.uia.within(element, root):
            return None
        node = self.uia.node(element, root)
        if node.get('role') == 'window' or not _inside(node.get('bounds'), x, y):
            return None
        return {'element': element, 'root': root, 'window': window, 'node': node}

    def target(self, observation, kind, point=None):
        node = observation['node']
        locator = node.get('locator')
        if not _locator_valid(locator):
            raise RecorderError('stable_locator_unavailable')
        target = {'kind': kind, 'locator': copy.deepcopy(locator)}
        if observation['window'] != self.binding.hwnd:
            target['window'] = {'title': self.bridge.window_text(observation['window']),
                                'class_name': self.bridge.window_class(observation['window'])}
        if point:
            bounds = node['bounds']
            if not _inside(bounds, *point) or bounds['width'] < 4 or bounds['height'] < 4:
                raise RecorderError('bounds_unavailable')
            xr = (point[0] - bounds['x']) / bounds['width']
            yr = (point[1] - bounds['y']) / bounds['height']
            if not .02 <= xr <= .98 or not .02 <= yr <= .98:
                raise RecorderError('region_edge_requires_retarget')
            target.update(point={'x_ratio': xr, 'y_ratio': yr}, recorded_bounds=copy.deepcopy(bounds))
        return target

    def step(self, action, observation, kind='element', point=None):
        node = observation['node']
        menu = node.get('role') == 'menuitem'
        name = node.get('name') or node.get('automation_id') or node.get('role') or '동작'
        return {'id': uuid.uuid4().hex, 'label': _clip(name + ' / ' + action), 'action': action,
                'target': self.target(observation, kind, point), 'headed': kind == 'region',
                'expect': {'kind': 'none' if menu else 'manual'},
                'recognition': {'target': 'identified' if kind == 'element' else 'region_identified',
                                'action': 'supported' if kind == 'element' else 'region_review_required',
                                'result': 'delivery_only' if menu else 'manual',
                                'reason': ('business_completion_not_verified' if menu else 'completion_condition_required')
                                          if kind == 'element' else 'recorded_region_requires_review'}}

    def watch(self, observation):
        if self.uia.secure(observation['element']):
            self.finalize_watch(True)
            self.model.manual('보호된 입력', 'protected_input_not_recorded')
            return
        value = self.uia.value(observation['element'])
        if value is None:
            return
        if self.watched and self.uia.same(self.watched['element'], observation['element']):
            return
        self.finalize_watch(True)
        self.watched, self.watched_value, self.watched_step = observation, value, None

    def finalize_watch(self, force):
        observation = self.watched
        if not observation:
            return
        try:
            if self.uia.secure(observation['element']):
                self.model.manual('보호된 입력', 'protected_input_not_recorded')
                self.watched = None
                return
            current = self.uia.value(observation['element'])
        except Exception:
            if force:
                self.model.manual('입력 결과를 읽지 못함', 'value_readback_unavailable')
                self.watched = None
            return
        if not isinstance(current, str) or '\0' in current or len(current.encode('utf-8')) > MAX_VALUE_BYTES:
            if force:
                self.model.manual('입력 결과 확인 필요', 'value_readback_unavailable')
                self.watched = None
            return
        if current != self.watched_value or force:
            try:
                step = self.step('set-value', observation)
            except RecorderError:
                self.model.manual('입력 대상 식별 필요', 'stable_locator_unavailable')
                self.watched = None
                return
            step.update(value=current, expect={'kind': 'value', 'value': current},
                        recognition={'target': 'identified', 'action': 'supported', 'result': 'checkable'})
            if self.watched_step:
                step['id'] = self.watched_step['id']
            self.model.add(step)
            self.watched_step, self.watched_value = step, current
        if force:
            self.watched = self.watched_step = None

    def capture_action(self, event, observation):
        if observation is None:
            self.model.manual('대상을 찾지 못한 클릭', 'capture_target_unavailable')
            return
        node = observation['node']
        if event['action'] == 'click' and node.get('role') == 'listitem' and self.watched:
            self.finalize_watch(True)
            return
        if self.uia.secure(observation['element']):
            self.finalize_watch(True)
            self.model.manual('보호된 입력', 'protected_input_not_recorded')
            return
        if not _locator_valid(node.get('locator')):
            self.finalize_watch(True)
            self.model.manual('이름 없는 요소 클릭', 'stable_locator_unavailable')
            return
        actions = node.get('available_actions', [])
        if event['action'] == 'click' and 'SetValue' in actions:
            self.watch(observation)
            return
        self.finalize_watch(True)
        if event['action'] == 'click' and 'Check' in actions:
            checked = self.uia.checked(observation['element'])
            if type(checked) is not bool:
                self.model.manual('체크 상태 확인 필요', 'indeterminate_check_state')
                return
            step = self.step('check' if checked else 'uncheck', observation)
            step['expect'] = {'kind': 'checked', 'value': checked}
            step['recognition'] = {'target': 'identified', 'action': 'supported', 'result': 'checkable'}
        elif event['action'] == 'click' and 'Click' in actions:
            step = self.step('click', observation)
        elif event['action'] == 'click' and 'Select' in actions:
            step = self.step('select', observation)
            step['value'] = node.get('name', '')
        elif node.get('role') != 'window':
            try:
                step = self.step(event['action'], observation, 'region', (event['x'], event['y']))
            except RecorderError as error:
                self.model.manual('영역 위치 보완 필요', str(error))
                return
        else:
            self.model.manual('창 배경 클릭', 'window_root_not_replay_target')
            return
        self.model.add(step)
        return step['id']

    def capture_double(self, down, observation):
        previous = self.recent_click
        if not down.get('double') or not previous or previous['token'] != down.get('previous_token'):
            return False
        try:
            if observation and previous['observation'] and not self.uia.same(
                    observation['element'], previous['observation']['element']):
                return False
        except Exception:
            pass  # A vanished target cannot justify leaving its first click replayable.
        step_id = previous['step_id']
        if self.watched and previous['observation']:
            try:
                same = self.uia.same(self.watched['element'], previous['observation']['element'])
            except Exception:
                same = True
            if same:
                if self.watched_step and self.watched_step['id'] != previous['watched_step_id']:
                    step_id = self.watched_step['id']
                self.watched = self.watched_step = None
        step = {'id': step_id or uuid.uuid4().hex, 'label': '더블클릭 동작 보완 필요', 'action': 'manual',
                'target': {'kind': 'manual', 'locator': {}}, 'expect': {'kind': 'manual'},
                'recognition': {'target': 'unrecognized', 'action': 'manual', 'result': 'manual',
                                'reason': 'double_click_needs_review'}}
        self.model.add(step)
        self.recent_click = None
        return True

    def handle(self, event):
        kind = event['kind']
        if not self.check_binding():
            return
        if kind == 'down':
            if self.enabled.is_set():
                self.model.set_state('recording')
            self.captures[event['token']] = (event, self.observe_down(event))
        elif kind == 'up':
            down, observation = self.captures.pop(event['token'], (None, None))
            if not down:
                self.model.manual('시작 지점이 없는 클릭', 'capture_target_unavailable')
            elif not event['inside'] or abs(event['x'] - down['x']) > 8 or abs(event['y'] - down['y']) > 8:
                self.finalize_watch(True)
                self.model.manual('드래그 또는 영역 밖 이동', 'drag_needs_review')
            else:
                time.sleep(.12)
                if not self.capture_double(down, observation):
                    step_id = self.capture_action(down, observation)
                    self.recent_click = {'token': down['token'], 'observation': observation, 'step_id': step_id,
                                         'watched_step_id': self.watched_step['id'] if self.watched_step else None}
        elif kind == 'typing':
            self.last_key = event['at']
            if not self.watched:
                window = self.bridge.root_window(self.bridge.foreground())
                if self.binding.contains_window(window):
                    root, element = self.uia.from_handle(window), self.uia.focused()
                    if element and self.uia.within(element, root):
                        self.watch({'element': element, 'root': root, 'window': window,
                                    'node': self.uia.node(element, root)})
            if not self.watched and event['at'] - self.last_unreadable_input > .8:
                self.last_unreadable_input = event['at']
                self.model.manual('입력 대상 확인 필요', 'input_value_unavailable')
        elif kind in {'scroll', 'enter', 'tab', 'escape', 'shortcut', 'pause'}:
            self.finalize_watch(True)
            labels = {'scroll': ('스크롤 동작', 'scroll_needs_review'),
                      'enter': ('Enter 확정 동작', 'keyboard_transition_needs_review'),
                      'tab': ('Tab 이동 동작', 'keyboard_transition_needs_review'),
                      'escape': ('Escape 동작과 일시정지', 'keyboard_transition_needs_review'),
                      'shortcut': ('단축키 동작', 'shortcut_needs_review')}
            if kind in labels:
                self.model.manual(*labels[kind])
            if kind == 'escape':
                self.enabled.clear()
                self.notices.put(('pause', None))

    def tick(self):
        now = time.monotonic()
        if now - self.last_identity > .5:
            self.last_identity = now
            if not self.check_binding():
                return
        if now - self.last_hover > .2:
            self.last_hover = now
            x, y = self.bridge.cursor()
            hit = self.bridge.window_from_point(x, y)
            if self.binding.contains_window(hit):
                try:
                    observation = self.uia.observe_point(self.binding, x, y, self.bridge.root_window(hit))
                    self.hovered, self.hover_at = observation, time.monotonic()
                    with self.model.lock:
                        self.model.hover_target = copy.deepcopy(observation['node'].get('locator'))
                except Exception:
                    self.hovered = None
            else:
                self.hovered = None
                with self.model.lock:
                    self.model.hover_target = None
        if self.watched and now - self.last_watch > .6 and now - self.last_key > .5:
            self.last_watch = now
            self.finalize_watch(False)


def record(request):
    """Run one visible recorder window and return the existing local protocol."""
    import tkinter as tk
    from tkinter import ttk
    import win_bridge as bridge
    bridge.set_dpi_awareness()
    binding = bridge.Binding(request)
    model = RecordingModel(request, binding.info())
    events, notices = queue.Queue(maxsize=128), queue.Queue()
    worker = CaptureWorker(binding, bridge, model, events, notices)
    worker.start()
    if not worker.ready.wait(3) or worker.error:
        worker.abort.set()
        raise RecorderError('accessibility_worker_unavailable')
    root = tk.Tk()
    root.title('Agent Desktop Workflows / 작업 녹화')
    root.geometry(f"760x440+{max(0, root.winfo_screenwidth() - 790)}+{max(0, root.winfo_screenheight() - 500)}")
    root.minsize(660, 350)
    root.configure(background='#edf3f7')
    root.attributes('-topmost', True)
    style = ttk.Style(root)
    style.configure('Recorder.Treeview', rowheight=29, font=('맑은 고딕', 9))
    header = tk.StringVar(value='대상: ' + _clip(model.target_info.get('title') or binding.exe, 160))
    detail = tk.StringVar(value='시작 후 평소처럼 작업하세요. 인식되지 않은 동작은 보완 단계로 남깁니다.\n녹화 중 실제 프로그램의 값과 상태가 변경됩니다.')
    ttk.Label(root, textvariable=header, padding=(14, 10), font=('맑은 고딕', 11, 'bold')).pack(fill='x')
    ttk.Label(root, textvariable=detail, padding=(14, 3, 14, 8), wraplength=710).pack(fill='x')
    controls = ttk.Frame(root, padding=10)
    controls.pack(side='bottom', fill='x')
    frame = ttk.Frame(root, padding=(12, 0, 12, 0))
    frame.pack(fill='both', expand=True)
    listing = ttk.Treeview(frame, columns=('step', 'name', 'target', 'action', 'result'), show='headings', style='Recorder.Treeview', height=6)
    for key, title, width in [('step', '단계', 45), ('name', '동작 / 대상', 235), ('target', '대상 인식', 120), ('action', '조작', 110), ('result', '결과 확인', 125)]:
        listing.heading(key, text=title)
        listing.column(key, width=width, minwidth=40, stretch=key == 'name')
    listing.tag_configure('warning', foreground='#9a5700')
    scrollbar = ttk.Scrollbar(frame, orient='vertical', command=listing.yview)
    listing.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y')
    listing.pack(fill='both', expand=True)
    result = None
    pump = None
    finishing = False
    finish_at = None
    cancelled = False
    rendered = None

    def enqueue(event):
        try:
            events.put_nowait(event)
        except queue.Full:
            notices.put(('fatal', 'recording_queue_full'))

    def start():
        nonlocal pump
        if finishing or worker.enabled.is_set():
            return
        try:
            binding.check()
            pump = HookPump(binding, bridge, events, notices)
            pump.start()
            worker.enabled.set()
            model.set_state('recording')
            start_button.configure(state='disabled')
            pause_button.configure(state='normal')
            bridge.activate(dict(request, process_started_utc_ticks=binding.created))
            detail.set('기록 중입니다. 대상 창에서 작업하세요. 보완이 필요한 단계는 아래에 표시됩니다.')
        except Exception:
            if pump:
                pump.stop()
            worker.enabled.clear()
            model.set_state('failed', 'recording_start_failed', partial=True)
            detail.set('녹화를 시작하지 못했습니다. 대상 창과 현재 사용자 세션을 확인하세요.')

    def pause():
        if pump:
            pump.stop()
        worker.enabled.clear()
        if not finishing:
            model.set_state('paused')
            enqueue({'kind': 'pause'})
            start_button.configure(state='normal')
            pause_button.configure(state='disabled')
            detail.set('일시정지했습니다. 재시작 후 입력 대상을 클릭하세요.')

    def finish(cancel=False):
        nonlocal finishing, finish_at, cancelled
        if finishing:
            return
        pause()
        finishing, cancelled, finish_at = True, cancel, time.monotonic()
        start_button.configure(state='disabled')
        pause_button.configure(state='disabled')
        finish_button.configure(state='disabled')
        cancel_button.configure(state='disabled')
        enqueue({'kind': 'finish'})
        detail.set('기록을 정리하고 있습니다. 응답이 없는 마지막 단계는 보완 대상으로 남깁니다.')

    def render(snapshot):
        nonlocal rendered
        with model.lock:
            steps = copy.deepcopy(model.steps)
        signature = json.dumps(steps, ensure_ascii=True, sort_keys=True)
        if signature == rendered:
            return
        rendered = signature
        listing.delete(*listing.get_children())
        for index, step in enumerate(steps):
            rec = step['recognition']
            target = '요소 확인' if rec['target'] == 'identified' else '영역 확인' if rec['target'] == 'region_identified' else '보완 필요'
            action = '지원' if rec['action'] == 'supported' else '검토 필요'
            expected = '값 확인 가능' if rec['result'] == 'checkable' else '전달 확인' if rec['result'] == 'delivery_only' else '완료 조건 필요'
            tags = ('warning',) if action != '지원' or rec['result'] == 'manual' else ()
            listing.insert('', 'end', iid=step['id'], values=(index + 1, step['label'], target, action, expected), tags=tags)
        if steps:
            listing.see(steps[-1]['id'])
            if not finishing:
                detail.set(f"{len(steps)}단계: {steps[-1]['label']}\n이 표시는 재생 준비 상태입니다. 실제 업무 완료는 별도로 확인합니다.")

    def tick_body():
        nonlocal result
        try:
            while True:
                kind, diagnostic = notices.get_nowait()
                if kind in {'pause', 'fatal'}:
                    pause()
                    if kind == 'fatal':
                        model.set_state('failed', diagnostic, partial=True)
                        model.manual('기록 확인 필요', diagnostic)
                        detail.set('기록을 계속할 수 없어 일시정지했습니다. 현재 단계와 대상 창을 확인하세요.')
                elif kind == 'outside' and worker.enabled.is_set():
                    model.set_state('outside_target')
        except queue.Empty:
            pass
        if model.stop_path and model.stop_path.exists() and not finishing:
            finish(False)
        if len(model.steps) >= MAX_STEPS and worker.enabled.is_set():
            pause()
            model.set_state('paused', 'recording_step_limit', partial=True)
        if finishing and (worker.finished.is_set() or time.monotonic() - finish_at > 2.5):
            if not worker.finished.is_set():
                model.set_state('review', 'pending_capture_requires_review', partial=True)
                model.manual('분석이 끝나지 않은 마지막 동작', 'pending_capture_requires_review')
                worker.abort.set()
            try:
                result = model.finish(cancelled)
            except Exception:
                result = {'ok': False, 'status': 'blocked', 'diagnostic': 'recording_write_failed',
                          'input_dispatched': False}
            root.destroy()
            return
        snapshot = model.snapshot()
        if worker.busy_since and time.monotonic() - worker.busy_since > 2:
            snapshot['diagnostic'] = 'accessibility_read_delayed'
            detail.set('현재 요소 응답을 기다립니다. 늦은 단계는 보완 대상으로 남깁니다.')
        state_labels = {'ready': '준비', 'recording': '녹화 중', 'paused': '일시정지', 'outside_target': '대상 창 밖', 'failed': '확인 필요'}
        header.set(f"{state_labels.get(snapshot['state'], snapshot['state'])} / {snapshot['event_count']}단계 / 보완 {snapshot['warning_count']}개")
        render(snapshot)
        try:
            model.write(model.progress, snapshot)
        except Exception:
            if worker.enabled.is_set():
                pause()
            model.set_state('failed', 'recording_write_failed', partial=True)
            detail.set('진행 상태를 저장하지 못해 녹화를 일시정지했습니다.')
        root.after(200, tick)

    def tick():
        nonlocal result
        try:
            tick_body()
        except Exception:
            result = _abort_recording(pump, worker)
            root.destroy()

    start_button = ttk.Button(controls, text='녹화 시작', command=start)
    pause_button = ttk.Button(controls, text='일시정지', command=pause, state='disabled')
    finish_button = ttk.Button(controls, text='마치고 검토', command=finish)
    cancel_button = ttk.Button(controls, text='취소', command=lambda: finish(True))
    for button in (start_button, pause_button, finish_button, cancel_button):
        button.pack(side='left', padx=4, ipady=3)
    root.protocol('WM_DELETE_WINDOW', finish)
    root.after(100, tick)
    if request.get('auto_start') is True:
        root.after(120, start)
    try:
        root.mainloop()
        return result or {'ok': False, 'status': 'cancelled', 'input_dispatched': False}
    finally:
        if pump:
            pump.stop()
        worker.enabled.clear()
        worker.abort.set()
        worker.join(timeout=.5)
