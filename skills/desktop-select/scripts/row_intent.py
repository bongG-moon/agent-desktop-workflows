"""Resolve a spoken launch instruction against the visible list at execution time."""
from __future__ import annotations

import re
import time

import row_action
import row_capture
import row_vision as visual
import win_bridge as bridge


_ORDINALS = {'첫': 1, '한': 1, '두': 2, '세': 3, '네': 4, '다섯': 5,
             '여섯': 6, '일곱': 7, '여덟': 8, '아홉': 9, '열': 10}
_ENGLISH = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5}
_LAUNCH_NAMES = {'실행', '시작', '재생', '켜기', 'launch', 'start', 'play', 'run'}


def parse(text):
    if not isinstance(text, str) or len(text) > 1000:
        return None
    words = ' '.join(text.strip().lower().rstrip('.!').split())
    # A window name gives context and a numbered device may describe the
    # effect of the launch. Only the ordinal attached to the row selects it.
    korean = re.fullmatch(
        r'(?:(?:(?!\S*(?:하고|한뒤|한후)\s)[\w.-]{1,40}\s+)?'
        r'(?:관리\s*창|관리자\s*창|목록\s*창|목록\s*화면|창|화면)(?:에서|의)\s+)?'
        r'(?:(?:현재|지금|화면의|화면에 보이는|목록에서|목록의)\s*)*'
        r'(?:(\d{1,2})\s*(?:번째|번(?:째)?)?|'
        r'(첫|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:번째|째)?)\s*'
        r'(?:행|줄|항목|요소|기기|인스턴스|플레이어)(?:의|에서|에 있는|에|을|를)?\s*'
        r'(?:실행|시작|재생|켜기)(?:\s*버튼(?:을|를)?)?\s*'
        r'(?:(?:눌러(?:서)?|클릭해(?:서)?)\s+\d{1,3}\s*(?:번\s*)?'
        r'(?:기기|인스턴스|플레이어)(?:를|을)?\s*(?:열어|실행해|시작해|켜)\s*(?:줘|주세요)?'
        r'|(?:클릭|선택|눌러|누르기|누른다|누름|하기|시켜|해|하|켜)?\s*'
        r'(?:해\s*)?(?:줘|주세요|주기|기 바랍니다|세요)?요?)?', words)
    if korean:
        n = int(korean[1]) if korean[1] else _ORDINALS[korean[2]]
    else:
        english = re.fullmatch(r'(?:please )?(?:launch|start|run|play) (?:the )?'
                               r'(first|second|third|fourth|fifth|\d{1,2}(?:st|nd|rd|th)?) '
                               r'(?:row|item|instance|device|player)', words)
        if not english:
            return None
        term = english[1]
        n = _ENGLISH[term] if term in _ENGLISH else int(re.match(r'\d+', term)[0])
    return n if 1 <= n <= 64 else None


def conflicts_with_launch(text):
    """Keep explicit prohibitions and alternative actions out of a host-supplied launch."""
    if not isinstance(text, str):
        return True
    return bool(re.search(
        r"(?:하지|시키지|켜지|누르지|클릭하지|실행하지|시작하지|눌러서는)\s*(?:마|말|않|안)|"
        r"(?:실행|시작|재생)\s*(?:금지|취소)|(?:종료|삭제|정지|중지|일시정지|재시작|재부팅|끄기|꺼줘|끄세요)|"
        r"\b(?:do not|don't|never|stop|delete|pause|restart|reboot|shut down)\b",
        text, re.IGNORECASE))


def _components(bits, width, height):
    """Connect foreground runs on adjacent scan lines without per-pixel objects."""
    parent, boxes = [], []
    previous = []
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for y in range(height):
        current = []
        j = 0
        for run in re.finditer(b'\x01+', bits[y*width:(y+1)*width]):
            left, right = run.span()
            while j < len(previous) and previous[j][1] < left:
                j += 1
            k, i = j, len(parent)
            parent.append(i)
            boxes.append([left, y, right, y+1])
            while k < len(previous) and previous[k][0] <= right:
                p = root(previous[k][2])
                if p != i:
                    a = boxes[p]
                    b = boxes[i]
                    b[:] = min(b[0], a[0]), min(b[1], a[1]), max(b[2], a[2]), max(b[3], a[3])
                    parent[p] = i
                k += 1
            current.append((left, right, i))
        previous = current
    return [b for i,b in enumerate(boxes) if parent[i] == i]


