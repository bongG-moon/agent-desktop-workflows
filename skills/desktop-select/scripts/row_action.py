"""Replay a local row demonstration and read back that row's expected state."""
from __future__ import annotations

import time
import ctypes as C
from ctypes import wintypes as W

import win_bridge as bridge


def _result(status, sent, state, *, diagnostic=None, before=None):
    result = {'status': status, 'input_dispatched': sent, 'observed_state': state,
              'effect_verification': 'matched' if status == 'verified' else 'unverified',
              'verification_scope': 'recorded_row_state'}
    if before:
        result['initial_state'] = before
    if diagnostic:
        result['diagnostic'] = diagnostic
    return result


class _LastInput(C.Structure):
    _fields_ = [('size', W.UINT), ('tick', W.DWORD)]


def _input_tick():
    info = _LastInput(C.sizeof(_LastInput), 0)
    read = bridge._sig(bridge.user32, 'GetLastInputInfo', W.BOOL, C.POINTER(_LastInput))
    if not read(C.byref(info)):
        raise bridge.BridgeError('physical_input_state_unavailable')
    return info.tick


def _idle_foreground(original, tick):
    if (bridge.foreground() != original or _input_tick() != tick
            or any(bridge.user32.GetAsyncKeyState(key) & 0x8000 for key in range(1, 255))):
        raise bridge.BridgeError('foreground_or_input_changed')


def foreground(binding):
    original = bridge.foreground()
    if bridge.root_window(original) == binding.hwnd and not bridge.user32.IsIconic(binding.hwnd):
        return
    tick = _input_tick()
    _idle_foreground(original, tick)
    activated = bridge.activate({'hwnd': binding.hwnd, 'pid': binding.pid, 'exe_name': binding.exe,
                                 'process_started_utc_ticks': binding.created})
    if activated.get('foreground_verified'):
        return
    binding.check()
    _idle_foreground(original, tick)
    user = bridge.user32
    attached = bridge._sig(user, 'AttachThreadInput', W.BOOL, W.DWORD, W.DWORD, W.BOOL)
    thread = bridge._sig(bridge.kernel32, 'GetCurrentThreadId', W.DWORD)()
    previous = user.GetWindowThreadProcessId(original, None)
    if not previous or thread == previous or not attached(thread, previous, True):
        raise bridge.BridgeError('target_not_foreground')
    try:
        # The previous action can open a foreground window on another UI
        # thread. Share its idle input queue only around this activation.
        binding.check()
        _idle_foreground(original, tick)
        bridge._sig(user, 'BringWindowToTop', W.BOOL, W.HWND)(binding.hwnd)
        user.SetForegroundWindow(binding.hwnd)
    finally:
        if not attached(thread, previous, False):
            raise bridge.BridgeError('focus_queue_detach_failed')
    for _ in range(6):
        binding.check()
        if _input_tick() != tick:
            raise bridge.BridgeError('foreground_or_input_changed')
        active = bridge.foreground()
        if bridge.root_window(active) == binding.hwnd:
            return
        if active != original:
            break
        time.sleep(.04)
    raise bridge.BridgeError('target_not_foreground')


