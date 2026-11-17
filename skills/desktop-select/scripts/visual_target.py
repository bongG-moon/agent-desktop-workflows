"""Teach and replay a small local image of an otherwise unnamed click target."""
from __future__ import annotations

import base64
from collections import deque
import ctypes as C
from ctypes import wintypes as W
import re
import threading
import time

import row_vision as vision
import coordination
import win_bridge as bridge


SIDE = 41
_CANVAS = {50000, 50006, 50020, 50025, 50026, 50032, 50033}
_INPUT = {50003, 50004, 50008, 50009, 50010, 50011, 50028, 50029, 50036}


def _require(condition, diagnostic):
    if not condition:
        raise bridge.BridgeError(diagnostic)


def _decoded(spec):
    _require(isinstance(spec, dict) and set(spec) == {'size', 'offset', 'rgb'}, 'invalid_visual_target')
    size, offset, encoded = spec['size'], spec['offset'], spec['rgb']
    _require(type(size) is list and size == [SIDE, SIDE] and all(type(v) is int for v in size)
             and type(offset) is list and len(offset) == 2
             and all(type(v) is int and 0 <= v < SIDE for v in offset)
             and isinstance(encoded, str) and len(encoded) == 4 * SIDE * SIDE, 'invalid_visual_target')
    try:
        pixels = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise bridge.BridgeError('invalid_visual_target') from error
    _require(len(pixels) == 3 * SIDE * SIDE, 'invalid_visual_target')
    return pixels


def _texture(pixels):
    # Two-direction detail prevents a plain background or a divider from
    # pinning a click to a visually arbitrary point along it.
    for stride in (3, 3 * SIDE):
        edges = 0
        for start in range(0, len(pixels) - stride, 3):
            if stride == 3 and start // 3 % SIDE == SIDE - 1:
                continue
            if max(abs(pixels[start+c] - pixels[start+stride+c]) for c in range(3)) >= 25:
                edges += 1
                if edges >= 12:
                    break
        if edges < 12:
            return False
    return True


def validate_spec(spec):
    _require(_texture(_decoded(spec)), 'visual_target_lacks_detail')
    return spec