def _triangles(bits, width, height):
    for x, y, right, bottom in _components(bits, width, height):
        w, h = right-x, bottom-y
        if not (7 <= w <= 80 and 8 <= h <= 96 and .55 <= w/h <= 1.4):
            continue
        template, intersection, actual = 0, 0, 0
        for dy in range(h):
            extent = max(1, round(w*(1-abs((2*dy+1)/h-1))))
            line = bits[(y+dy)*width+x:(y+dy)*width+right]
            template += extent
            intersection += line[:extent].count(1)
            actual += line.count(1)
        if intersection / (actual + template - intersection) >= .83:
            yield x, y, right, bottom


def _recorded_play(spec):
    w, h = spec['boxes']['button'][2:]
    bits = visual._unpack(spec['button'], w*h)
    triangles = list(_triangles(bits,w,h))
    if len(triangles) != 1:
        return False
    x,y,right,bottom = triangles[0]
    return x <= w//2 < right and y <= h//2 < bottom


def play_glyphs(pixels, width, height):
    """Accept an isolated right-facing filled triangle enclosed by a button face."""
    candidates = []
    for x, y, right, bottom in _triangles(visual._glyph(pixels), width, height):
        w, h = right-x, bottom-y
        cx, cy = x+w//2, y+h//2
        def color(px, py):
            offset = (py*width+px)*3
            return pixels[offset:offset+3]
        # The glyph must be set into a distinct button, surrounded on four sides.
        def has_rim(px, py, dx, dy):
            if not (0 <= px < width and 0 <= py < height):
                return False
            inside = color(px, py)
            for distance in range(1, 2*max(w,h)+4):
                xx, yy = px+dx*distance, py+dy*distance
                if not (0 <= xx < width and 0 <= yy < height):
                    break
                if max(abs(a-b) for a,b in zip(inside,color(xx, yy))) >= 16:
                    return True
            return False
        if all(has_rim(*ray) for ray in ((x-2,cy,-1,0),(right+1,cy,1,0),
                                        (cx,y-2,0,-1),(cx,bottom+1,0,1))):
            candidates.append((cx, cy))
    return candidates


def _ordinal_row(binding, ordinal):
    _, rows = visual._row_layout(binding, include_viewport=True)
    return _at_ordinal(rows, ordinal)


def _at_ordinal(rows, ordinal):
    if type(ordinal) is not int or not 1 <= ordinal <= 64:
        raise bridge.BridgeError('invalid_row_ordinal')
    if not rows or len({m['viewport'] for _,m in rows}) != 1:
        raise bridge.BridgeError('row_list_ambiguous' if rows else 'row_list_not_visible')
    ordered = sorted((r for r,_ in rows), key=lambda r:r[1])
    if any(b[1] < a[1]+a[3] for a,b in zip(ordered, ordered[1:])):
        raise bridge.BridgeError('row_list_ambiguous')
    if len(ordered) < ordinal:
        raise bridge.BridgeError('row_ordinal_not_visible')
    return ordered[ordinal-1]


