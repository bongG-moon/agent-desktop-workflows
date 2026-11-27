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
NOTEPAD_FIND_NEXT = {'surface': 'notepad-find-next'}
_CANVAS = {50000, 50006, 50020, 50021, 50025, 50026, 50032, 50033}
_INPUT = {50003, 50004, 50008, 50009, 50010, 50011, 50028, 50029, 50036}
_DATA = {50007, 50008, 50028, 50029, 50036}  # ListItem, List, DataGrid, DataItem, Table


def _require(condition, diagnostic):
    if not condition:
        raise bridge.BridgeError(diagnostic)


def _decoded(spec):
    _require(isinstance(spec, dict) and set(spec) - {'gesture'} == {'size', 'offset', 'rgb'}
             and ('gesture' not in spec or spec['gesture'] == 'double-click'), 'invalid_visual_target')
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
    if spec == NOTEPAD_FIND_NEXT:
        return spec
    if isinstance(spec,dict) and spec.get('surface')=='win32-toolbar-glyph':
        from toolbar_button import decoded
        decoded(spec)
        return spec
    _require(_texture(_decoded(spec)), 'visual_target_lacks_detail')
    return spec


def _box(window, point):
    _require(len(point) == 2 and all(type(n) is int for n in point), 'invalid_visual_point')
    _require(vision._inside([*point, 1, 1], window) and min(window[2:]) >= SIDE,
             'visual_point_outside_window')
    return [max(window[i], min(point[i] - SIDE//2, window[i] + window[i+2] - SIDE))
            for i in range(2)] + [SIDE, SIDE]


def _icon_model(rgb):
    """Identify a solid glyph on a uniform background. Photos, borders and text blocks keep strict RGB matching."""
    bg = tuple(rgb[:3])
    # Require an empty rim so a crop touching an adjacent widget cannot claim a background change.
    rim = (*range(SIDE), *range(SIDE*(SIDE-1), SIDE*SIDE), *range(0, SIDE*SIDE, SIDE),
           *range(SIDE-1, SIDE*SIDE, SIDE))
    if any(rgb[i*3:i*3+3] != rgb[:3] for i in rim):
        return None
    colors = list(zip(rgb[::3], rgb[1::3], rgb[2::3]))
    if colors.count(bg) < len(colors)*.55:
        return None
    fg = max(colors, key=lambda p: max(abs(a-b) for a, b in zip(bg, p)))
    ch = max(range(3), key=lambda i: abs(fg[i]-bg[i]))
    span = fg[ch]-bg[ch]
    if abs(span) < 72:
        return None
    # Alpha is the ink coverage of each antialiased pixel, independent of a hover fill.
    alpha = [round(255*(p[ch]-bg[ch])/span) for p in colors]
    if sum(a >= 192 for a in alpha) < 16:
        return None
    for p, a in zip(colors, alpha):
        if not 0 <= a <= 255 or any(abs(p[i]-bg[i]-(fg[i]-bg[i])*a/255) > 6 for i in range(3)):
            return None
    return bg, bytes(alpha)


def _anchors(rgb, icon=None):
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
        # Hover may tint the fill. Fully opaque ink remains a narrow search anchor.
        margin = 12 if icon is None else 12 + 40*(255-icon[1][y*SIDE+x])//255
        pieces.append(b'[\\x%02x-\\x%02x]' % (max(0, value-margin), min(255, value+margin)))
        last = x
    return channel, y, re.compile(b'(?=' + b''.join(pieces) + b')', re.DOTALL)


def _distance(reference, actual, icon=None):
    # Limit both overall drift and large pixel differences. A changed glyph
    # cannot win solely because its surrounding margins still match.
    if reference == actual:
        return 0
    if icon is not None:
        bg, alpha = icon
        shift = [actual[i]-bg[i] for i in range(3)]
        if max(map(abs, shift)) > 40:
            return None
        total = 0
        for i, ink in enumerate(alpha):
            # Only a uniform background tint is explained; a moved stroke or a different glyph is not.
            for c in range(3):
                diff = abs(actual[i*3+c] - reference[i*3+c] - round(shift[c]*(255-ink)/255))
                if diff > 12:
                    return None
                total += diff
        return total
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
    icon = _icon_model(rgb)
    channel, row, pattern = _anchors(rgb, icon)
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
        distance = _distance(rgb, candidate, icon)
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


def _frame_patch(window, pixels, screen_point, *, client=None):
    """Encode the viewed patch; callers must independently prove its identity."""
    box = _box(window if client is None else client, screen_point)
    rgb = vision._rgb((window, (), pixels), box)
    spec = dict(size=[SIDE,SIDE], offset=[screen_point[i]-box[i] for i in range(2)],
                rgb=base64.b64encode(rgb).decode('ascii'))
    return validate_spec(spec)


def from_frame(window, pixels, screen_point, *, client=None):
    """Build a target from the exact full-resolution BGRA image viewed by the host."""
    spec = _frame_patch(window, pixels, screen_point, client=client)
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
    hwnd = bridge.normalize_hwnd(element.property(30020))
    return (type(pid) is int and pid > 0 and hwnd != 0
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
    pane = bridge.normalize_hwnd(painted.property(30020))
    if (painted.property(30010) is not False or uia.pid(painted) == binding.pid
            or not safe_provider(binding, uia, painted)
            or not native_child(binding, hit, binding.pid) or not bridge.user32.IsChild(hit,pane)
            or bridge.user32.IsWindowEnabled(pane) or not bridge.user32.IsWindowEnabled(hit)
            or not bridge.user32.IsWindowEnabled(binding.hwnd)):
        return False
    receiver = uia.from_handle(hit)
    return (receiver is not None and bridge.normalize_hwnd(receiver.property(30020)) == hit and uia.pid(receiver) == binding.pid
            and receiver.property(30003) in (50025,50026,50033) and receiver.property(30010) is True
            and safe_provider(binding,uia,receiver) and uia.within(painted,receiver)
            and not any(receiver.property(k) is True for k in (30040,30043,30149)))


def _client_rect(binding):
    client = bridge.RECT()
    origin = W.POINT()
    user = bridge.user32
    get_client = bridge._sig(user, 'GetClientRect', W.BOOL, W.HWND, C.POINTER(bridge.RECT))
    to_screen = bridge._sig(user, 'ClientToScreen', W.BOOL, W.HWND, C.POINTER(W.POINT))
    _require(get_client(binding.hwnd, C.byref(client)) and to_screen(binding.hwnd, C.byref(origin)),
             'visual_target_outside_client')
    return [origin.x, origin.y, client.right, client.bottom]


def _safe_patch(binding, uia, box, point, *, exposed, gesture=None, data_surface=None, client=None):
    """Check control metadata only on branches intersecting the intended image."""
    binding.check()
    _require(vision._inside(box, _client_rect(binding) if client is None else client), 'visual_target_outside_client')
    user = bridge.user32
    root = uia.from_handle(binding.hwnd)
    _require(root is not None, 'visual_window_unavailable')
    affinity = W.DWORD()
    if bridge._sig(user,'GetWindowDisplayAffinity',W.BOOL,W.HWND,C.POINTER(W.DWORD))(binding.hwnd,C.byref(affinity)):
        _require(affinity.value == 0, 'visual_window_capture_protected')
    hit = None
    allow_data = gesture == 'double-click'
    if exposed:
        hit = bridge.window_from_point(*point)
        _require(bridge.root_window(bridge.foreground()) == binding.hwnd and _hit_belongs(binding, hit),
                 'visual_point_not_exposed')
        element = uia.from_point(*point)
        _require(element is not None and safe_provider(binding, uia, element)
                 and (element.property(30003) in _CANVAS or _read_only_document(element)
                      or allow_data and element.property(30003) in _DATA)
                 and uia.within(element, root)
                 and _enabled_canvas(binding,uia,element,hit), 'visual_hit_not_actionable')
        if bridge.hwnd_pid(hit) != binding.pid:
            embedded = uia.from_handle(hit)
            _require(embedded is not None and bridge.normalize_hwnd(embedded.property(30020)) == hit
                     and uia.pid(embedded) == bridge.hwnd_pid(hit) and safe_provider(binding,uia,embedded),
                     'visual_hit_not_actionable')
    pending = deque([(root, False)])
    count = 0
    while pending:
        element, in_data = pending.popleft()
        count += 1
        _require(count <= 1600, 'visual_scope_incomplete')
        rect = vision._bounds(uia, element)
        if rect[2] > 0 and rect[3] > 0:
            if not vision._overlaps(rect, box):
                continue
        elif element.property(30022) is True:
            continue
        role = element.property(30003)
        doc = _read_only_document(element)
        data = allow_data and (role in _DATA or in_data and role == 50020)
        _require(safe_provider(binding,uia,element) and (role not in _INPUT or data)
                 and (element.property(30040) is not True or data or doc) and element.property(30149) is not True
                 and (element.property(30043) is not True or role in (50026,50032,50033)
                      or doc or data and element.property(30046) is True),
                 'visual_target_protected_or_editable')
        if data and data_surface is not None:
            data_surface.add(role)
        pending.extend((child, in_data or data) for child in uia.children(element))
    return hit


def _read_only_document(element):
    # UIA's ValueIsReadOnly is meaningful only when the Value pattern is available.
    return (element.property(30003) == 50030 and element.property(30043) is True
            and element.property(30046) is True and element.property(30149) is not True)


def observe(binding, spec):
    """Resolve from pixels belonging to this HWND without activating it."""
    if spec == NOTEPAD_FIND_NEXT:
        from notepad_find import locate
        return locate(binding)
    if isinstance(spec,dict) and spec.get('surface')=='win32-toolbar-glyph':
        from toolbar_button import observe as observe_button
        return observe_button(binding,spec)
    window = vision._window_rect(binding)
    validate_spec(spec)
    pixels = vision._frame(binding, window)
    box, point = _locate(window, pixels, spec)
    with bridge.UIA() as uia:
        _safe_patch(binding, uia, box, point, exposed=False, gesture=spec.get('gesture'))
    _require(vision._window_rect(binding) == window, 'visual_window_moved')
    return dict(point=point, box=box, window=window)


def teach(binding, screen_point):
    window = vision._window_rect(binding)
    client = _client_rect(binding)
    box = _box(client, screen_point)
    with bridge.UIA() as uia:
        _safe_patch(binding, uia, box, screen_point, exposed=False, client=client)
    spec = from_frame(window, vision._frame(binding, window), screen_point, client=client)
    _require(observe(binding, spec)['point'] == list(screen_point), 'visual_target_changed')
    return spec


def execute(binding, spec, guard, *, read_only=False, background_only=False, first_match_timeout=0,
            before_input=None, reviewed_point=None):
    """Deliver one gesture in a single SendInput batch after matching the current window."""
    def blocked(reason):
        return dict(status='blocked', input_dispatched=False, diagnostic=reason)
    if background_only and not read_only:
        return blocked('physical_input_requires_foreground')
    try:
        if reviewed_point is not None:
            _require(not read_only and callable(before_input) and isinstance(spec, dict)
                     and spec.get('gesture') == 'double-click'
                     and isinstance(reviewed_point, (list, tuple)) and len(reviewed_point) == 2
                     and all(type(value) is int for value in reviewed_point), 'invalid_reviewed_result_point')
            reviewed_point = tuple(reviewed_point)

        def current_target():
            if reviewed_point is None:
                return observe(binding, spec)
            # This is only an ephemeral current-frame result confirmation.
            # Global image uniqueness cannot disambiguate equal text in another
            # column. The mandatory callback authenticates the full table,
            # original HWND/geometry and selected point before any input.
            window = vision._window_rect(binding)
            validate_spec(spec)
            box = [reviewed_point[i] - spec['offset'][i] for i in range(2)] + list(spec['size'])
            _require(vision._inside(box, window), 'visual_point_outside_window')
            with bridge.UIA() as uia:
                _safe_patch(binding, uia, box, reviewed_point, exposed=False, gesture='double-click')
            return dict(point=list(reviewed_point), box=box, window=window)

        deadline = time.monotonic() + min(max(first_match_timeout, 0), 30)
        while True:
            guard()
            try:
                found = current_target()
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
            current = current_target()
            point, box = current['point'], current['box']
            _require(found['window'] == current['window'] and found['point'] == point,
                     'visual_target_changed_before_input')
            if spec != NOTEPAD_FIND_NEXT:
                with bridge.UIA() as uia:
                    shown = _safe_patch(binding, uia, box, point, exposed=True, gesture=spec.get('gesture'))
                    if 'toolbar' in current:
                        from toolbar_button import exposed
                        _require(exposed(binding,uia,current),'visual_hit_not_actionable')
            if before_input is not None:
                before_input(point)
            guard()
            bridge._combo_surface(binding)
            if spec == NOTEPAD_FIND_NEXT:
                from notepad_find import locate
                exposed = locate(binding, exposed=True)
                _require(all(exposed[key] == current[key] for key in ('point', 'window', 'overlay')),
                         'visual_target_changed_before_input')
            else:
                raw = vision._frame(binding, current['window'], crop=box)
                candidate = vision._rgb((box, (), raw), box)
                if 'toolbar' in current:
                    from toolbar_button import decoded, matches, exposed
                    with bridge.UIA() as uia:
                        _require(exposed(binding,uia,current) and matches(decoded(spec),candidate),
                                 'visual_target_changed_before_input')
                else:
                    reference = _decoded(spec)
                    _require((reference == candidate) if reviewed_point is not None else
                             _distance(reference, candidate, _icon_model(reference)) is not None,
                             'visual_target_changed_before_input')
                hit = bridge.window_from_point(*point)
                _require(hit == shown and _hit_belongs(binding, hit)
                         and bridge.root_window(bridge.foreground()) == binding.hwnd
                         and vision._window_rect(binding) == current['window'], 'visual_point_not_exposed')
            _require(row_action._input_tick() == tick, 'foreground_or_input_changed')
            try:
                gesture = spec.get('gesture', 'click')
                result = bridge._send_point(*point, gesture)
            except Exception as error:
                result = dict(ok=False, input_dispatched=getattr(error,'input_dispatched', None),
                              diagnostic=getattr(error,'code','visual_click_delivery_uncertain'))
    except (bridge.BridgeError, OSError, ValueError) as error:
        return blocked(getattr(error,'code','visual_observation_failed'))
    delivered = result.get('ok') and result.get('input_dispatched') is True
    return dict(status='delivered_unverified' if delivered else 'uncertain',
                diagnostic=('visual_double_click_dispatched' if gesture == 'double-click' else 'visual_click_dispatched')
                if delivered else result.get('diagnostic','visual_click_delivery_uncertain'),
                input_dispatched=result.get('input_dispatched'), input_mode='physical_pointer', physical_input=True,
                **{'screen_point' if delivered else 'attempted_screen_point': list(point)},
                effect_verification='unverified', verification_scope='click_delivery', business_commit='not_verified',
                **({'gesture': gesture} if gesture == 'double-click' else {}))


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
        hit = bridge.window_from_point(*point)
        if hit and bridge.window_class(hit).casefold()=='toolbarwindow32':
            from toolbar_button import native_toolbar, sample
            if native_toolbar(binding,hit):
                button = sample(binding,uia,window,point,hit)
                if button is not None:
                    return button
        client = _client_rect(binding)
        box = _box(client, point)
        data_surface = set()
        hit = _safe_patch(binding, uia, box, point, exposed=True, gesture='double-click', data_surface=data_surface,
                          client=client)
        start = time.monotonic()
        pixels = vision._frame(binding, window, crop=box)
        rgb = vision._rgb((box, (), pixels), box)
        _require(_texture(rgb), 'visual_target_lacks_detail')
        end = time.monotonic()
        if point != tuple(bridge.cursor()) or hit != bridge.window_from_point(*point):
            return None
        # Recheck input protection on the same UIA region before keeping pixels.
        _safe_patch(binding, uia, box, point, exposed=True, gesture='double-click', data_surface=data_surface)
        spec = dict(size=[SIDE, SIDE], offset=[point[i]-box[i] for i in range(2)],
                    rgb=base64.b64encode(rgb).decode('ascii'))
        if data_surface:
            spec['gesture'] = 'double-click'  # Only a measured double-click may adopt a row crop.
        return start, end, hit, box, point, spec

    def run(self):
        try:
            with bridge.UIA() as uia:
                while not self.stopping.is_set():
                    previous = None
                    try:
                        active = bridge.root_window(bridge.foreground()) == self.binding.hwnd
                        if not active:
                            self.latest = None
                            self.stopping.wait(.18)
                            continue
                        previous = tuple(bridge.cursor())
                        sample = self._sample(uia)
                    except (bridge.BridgeError, OSError, ValueError):
                        sample = None
                    self.latest = sample if sample and sample[0] > self.cutoff else None
                    self._wait_for_hover(previous, .35 if sample else .18)
        except (bridge.BridgeError, OSError):
            pass  # Semantic recording can still run on this window.
        finally:
            self.latest = None

    def _wait_for_hover(self, previous, interval):
        # A settled pointer at a new position should not wait for the old icon's
        # refresh timer. Poll only the cursor here; do not paint or traverse UIA while moving.
        if previous is None:
            self.stopping.wait(interval)
            return
        deadline = time.monotonic() + interval
        last = previous
        while not self.stopping.is_set():
            left = deadline - time.monotonic()
            if left <= 0 or self.stopping.wait(min(.03, left)):
                return
            try:
                point = tuple(bridge.cursor())
            except (bridge.BridgeError, OSError):
                return
            if point != previous and point == last:
                return
            last = point

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
                box[0] <= x < box[0]+box[2] and box[1] <= y < box[1]+box[3]):
            self.pinned.append((event['token'], event['at'], sample[5], [x-box[0], y-box[1]]))
            return True
        return False

    def before(self, event):
        for token, when, spec, offset in reversed(tuple(self.pinned)):
            if token == event.get('token') and when == event.get('at'):
                if spec.get('surface')=='win32-toolbar-glyph':
                    return dict(spec)
                return dict(spec, offset=offset)
        return None

    def invalidate(self, at=None):
        self.cutoff = time.monotonic() if at is None else at
        self.latest = None

    def stop(self):
        self.stopping.set()
        self.latest = None
        self.pinned.clear()