def _box(window, point):
    _require(len(point) == 2 and all(type(n) is int for n in point), 'invalid_visual_point')
    _require(vision._inside([*point, 1, 1], window) and min(window[2:]) >= SIDE,
             'visual_point_outside_window')
    return [max(window[i], min(point[i] - SIDE//2, window[i] + window[i+2] - SIDE))
            for i in range(2)] + [SIDE, SIDE]


def _anchors(rgb):
    # A short pattern along the most distinctive scanline lets Python's C
    # regexp engine screen the whole window. Confirm candidates using all RGB pixels.
    best = None
    for channel in range(3):
        plane = rgb[channel::3]
        for y in range(0, SIDE):
            row = plane[y*SIDE:(y+1)*SIDE]
            gradient = sum(abs(a-b) for a,b in zip(row, row[1:]))
            score = max(row) - min(row) + gradient/4
            if best is None or score > best[0]:
                best = score, channel, y, row
    _, channel, y, row = best
    selected = sorted(set([0, SIDE-1, row.index(min(row)), row.index(max(row))] +
                          list(range(4, SIDE-1, 5))))
    pieces = []
    last = -1
    for x in selected:
        if x-last > 1:
            pieces.append(b'.{%d}' % (x-last-1))
        value = row[x]
        pieces.append(b'[\\x%02x-\\x%02x]' % (max(0, value-12), min(255, value+12)))
        last = x
    return channel, y, re.compile(b'(?=' + b''.join(pieces) + b')', re.DOTALL)


def _distance(reference, actual):
    # Limit both overall drift and large pixel differences. A changed glyph
    # cannot win solely because its surrounding margins still match.
    total = changed = 0
    for i in range(0, len(reference), 3):
        diff = max(abs(reference[i+c]-actual[i+c]) for c in range(3))
        total += diff
        changed += diff > 24
        if changed > SIDE*SIDE*.025 or total > SIDE*SIDE*4:
            return None
    return total


def _locate(window, pixels, spec):
    rgb = _decoded(spec)
    _require(_texture(rgb), 'visual_target_lacks_detail')
    x0, y0, width, height = window
    _require(width > 0 and height > 0 and len(pixels) == width*height*4, 'visual_frame_unavailable')
    channel, row, pattern = _anchors(rgb)
    plane = pixels[2-channel::4]  # PrintWindow produces BGRA.
    best, first, candidates = None, None, 0
    for found in pattern.finditer(plane):
        y, x = divmod(found.start(), width)
        y -= row
        if y < 0 or x+SIDE > width or y+SIDE > height:
            continue
        candidates += 1
        _require(candidates <= 1024, 'visual_target_too_common')
        box = [x0+x, y0+y, SIDE, SIDE]
        candidate = vision._rgb((window, (), pixels), box)
        distance = 0 if rgb == candidate else _distance(rgb, candidate)
        if distance is None:
            continue
        if first is None:
            first = (x,y)
        _require(max(abs(x-first[0]), abs(y-first[1])) <= 2, 'visual_target_ambiguous')
        if best is None or distance < best[0]:
            best = (distance, box)
    _require(best is not None, 'visual_target_not_found')
    box = best[1]
    return box, [box[i]+spec['offset'][i] for i in range(2)]


def from_frame(window, pixels, screen_point):
    """Build a target from the exact full-resolution BGRA image viewed by the host."""
    box = _box(window, screen_point)
    rgb = vision._rgb((window, (), pixels), box)
    spec = dict(size=[SIDE,SIDE], offset=[screen_point[i]-box[i] for i in range(2)],
                rgb=base64.b64encode(rgb).decode('ascii'))
    _, matched = _locate(window, pixels, spec)
    _require(tuple(matched) == tuple(screen_point), 'visual_target_ambiguous')
    return spec


def native_child(binding, hwnd, pid=None):
    """A real embedded child: owned popups and sibling application windows do not qualify."""
    return (bool(hwnd) and hwnd != binding.hwnd and bridge.root_window(hwnd) == binding.hwnd
            and bool(bridge.user32.IsChild(binding.hwnd, hwnd))
            and (pid is None or bridge.hwnd_pid(hwnd) == pid))


def safe_provider(binding, uia, element):
    if uia.secure(element):
        return False
    pid = uia.pid(element)
    if pid == binding.pid:
        return True
    # A cross-process renderer can expose one UIA Pane hosted inside the
    # selected top-level HWND. A foreign UIA descendant alone proves nothing.
    hwnd = element.property(30020)
    return (type(pid) is int and pid > 0 and type(hwnd) is int
            and element.property(30003) in (50025, 50026, 50033)
            and element.property(30019) is False and native_child(binding, hwnd, pid)
            and bool(bridge.user32.IsWindowVisible(hwnd))
            and not any(element.property(k) is True for k in (30040, 30043, 30149)))


def _hit_belongs(binding, hwnd):
    return (bool(hwnd) and bridge.root_window(hwnd) == binding.hwnd and
            (bridge.hwnd_pid(hwnd) == binding.pid or native_child(binding, hwnd)))


def _enabled_canvas(binding, uia, painted, hit):
    if painted.property(30010) is True:
        return True
    # WindowFromPoint skips disabled HWNDs. A disabled paint-only child
    # from another process can sit over its enabled native input receiver.
    # Require agreement between the Win32 and UIA parent relationships.
    pane = painted.property(30020)
    if (painted.property(30010) is not False or uia.pid(painted) == binding.pid
            or not safe_provider(binding, uia, painted)
            or not native_child(binding, hit, binding.pid) or not bridge.user32.IsChild(hit,pane)
            or bridge.user32.IsWindowEnabled(pane) or not bridge.user32.IsWindowEnabled(hit)
            or not bridge.user32.IsWindowEnabled(binding.hwnd)):
        return False
    receiver = uia.from_handle(hit)
    return (receiver is not None and receiver.property(30020) == hit and uia.pid(receiver) == binding.pid
            and receiver.property(30003) in (50025,50026,50033) and receiver.property(30010) is True
            and safe_provider(binding,uia,receiver) and uia.within(painted,receiver)
            and not any(receiver.property(k) is True for k in (30040,30043,30149)))


def _safe_patch(binding, uia, box, point, *, exposed):
    """Check control metadata only on branches intersecting the intended image."""
    binding.check()
    client = bridge.RECT()
    origin = W.POINT()
    user = bridge.user32
    get_client = bridge._sig(user, 'GetClientRect', W.BOOL, W.HWND, C.POINTER(bridge.RECT))
    to_screen = bridge._sig(user, 'ClientToScreen', W.BOOL, W.HWND, C.POINTER(W.POINT))
    _require(get_client(binding.hwnd, C.byref(client)) and to_screen(binding.hwnd, C.byref(origin))
             and vision._inside(box, [origin.x, origin.y, client.right, client.bottom]), 'visual_target_outside_client')
    root = uia.from_handle(binding.hwnd)
    _require(root is not None, 'visual_window_unavailable')
    affinity = W.DWORD()
    if bridge._sig(user,'GetWindowDisplayAffinity',W.BOOL,W.HWND,C.POINTER(W.DWORD))(binding.hwnd,C.byref(affinity)):
        _require(affinity.value == 0, 'visual_window_capture_protected')
    hit = None
    if exposed:
        hit = bridge.window_from_point(*point)
        _require(bridge.root_window(bridge.foreground()) == binding.hwnd and _hit_belongs(binding, hit),
                 'visual_point_not_exposed')
        element = uia.from_point(*point)
        _require(element is not None and safe_provider(binding, uia, element)
                 and element.property(30003) in _CANVAS and uia.within(element, root)
                 and _enabled_canvas(binding,uia,element,hit), 'visual_hit_not_actionable')
        if bridge.hwnd_pid(hit) != binding.pid:
            embedded = uia.from_handle(hit)
            _require(embedded is not None and embedded.property(30020) == hit
                     and uia.pid(embedded) == bridge.hwnd_pid(hit) and safe_provider(binding,uia,embedded),
                     'visual_hit_not_actionable')
    pending = deque([root])
    count = 0
    while pending:
        element = pending.popleft()
        count += 1
        _require(count <= 1600, 'visual_scope_incomplete')
        rect = vision._bounds(uia, element)
        if rect[2] > 0 and rect[3] > 0:
            if not vision._overlaps(rect, box):
                continue
        elif element.property(30022) is True:
            continue
        role = element.property(30003)
        _require(safe_provider(binding,uia,element) and role not in _INPUT
                 and element.property(30040) is not True and element.property(30149) is not True
                 and (element.property(30043) is not True or role in (50026,50032,50033)),
                 'visual_target_protected_or_editable')
        pending.extend(uia.children(element))
    return hit


def observe(binding, spec):
    """Resolve from pixels belonging to this HWND without activating it."""
    window = vision._window_rect(binding)
    validate_spec(spec)
    pixels = vision._frame(binding, window)
    box, point = _locate(window, pixels, spec)
    with bridge.UIA() as uia:
        _safe_patch(binding, uia, box, point, exposed=False)
    _require(vision._window_rect(binding) == window, 'visual_window_moved')
    return dict(point=point, box=box, window=window)


def teach(binding, screen_point):
    window = vision._window_rect(binding)
    box = _box(window, screen_point)
    with bridge.UIA() as uia:
        _safe_patch(binding, uia, box, screen_point, exposed=False)
    spec = from_frame(window, vision._frame(binding, window), screen_point)
    _require(observe(binding, spec)['point'] == list(screen_point), 'visual_target_changed')
    return spec


def execute(binding, spec, guard, *, read_only=False, background_only=False, first_match_timeout=0, before_input=None):
    """Deliver at most one click after independently matching the current window."""
    def blocked(reason):
        return dict(status='blocked', input_dispatched=False, diagnostic=reason)
    if background_only and not read_only:
        return blocked('physical_input_requires_foreground')
    try:
        deadline = time.monotonic() + min(max(first_match_timeout, 0), 30)
        while True:
            guard()
            try:
                found = observe(binding, spec)
                break
            except bridge.BridgeError as error:
                remaining = deadline - time.monotonic()
                if error.code != 'visual_target_not_found' or remaining <= 0:
                    raise
                time.sleep(min(.4, remaining))
        if read_only:
            return dict(status='observed', input_dispatched=False, target_resolution='unique_visual_match')
        import row_action
        with coordination.desktop_input():
            guard()
            row_action.foreground(binding)
            guard()
            bridge._combo_surface(binding)
            tick = row_action._input_tick()
            current = observe(binding, spec)
            point, box = current['point'], current['box']
            _require(found['window'] == current['window'] and found['point'] == point,
                     'visual_target_changed_before_input')
            with bridge.UIA() as uia:
                shown = _safe_patch(binding, uia, box, point, exposed=True)
            if before_input is not None:
                before_input(point)
            guard()
            bridge._combo_surface(binding)
            raw = vision._frame(binding, current['window'], crop=box)
            candidate = vision._rgb((box, (), raw), box)
            _require(_distance(_decoded(spec), candidate) is not None, 'visual_target_changed_before_input')
            hit = bridge.window_from_point(*point)
            _require(hit == shown and _hit_belongs(binding, hit)
                     and bridge.root_window(bridge.foreground()) == binding.hwnd
                     and vision._window_rect(binding) == current['window'], 'visual_point_not_exposed')
            _require(row_action._input_tick() == tick, 'foreground_or_input_changed')
            try:
                result = bridge._send_point(*point, 'click')
            except Exception as error:
                result = dict(ok=False, input_dispatched=getattr(error,'input_dispatched', None),
                              diagnostic=getattr(error,'code','visual_click_delivery_uncertain'))
    except (bridge.BridgeError, OSError, ValueError) as error:
        return blocked(getattr(error,'code','visual_observation_failed'))
    delivered = result.get('ok') and result.get('input_dispatched') is True
    return dict(status='delivered_unverified' if delivered else 'uncertain',
                diagnostic='visual_click_dispatched' if delivered else result.get('diagnostic','visual_click_delivery_uncertain'),
                input_dispatched=result.get('input_dispatched'), input_mode='physical_pointer', physical_input=True,
                effect_verification='unverified', verification_scope='click_delivery', business_commit='not_verified')


class HoverTracker(threading.Thread):
    """Sample just the pointed target before click. Hook callbacks only copy references."""
    def __init__(self, binding):
        super().__init__(name='DesktopSelectVisualSampler', daemon=True)
        self.binding = binding
        self.stopping = threading.Event()
        self.latest = None
        self.cutoff = 0.
        self.pinned = deque(maxlen=4)

    def _sample(self, uia):
        binding = self.binding
        if bridge.root_window(bridge.foreground()) != binding.hwnd:
            return None
        point = tuple(bridge.cursor())
        window = vision._window_rect(binding)
        box = _box(window, point)
        hit = _safe_patch(binding, uia, box, point, exposed=True)
        start = time.monotonic()
        pixels = vision._frame(binding, window, crop=box)
        rgb = vision._rgb((box, (), pixels), box)
        _require(_texture(rgb), 'visual_target_lacks_detail')
        end = time.monotonic()
        if point != tuple(bridge.cursor()) or hit != bridge.window_from_point(*point):
            return None
        # Recheck input protection on the same UIA region before keeping pixels.
        _safe_patch(binding, uia, box, point, exposed=True)
        spec = dict(size=[SIDE, SIDE], offset=[point[i]-box[i] for i in range(2)],
                    rgb=base64.b64encode(rgb).decode('ascii'))
        return start, end, hit, box, point, spec

    def run(self):
        try:
            with bridge.UIA() as uia:
                while not self.stopping.is_set():
                    try:
                        sample = self._sample(uia)
                    except (bridge.BridgeError, OSError, ValueError):
                        sample = None
                    self.latest = sample if sample and sample[0] > self.cutoff else None
                    self.stopping.wait(.35 if sample else .18)
        except (bridge.BridgeError, OSError):
            pass  # Semantic recording can still run on this window.
        finally:
            self.latest = None

    def pin_down(self, event):
        sample = self.latest
        if (sample is None or not (event.get('scope_proven') or event.get('visual_embedded') is True)
                or event.get('action') != 'click'
                or event.get('window') != self.binding.hwnd or sample[0] <= self.cutoff
                or not 0 <= event['at']-sample[1] <= .65 or event.get('hit_window') != sample[2]):
            return
        x, y = event['x'], event['y']
        box, point = sample[3:5]
        if (max(abs(x-point[0]), abs(y-point[1])) <= 8 and
                box[0] <= x < box[0]+SIDE and box[1] <= y < box[1]+SIDE):
            self.pinned.append((event['token'], event['at'], sample[5], [x-box[0], y-box[1]]))
            return True
        return False

    def before(self, event):
        for token, when, spec, offset in reversed(tuple(self.pinned)):
            if token == event.get('token') and when == event.get('at'):
                return dict(spec, offset=offset)
        return None

    def invalidate(self, at=None):
        self.cutoff = time.monotonic() if at is None else at
        self.latest = None

    def stop(self):
        self.stopping.set()
        self.latest = None
        self.pinned.clear()
