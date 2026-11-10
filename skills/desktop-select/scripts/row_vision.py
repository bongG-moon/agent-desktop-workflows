"""Recognize a demonstrated row, its button and its result on the local desktop."""
from __future__ import annotations

import base64
from collections import deque
import ctypes as C
from ctypes import wintypes as W
import struct

import win_bridge as bridge


def _reject(code):
    raise bridge.BridgeError(code)


def _inside(rect, parent):
    x, y, w, h = rect
    px, py, pw, ph = parent
    return w > 0 and h > 0 and px <= x and py <= y and x + w <= px + pw and y + h <= py + ph


def _overlaps(a, b):
    return (max(a[0], b[0]) < min(a[0] + a[2], b[0] + b[2]) and
            max(a[1], b[1]) < min(a[1] + a[3], b[1] + b[3]))


def _bounds(uia, element):
    r = uia.bounds(element)
    return [r[k] for k in ('x', 'y', 'width', 'height')]


def _window_rect(binding):
    binding.check()
    if bridge.user32.IsIconic(binding.hwnd) or not bridge.user32.IsWindowVisible(binding.hwnd):
        _reject('row_window_not_visible')
    r = bridge.RECT()
    fn = bridge._sig(bridge.user32, 'GetWindowRect', W.BOOL, W.HWND, C.POINTER(bridge.RECT))
    if not fn(binding.hwnd, C.byref(r)):
        _reject('row_window_unavailable')
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def _row_layout(binding):
    """Enumerate full visible row peers; skip clipped and protected content."""
    window = _window_rect(binding)
    rows, protected = [], []
    with bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        if root is None:
            _reject('row_window_unavailable')
        queue = deque([(root, [], None, 0)])
        count = 0
        while queue:
            e, scope, viewport, depth = queue.popleft()
            count += 1
            if count > 2500 or depth > 26:
                _reject('row_tree_incomplete')
            if uia.pid(e) != binding.pid or uia.secure(e):
                rect = _bounds(uia, e)
                if rect[2] and rect[3]:
                    protected.append(rect)
                elif e.property(30022) is not True:
                    _reject('row_protected_bounds_unknown')
                continue
            control = e.property(30003)
            aid = e.property(30011)
            stable_id = isinstance(aid, str) and bool(aid) and not aid.isdecimal() and len(aid) <= 160
            parent_scope = scope
            if stable_id:
                scope = [*scope[-3:], aid]
            if control in (50008, 50028, 50036):  # List, DataGrid, Table
                viewport = _bounds(uia, e)
            if control in (50007, 50029) and viewport and e.property(30022) is False and e.property(30010) is True:
                rect = _bounds(uia, e)
                if (_inside(rect, viewport) and _inside(rect, window) and
                        rect[2] <= 5000 and rect[3] <= 512):
                    identity = {'role': bridge.ROLES[control], 'scope': parent_scope}
                    if stable_id:
                        identity['row_id'] = aid
                    rows.append((rect, identity))
            for child in uia.children(e):
                queue.append((child, scope, viewport, depth + 1))
    if _window_rect(binding) != window:
        _reject('row_window_moved')
    return window, [(rect, meta) for rect, meta in rows if not any(_overlaps(rect, p) for p in protected)]


def visible_rows(binding):
    return sorted((r for r, _ in _row_layout(binding)[1]), key=lambda r: (r[1], r[0]))


def _frame(binding, window):
    """Print the target HWND into a private DIB. Occluding apps never enter this image."""
    w, h = window[2:]
    if not (0 < w <= 12000 and 0 < h <= 12000 and w*h <= 16_777_216):
        _reject('row_window_size_unsupported')
    user = bridge.user32
    gdi = C.WinDLL('gdi32', use_last_error=True)
    sig, handle = bridge._sig, W.HANDLE
    create_dc = sig(gdi, 'CreateCompatibleDC', handle, handle)
    create_bitmap = sig(gdi, 'CreateDIBSection', handle, handle, C.c_void_p, W.UINT,
                        C.POINTER(C.c_void_p), handle, W.DWORD)
    select = sig(gdi, 'SelectObject', handle, handle, handle)
    delete = sig(gdi, 'DeleteObject', W.BOOL, handle)
    delete_dc = sig(gdi, 'DeleteDC', W.BOOL, handle)
    paint = sig(user, 'PrintWindow', W.BOOL, W.HWND, handle, W.UINT)
    flush = sig(gdi, 'GdiFlush', W.BOOL)
    bmi = C.create_string_buffer(struct.pack('<IiiHHIIiiII', 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0))
    bits = C.c_void_p()
    dc = create_dc(None)
    if not dc:
        _reject('row_pixels_unavailable')
    bitmap = old = None
    try:
        bitmap = create_bitmap(dc, bmi, 0, C.byref(bits), None, 0)
        if not bitmap or not bits.value:
            _reject('row_pixels_unavailable')
        old = select(dc, bitmap)
        if not old or old == C.c_void_p(-1).value:
            _reject('row_pixels_unavailable')
        if not paint(binding.hwnd, dc, 2) or not flush():
            _reject('row_pixels_unavailable')
        raw = C.string_at(bits.value, w*h*4)
    finally:
        if old and old != C.c_void_p(-1).value:
            select(dc, old)
        if bitmap:
            delete(bitmap)
        delete_dc(dc)
    if _window_rect(binding) != window:
        _reject('row_window_moved')
    return raw