def _named_button(binding, rect):
    """Inspect row descendants only, including providers hit-testing the list."""
    with bridge.UIA() as uia:
        root = uia.from_handle(binding.hwnd)
        element = uia.from_point(rect[0]+rect[2]//2, rect[1]+rect[3]//2)
        if not element or not uia.within(element, root):
            return None
        for _ in range(24):
            if not element or uia.pid(element) != binding.pid or uia.secure(element):
                return None
            role = element.property(30003)
            if role in (50007, 50029) and visual._bounds(uia,element) == rect:
                break
            if role in (50008, 50028, 50036):
                match = [c for c in uia.children(element) if c.property(30003) in (50007,50029)
                         and visual._bounds(uia,c) == rect]
                element = match[0] if len(match)==1 else None
            else:
                element = uia.parent(element)
        else:
            return None
        pending = list(uia.children(element))
        seen, hits = 0, []
        while pending and seen < 160:
            child = pending.pop()
            seen += 1
            if uia.pid(child) != binding.pid or uia.secure(child):
                continue
            if child.property(30003) == 50000:
                name = str(child.property(30005) or '').strip().casefold()
                if name in _LAUNCH_NAMES and child.property(30010) is True and child.property(30022) is not True:
                    box = visual._bounds(uia,child)
                    if visual._inside(box, rect):
                        hits.append((box[0]+box[2]//2, box[1]+box[3]//2))
            else:
                pending.extend(uia.children(child))
        if pending or len(hits)>1:
            raise bridge.BridgeError('row_launch_button_ambiguous')
        return hits[0] if hits else None


def locate(binding, ordinal):
    rect = _ordinal_row(binding, ordinal)
    frame = row_capture.sample(binding)
    if frame is None:
        raise bridge.BridgeError('row_pixels_unavailable')
    index = _row_index(frame, rect)
    target = _named_button(binding, rect)
    source = 'accessible_button'
    if target is None:
        icons = play_glyphs(frame.pixels[index], *rect[2:])
        if len(icons) != 1:
            raise bridge.BridgeError('row_launch_button_ambiguous' if icons else 'row_launch_button_not_found')
        target = (rect[0]+icons[0][0], rect[1]+icons[0][1])
        source = 'visible_play_button'
    return frame, rect, target, source


def _row_index(frame, rect):
    matches = [i for i,(r,_) in enumerate(frame.rows) if r == rect]
    if len(matches) != 1:
        raise bridge.BridgeError('row_layout_changed')
    return matches[0]


def _memory(binding, title, ordinal, directory):
    from hashlib import sha256
    import learned_tasks as tasks
    key = '\n'.join((binding.exe.casefold(), title, str(ordinal))).encode('utf-8')
    return tasks._directory(directory) / 'row-launches', sha256(key).hexdigest()


def _remember(store, plan):
    """Replace only the derived cache for this ordinal when its occupant changes."""
    import json
    import os
    from pathlib import Path
    import tempfile
    import learned_tasks as tasks
    tasks._validate(plan)
    store = tasks._directory(store)
    store.mkdir(parents=True, exist_ok=True)
    destination = store / (plan['name'] + '.json')
    tasks._deny_redirect(destination)
    with tempfile.NamedTemporaryFile(dir=store, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        path.write_bytes(json.dumps(plan,ensure_ascii=False).encode('utf-8'))
        os.replace(path, destination)
    finally:
        path.unlink(missing_ok=True)


def execute(binding, title, ordinal, guard, directory=None, *, preview=False, timeout=75, hint=None):
    import learned_tasks as tasks
    store, key = _memory(binding,title,ordinal,directory)
    guard()
    if not preview and bridge.user32.IsIconic(binding.hwnd):
        row_action.foreground(binding)
    examples = []
    if hint is not None:
        visual.validate_spec(hint)
        if _recorded_play(hint):
            examples.append((hint, 'recorded_row_hint'))
    try:
        saved = tasks.load_task(key,store)
    except tasks.TaskError as error:
        if error.code != 'task_not_found':
            raise
    else:
        tasks.check_binding(saved,binding.exe,title)
        prior = saved['steps'][0]
        if prior['action'] == 'row-action' and prior['row'] != hint:
            examples.append((prior['row'], 'previously_observed_row'))
    if examples:
        snapshot = visual._snapshot(binding, include_viewport=True)
        rect = _at_ordinal(snapshot[1],ordinal)
    for spec, evidence in examples:
        seen = visual.observe_row(binding,spec,snapshot=snapshot)
        if seen['row_bounds'] == rect and seen['kind'] in ('before','after'):
            if preview:
                return dict(status='resolved', ordinal=ordinal, evidence=evidence,
                            observed_state=seen['kind'], input_dispatched=False)
            if seen['kind'] == 'after':
                # Check identity, ordinal and effect together in both samples.
                # No further clicks or foreground changes are needed.
                time.sleep(.15)
                guard()
                fresh = visual._snapshot(binding,include_viewport=True)
                current = visual.observe_row(binding,spec,snapshot=fresh)
                if _at_ordinal(fresh[1],ordinal) == rect and current == seen:
                    return dict(row_action._result('verified',False,'after',before='after'),
                                ordinal=ordinal,evidence=evidence)
                raise bridge.BridgeError('row_changed_before_action')
            def same_position():
                guard()
                if _ordinal_row(binding,ordinal) != rect:
                    raise bridge.BridgeError('row_order_changed')
            return dict(row_action.execute(binding,spec,same_position,required_bounds=rect),
                        ordinal=ordinal,evidence=evidence)
    if not preview:
        row_action.foreground(binding)
    guard()
    frame, rect, point, source = locate(binding, ordinal)
    press = row_capture.prepare(frame, point, frame.end)
    if preview:
        return dict(status='resolved', ordinal=ordinal, evidence=source, input_dispatched=False)
    # Re-observe both order and intent at dispatch time; never use old coordinates.
    second, check_rect, check_point, check_source = locate(binding,ordinal)
    if (check_rect,check_point,check_source) != (rect,point,source):
        raise bridge.BridgeError('row_changed_before_action')
    index = _row_index(second,rect)
    box = row_capture._button(point,rect)
    if not box or row_capture._crop(frame.pixels[_row_index(frame,rect)],rect[2],box) != row_capture._crop(second.pixels[index],rect[2],box):
        raise bridge.BridgeError('row_button_changed')
    if press and not any(row_capture._crop(press.pixels,rect[2],key_box) ==
                         row_capture._crop(second.pixels[index],rect[2],key_box) for key_box in press.keys):
        raise bridge.BridgeError('row_identity_changed')
    obs = dict(kind='before', point=point, row_bounds=rect)
    visual.confirm_point(binding,obs)
    guard()
    bridge._combo_surface(binding)
    window = visual._window_rect(binding)
    if window != second.window:
        raise bridge.BridgeError('row_window_moved')
    latest = (window, (), visual._frame(binding, window))
    def matches(box):
        previous = visual._glyph(row_capture._crop(second.pixels[index],rect[2],box))
        current = visual._glyph(visual._rgb(latest, visual._patch(rect,box)))
        return visual._match_distance(previous,current,box[2]) <= .14
    if not matches(box) or (press and not any(matches(key_box) for key_box in press.keys)):
        raise bridge.BridgeError('row_changed_before_action')
    guard()
    bridge._combo_surface(binding)
    if bridge.root_window(bridge.window_from_point(*point)) != binding.hwnd:
        raise bridge.BridgeError('row_point_not_exposed')
    delivery = bridge._send_point(*point,'click')
    if delivery.get('input_dispatched') is not True or not delivery.get('ok'):
        return row_action._result('uncertain',delivery.get('input_dispatched'),'unknown',
                                  diagnostic=delivery.get('diagnostic','row_delivery_uncertain'))
    # Confirm a distinct same-row state marker as well as disappearance of the
    # original launch affordance. A changed hover effect alone supplies neither.
    deadline = time.monotonic()+timeout
    last, since = None, 0.
    while press and time.monotonic() < deadline:
        time.sleep(.35)
        try:
            guard()
            frame = row_capture.sample(binding)
            if frame is None:
                continue
            spec = row_capture.infer(press,frame)
            if spec:
                if source == 'accessible_button':
                    disappeared = _named_button(binding,rect) is None
                else:
                    disappeared = not play_glyphs(frame.pixels[_row_index(frame,rect)], *rect[2:])
                if not disappeared:
                    spec = None
            if spec != last:
                last, since = spec, time.monotonic()
            elif spec and time.monotonic()-since >= 1.0:
                plan = dict(schema=1, name=key, exe=binding.exe, window=title, steps=[
                    dict(action='row-action',label=f'{ordinal}번째 행 실행',row=spec)])
                try:
                    _remember(store,plan)
                except (tasks.TaskError,OSError):
                    pass  # The observed outcome remains valid if it cannot be cached.
                result = row_action._result('verified',True,'after',before='before')
                return dict(result,ordinal=ordinal,evidence=source,verification_scope='launch_control_and_row_state_changed')
        except (bridge.BridgeError,ValueError,OSError):
            last = None
    return dict(row_action._result('delivered_unverified',True,'unknown',diagnostic='row_state_not_confirmed'),
                ordinal=ordinal,evidence=source)