def execute(binding, spec, guard, *, timeout=90):
    from row_vision import confirm_pixels, confirm_point, observe_row
    guard()
    observed = observe_row(binding, spec)
    state = observed['kind']
    if state not in ('before', 'after'):
        return _result('blocked', False, state, diagnostic='row_' + state)
    # The independent second sample avoids treating transient rendering as a
    # confirmed state and binds the input to the same current physical surface.
    if state == 'after':
        time.sleep(.15)
    else:
        foreground(binding)
    guard()
    current = observe_row(binding, spec)
    if current['kind'] != state or current['row_bounds'] != observed['row_bounds']:
        return _result('blocked', False, current['kind'], diagnostic='row_changed_before_action')
    if state == 'after':
        return _result('verified', False, state, before='after')
    if current.get('point') != observed.get('point') or not current.get('point'):
        return _result('blocked', False, state, diagnostic='row_button_changed')
    guard()
    bridge._combo_surface(binding)  # foreground, visibility and physical input idle
    confirm_point(binding, current)
    confirm_pixels(binding, spec, current)
    guard()
    bridge._combo_surface(binding)
    if bridge.root_window(bridge.window_from_point(*current['point'])) != binding.hwnd:
        return _result('blocked', False, state, diagnostic='row_point_not_exposed')
    sent = bridge._send_point(*current['point'], 'click')
    if sent.get('input_dispatched') is not True or not sent.get('ok'):
        return _result('uncertain', sent.get('input_dispatched'), 'unknown',
                       diagnostic=sent.get('diagnostic', 'row_delivery_uncertain'), before='before')

    deadline = time.monotonic() + timeout
    last_state, matches, diagnostic = 'before', 0, 'row_state_not_confirmed'
    while time.monotonic() < deadline:
        try:
            guard()
            last_state = observe_row(binding, spec)['kind']
        except Exception as error:
            # Keep evidence of the already sent input even when the window or
            # provider becomes unavailable during verification.
            code = getattr(error, 'code', 'row_readback_failed')
            diagnostic = code
            if code not in ('row_layout_changed', 'row_window_moved', 'row_pixels_unavailable', 'window_disabled'):
                last_state = 'unknown'
                break
            last_state = 'unknown'
        matches = matches + 1 if last_state == 'after' else 0
        if matches == 2:
            return _result('verified', True, 'after', before='before')
        time.sleep(.35)
    return _result('delivered_unverified', True, last_state, diagnostic=diagnostic, before='before')


def teach_rows(binding, title, args, directory):
    """Terminal counterpart of the graphical recorder for repeated row layouts."""
    import sys
    import learned_tasks as tasks
    from row_vision import teach_after, teach_before, transfer_after

    def message(value):
        print(value, file=sys.stderr, flush=True)

    def check():
        binding.check()
        if bridge.window_text(binding.hwnd) != title:
            raise bridge.BridgeError('business_window_changed')

    tasks.check_new_task(args.task, directory)
    if not 1 <= len(args.row) <= tasks.MAX_STEPS:
        raise tasks.TaskError('invalid_recorded_step_count')
    if any(not tasks._text(label, 200) for label, _, _ in args.row):
        raise tasks.TaskError('invalid_row_step')
    try:
        points = [(int(x), int(y)) for _, x, y in args.row]
    except ValueError as error:
        raise tasks.TaskError('invalid_row_point') from error
    if len(set(points)) != len(points):
        raise tasks.TaskError('duplicate_demonstrated_point')
    drafts = []
    check()
    for point in points:
        check()
        draft = teach_before(binding, point, args.key_box, args.button_box, args.state_box)
        if any(all(other.get(k) == draft.get(k) for k in ('scope', 'role', 'row_id', 'key')) for other in drafts):
            raise tasks.TaskError('duplicate_demonstrated_row')
        drafts.append(draft)
    message(f'첫 행 [{args.row[0][0]}]에서 버튼을 직접 누르고 원하는 상태가 되면 Enter를 누르세요. 취소는 q입니다.')
    while True:
        answer = sys.stdin.readline()
        if not answer or answer.strip():
            return {'status': 'cancelled', 'input_dispatched': False}
        check()
        try:
            example = teach_after(binding, drafts[0])
            break
        except bridge.BridgeError as error:
            message(f'현재 행의 완료 상태를 읽을 수 없습니다 ({error.code}). 처음 표본은 보관 중입니다. '
                    '행과 상태를 확인하고 Enter를 눌러 다시 읽거나 q로 취소하세요.')
    specs = [example] + [transfer_after(draft, example) for draft in drafts[1:]]
    steps = [{'action': 'row-action', 'row': spec, 'label': row[0]} for row, spec in zip(args.row, specs)]
    plan = tasks.from_recording(args.task, binding.exe, title, steps)
    message('행 순서: ' + ' → '.join(step['label'] for step in steps))
    message('각 행의 같은 버튼과 완료 상태를 적용합니다. 저장하려면 SAVE를 입력하세요.')
    if sys.stdin.readline().strip() != 'SAVE':
        return {'status': 'cancelled', 'input_dispatched': False}
    tasks.save_task(plan, directory)
    return {'status': 'recorded', 'task': tasks.summary(plan), 'input_dispatched': False}