def _snapshot(binding):
    window, rows = _row_layout(binding)
    frame = _frame(binding, window)
    # A sort or scroll while PrintWindow was running invalidates row geometry.
    if (window, rows) != _row_layout(binding):
        _reject('row_layout_changed')
    return window, rows, frame


def _rgb(snapshot, rect):
    window, _, frame = snapshot
    if not _inside(rect, window):
        _reject('row_patch_outside_window')
    x, y, w, h = rect
    start = ((y-window[1])*window[2] + x-window[0])*4
    stride = window[2]*4
    bgra = b''.join(frame[start+i*stride:start+i*stride+w*4] for i in range(h))
    rgb = bytearray(w*h*3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return bytes(rgb)


def _point_row(rows, point):
    if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(type(v) is int for v in point):
        _reject('invalid_row_point')
    candidates = [(r, meta) for r, meta in rows if _inside([*point,1,1], r)]
    if len(candidates) != 1:
        _reject('row_at_point_not_unique')
    return candidates[0]


def row_preview(binding, row_point):
    snapshot = _snapshot(binding)
    r, _ = _point_row(snapshot[1], row_point)
    return r, _ppm_header(r[2:]) + _rgb(snapshot, r)


def _ppm_header(size):
    return ('P6\n%d %d\n255\n' % tuple(size)).encode('ascii')


def _patch(rect, box):
    return [rect[0]+box[0], rect[1]+box[1], box[2], box[3]]


def _glyph(rgb):
    """Segment glyphs against their local background; ignore uniform hover brightness."""
    gray = bytes((r*77+g*150+b*29) >> 8 for r,g,b in zip(rgb[::3],rgb[1::3],rgb[2::3]))
    histogram = [0]*256
    for g in gray:
        histogram[g] += 1
    background = max(range(256), key=histogram.__getitem__)
    amplitude = max(background-min(gray), max(gray)-background)
    if amplitude < 14:
        return bytes(len(gray))
    # Edge antialiasing and different hover backgrounds alter contrast slightly.
    threshold = max(12, int(amplitude*.38))
    return bytes(abs(g-background) >= threshold for g in gray)


def _pattern(rgb):
    glyph = _glyph(rgb)
    if glyph.count(1) < 8 or glyph.count(0) < 8:
        _reject('row_patch_lacks_detail')
    packed = bytes(sum(bit << j for j,bit in enumerate(glyph[i:i+8])) for i in range(0,len(glyph),8))
    return base64.b64encode(packed).decode('ascii')


def _unpack(encoded, count):
    if not isinstance(encoded, str) or len(encoded) != 4*(((count+7)//8+2)//3):
        _reject('invalid_row_template')
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        _reject('invalid_row_template')
    if len(data) != (count+7)//8 or (count%8 and data[-1] >> (count%8)):
        _reject('invalid_row_template')
    bits = bytes((v >> j) & 1 for v in data for j in range(8))[:count]
    if bits.count(1) < 8 or bits.count(0) < 8:
        _reject('invalid_row_template')
    return bits


def _difference(a,b):
    # Fraction of foreground pixels that disagree; large empty margins confer no credit.
    intersection = sum(x & y for x,y in zip(a,b))
    union = a.count(1) + b.count(1) - intersection
    return 1-intersection/union if union else 1.


def _match_distance(reference, current, width):
    """A changed digit or suffix cannot hide in a long line of matching text."""
    distance = _difference(reference, current)
    if distance == 0 or distance > .20:
        return distance
    height = len(reference) // width
    for y in range(0, height, 8):
        for x in range(0, width, 8):
            changes = foreground = 0
            # Overlap tile boundaries so a small changed glyph is tested together.
            tile_width = min(16, width - x)
            for scan in range(y, min(y + 16, height)):
                start = scan * width + x
                old = int.from_bytes(reference[start:start + tile_width], 'little')
                new = int.from_bytes(current[start:start + tile_width], 'little')
                changes += (old ^ new).bit_count()
                foreground += (old | new).bit_count()
            if changes >= 3 and changes > .30 * foreground:
                return max(distance, .25)
    return distance


def _validate_layout(size, boxes):
    if (not isinstance(size,list) or len(size)!=2 or any(type(v) is not int for v in size)
            or not 1<=size[0]<=5000 or not 1<=size[1]<=512):
        _reject('invalid_row_size')
    if not isinstance(boxes,dict) or set(boxes) != {'key','button','state'}:
        _reject('invalid_row_boxes')
    seen = []
    for key in ('key','button','state'):
        box = boxes[key]
        if (not isinstance(box,list) or len(box)!=4 or any(type(v) is not int for v in box)
                or not _inside(box,[0,0,*size]) or not 3<=box[2]<=512 or not 3<=box[3]<=128
                or box[2]*box[3]>32768 or any(_overlaps(box,other) for other in seen)):
            _reject('invalid_row_boxes')
        seen.append(box)


def validate_spec(spec, *, draft=False):
    fields = {'version','size','role','scope','boxes','key','button','before'}
    if not draft:
        fields.add('after')
    if isinstance(spec, dict) and 'row_id' in spec:
        if not isinstance(spec['row_id'], str) or not 1 <= len(spec['row_id']) <= 160:
            _reject('invalid_row_template')
        fields.add('row_id')
    if (not isinstance(spec,dict) or set(spec)!=fields or type(spec['version']) is not int or spec['version']!=1
            or spec['role'] not in ('listitem','row') or not isinstance(spec['scope'],list)
            or len(spec['scope'])>4 or any(not isinstance(s,str) or not 1<=len(s)<=160 for s in spec['scope'])):
        _reject('invalid_row_template')
    _validate_layout(spec['size'],spec['boxes'])
    masks = {}
    for field in ('key','button','before') if draft else ('key','button','before','after'):
        box = spec['boxes']['state' if field in ('before','after') else field]
        masks[field] = _unpack(spec[field],box[2]*box[3])
    if not draft and _difference(masks['before'],masks['after']) < .30:
        _reject('row_state_change_unproven')


def template_preview(spec, field):
    """Render only the stored foreground mask for review in the recorder."""
    validate_spec(spec, draft='after' not in spec)
    if field not in ('key', 'button', 'before', 'after') or field not in spec:
        _reject('invalid_row_template')
    size = spec['boxes']['state' if field in ('before', 'after') else field][2:]
    mask = _unpack(spec[field], size[0] * size[1])
    pixels = bytearray(len(mask) * 3)
    for i, bit in enumerate(mask):
        if bit:
            pixels[3*i:3*i+3] = b'\xff\xff\xff'
    return _ppm_header(size) + pixels


def _match(snapshot, spec):
    key = spec['boxes']['key']
    reference = _unpack(spec['key'],key[2]*key[3])
    scores = []
    for rect,meta in snapshot[1]:
        if (rect[2:] == spec['size'] and all(meta[k]==spec[k] for k in ('role','scope'))
                and meta.get('row_id') == spec.get('row_id')):
            current = _glyph(_rgb(snapshot,_patch(rect,key)))
            scores.append((_match_distance(reference, current, key[2]), rect))
    scores.sort()
    if not scores or scores[0][0] > .14:
        return 'missing', None
    if len(scores)>1 and scores[1][0] <= max(.20,scores[0][0]+.10):
        return 'ambiguous', None
    return 'found', scores[0][1]


def _evidence(snapshot, rect, spec, field):
    box = spec['boxes']['state' if field in ('before','after') else field]
    reference = _unpack(spec[field], box[2] * box[3])
    current = _glyph(_rgb(snapshot, _patch(rect, box)))
    return _match_distance(reference, current, box[2])


def teach_before(binding,row_point,key_box,button_box,state_box,*,preview=None):
    snapshot = _snapshot(binding)
    rect, meta = _point_row(snapshot[1],row_point)
    boxes = {'key':list(key_box),'button':list(button_box),'state':list(state_box)}
    _validate_layout(rect[2:],boxes)
    spec = dict(version=1,size=rect[2:],boxes=boxes,**meta)
    previous = None
    if preview is not None:
        old_rect, ppm = preview
        header = _ppm_header(rect[2:])
        if list(old_rect)!=rect or not ppm.startswith(header) or len(ppm)!=len(header)+rect[2]*rect[3]*3:
            _reject('row_preview_changed')
        previous = ppm[len(header):]
    for area,box in boxes.items():
        current = _rgb(snapshot,_patch(rect,box))
        if previous is not None:
            x,y,w,h = box
            offset, stride = (rect[2]*y+x)*3, rect[2]*3
            shown = b''.join(previous[offset+i*stride:offset+i*stride+w*3] for i in range(h))
            if _match_distance(_glyph(shown), _glyph(current), w) > .14:
                _reject('row_preview_changed')
        spec['before' if area=='state' else area] = _pattern(current)
    kind, found = _match(snapshot,spec)
    if kind!='found' or found!=rect:
        _reject('row_key_not_unique')
    validate_spec(spec,draft=True)
    return spec


def teach_after(binding,draft):
    validate_spec(draft,draft=True)
    snapshot = _snapshot(binding)
    kind,rect = _match(snapshot,draft)
    if kind!='found':
        _reject('row_key_'+kind)
    spec = dict(draft,after=_pattern(_rgb(snapshot,_patch(rect,draft['boxes']['state']))))
    validate_spec(spec)
    return spec


def transfer_after(draft,example_spec):
    validate_spec(draft,draft=True)
    validate_spec(example_spec)
    if any(draft[k]!=example_spec[k] for k in ('size','role','scope','boxes')):
        _reject('row_example_incompatible')
    for field, key in (('before','state'),('button','button')):
        w,h = draft['boxes'][key][2:]
        if _match_distance(_unpack(draft[field], w*h), _unpack(example_spec[field], w*h), w) > .14:
            _reject('row_example_incompatible')
    spec = dict(draft,after=example_spec['after'])
    validate_spec(spec)
    return spec


def observe_row(binding,spec):
    validate_spec(spec)
    snapshot = _snapshot(binding)
    kind,rect = _match(snapshot,spec)
    point = None
    if kind=='found':
        if _evidence(snapshot,rect,spec,'after') <= .14:
            kind = 'after'
        elif _evidence(snapshot,rect,spec,'before') <= .14 and _evidence(snapshot,rect,spec,'button') <= .14:
            kind = 'before'
            x,y,w,h = _patch(rect,spec['boxes']['button'])
            point = [x+w//2,y+h//2]
        else:
            kind = 'unknown'
    return {'kind':kind,'point':point,'row_bounds':rect}


def _list_hit_row(uia, element, pid, point, rect):
    """Some providers hit-test the List itself. Follow its unique visible branch at the point."""
    if element.property(30003) not in (50008, 50028, 50036):
        return False
    for _ in range(12):
        hits = []
        for child in uia.children(element):
            if child.property(30022) is True:
                continue
            bounds = _bounds(uia, child)
            if _inside([*point, 1, 1], bounds):
                hits.append((child, bounds))
        if len(hits) != 1:
            return False
        element, bounds = hits[0]
        if uia.pid(element) != pid or uia.secure(element) or element.property(30010) is not True:
            return False
        role = element.property(30003)
        if role in (50007, 50029):
            return bounds == rect
        if role not in (50008, 50028, 50036, 50026, 50033):
            return False
    return False


def confirm_point(binding,obs):
    if obs.get('kind')!='before':
        _reject('row_not_ready')
    point = obs['point']
    rect,_ = _point_row(_row_layout(binding)[1],point)
    x,y = point
    if (rect!=obs['row_bounds'] or bridge.root_window(bridge.foreground())!=binding.hwnd
            or bridge.root_window(bridge.window_from_point(x,y))!=binding.hwnd
            or bridge.hwnd_pid(bridge.window_from_point(x,y))!=binding.pid):
        _reject('row_point_not_exposed')
    with bridge.UIA() as uia:
        root, element = uia.from_handle(binding.hwnd), uia.from_point(x,y)
        if root is None or element is None or not uia.within(element,root):
            _reject('row_hit_not_owned')
        if (uia.pid(element) == binding.pid and not uia.secure(element)
                and element.property(30010) is True and _list_hit_row(uia, element, binding.pid, point, rect)):
            binding.check()
            return
        for _ in range(28):
            if (element is None or uia.pid(element) != binding.pid or uia.secure(element)
                    or element.property(30010) is not True):
                break
            if element.property(30003) in (50007,50029) and _bounds(uia,element)==rect:
                binding.check()
                return
            element = uia.parent(element)
    _reject('row_hit_not_owned')


def confirm_pixels(binding, spec, obs):
    """Recheck the row glyphs at the intended physical point immediately before input."""
    validate_spec(spec)
    rect = obs.get('row_bounds')
    if obs.get('kind') != 'before' or not rect or rect[2:] != spec['size']:
        _reject('row_not_ready')
    window = _window_rect(binding)
    if not _inside(rect, window):
        _reject('row_window_moved')
    snapshot = window, (), _frame(binding, window)
    if any(_evidence(snapshot, rect, spec, field) > .14 for field in ('key', 'button', 'before')):
        _reject('row_pixels_changed')
    x, y, w, h = _patch(rect, spec['boxes']['button'])
    point = [x + w//2, y + h//2]
    hit = bridge.window_from_point(*point)
    if (obs.get('point') != point or bridge.root_window(bridge.foreground()) != binding.hwnd
            or bridge.root_window(hit) != binding.hwnd or bridge.hwnd_pid(hit) != binding.pid):
        _reject('row_point_not_exposed')
