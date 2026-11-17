"""Bind a user-named row and its action from the same scoped window image."""
import row_action
import row_capture
import row_intent
import row_vision as vision
import coordination
import win_bridge as bridge


FIELDS = ('key', 'button')


def _require(ok, diagnostic):
    if not ok:
        raise bridge.BridgeError(diagnostic)


def validate_spec(spec):
    _require(isinstance(spec,dict) and set(spec)-{'row_id'} == {'version','size','scope','role','boxes','key'}
             and type(spec['version']) is int and spec['version']==1 and spec['role'] in ('listitem','row')
             and isinstance(spec['scope'],list) and len(spec['scope'])<=4
             and all(isinstance(item,str) and 1<=len(item)<=160 for item in spec['scope'])
             and ('row_id' not in spec or isinstance(spec['row_id'],str) and 1<=len(spec['row_id'])<=160),
             'invalid_row_target')
    _check_boxes(spec['size'],spec['boxes'])
    width, height = spec['boxes']['key'][2:]
    vision._unpack(spec['key'], width * height)
    return spec


def _check_boxes(size, boxes):
    _require(isinstance(size,list) and len(size)==2 and all(type(i) is int for i in size)
             and 1<=size[0]<=5000 and 1<=size[1]<=512, 'invalid_row_target_size')
    _require(isinstance(boxes,dict) and set(boxes)==set(FIELDS), 'invalid_row_target_boxes')
    for box in boxes.values():
        _require(isinstance(box,list) and len(box)==4 and all(type(i) is int for i in box)
                 and 3<=box[2]<=512 and 3<=box[3]<=128 and box[2]*box[3]<=32768
                 and vision._inside(box,[0,0,*size]), 'invalid_row_target_boxes')
    _require(not vision._overlaps(boxes['key'],boxes['button']), 'row_label_overlaps_button')


def _identity_box(sample, rect, point):
    # Use only text/glyphs explicitly pointed out by the host. Do not substitute
    # a different unique neighbouring column such as a timestamp or process ID.
    width, height = rect[2:]
    x, y = point[0] - rect[0], point[1] - rect[1]
    glyphs = vision._glyph(vision._rgb(sample, rect))
    matches = [box for box in row_capture._regions(width, height, glyphs)
               if vision._inside([x,y,1,1], box)]
    if len(matches) == 1:
        return matches[0]
    # Long names can exceed the region-size limit; use a bounded portion
    # centred on the chosen text (the prefix or ID is usually sufficient).
    rx, ry = min(64, max(10,height//2)), min(24, max(6,height//4))
    left, top = max(0,x-rx), max(0,y-ry)
    return [left, top, min(width,x+rx+1)-left, min(height,y+ry+1)-top]


def _identity(sample, spec):
    kind, rect, delta = vision._match(sample, spec)
    _require(kind == 'found', 'row_identity_' + kind)
    return rect, delta


def _launch(binding, sample, spec, rect, delta):
    # The recorded column may have displayed Power/Stop. Only a currently
    # identifiable launch control in this same row may receive a cold click.
    column = vision._patch(rect, spec['boxes']['button'], delta)
    point = row_intent._named_button(binding, rect)
    if point is None:
        glyphs = row_intent.play_glyphs(vision._rgb(sample,rect), *rect[2:])
        matches = [(rect[0]+x,rect[1]+y) for x,y in glyphs
                   if vision._inside([rect[0]+x,rect[1]+y,1,1], column)]
        _require(len(matches) == 1, 'row_launch_button_ambiguous' if matches else 'row_launch_button_not_found')
        point = matches[0]
    _require(vision._inside([*point,1,1], column), 'row_launch_button_outside_recorded_column')
    return dict(kind='before', row_bounds=rect, point=list(point), paint_offset=list(delta))


def bind(binding, frame_png, identity_point, action_point):
    """Both points are native bitmap coordinates in an authenticated ground frame."""
    import grounding
    frame, pixels = grounding._pixels(frame_png)
    snapshot = vision._snapshot(binding)
    window, rows, _ = snapshot
    _require(frame[2:] == window[2:], 'grounding_window_resized')
    points = []
    for point in (identity_point, action_point):
        _require(isinstance(point,(list,tuple)) and len(point)==2 and all(type(v) is int for v in point)
                 and vision._inside([*point,1,1], frame), 'invalid_row_point')
        points.append([window[0] + point[0], window[1] + point[1]])
    identity, button = points
    rect, meta = vision._point_row(rows, identity)
    _require(vision._point_row(rows, button)[0] == rect, 'row_points_in_different_rows')
    shown = (window, rows, pixels)
    boxes = dict(key=_identity_box(shown, rect, identity), button=row_capture._button(button, rect))
    _check_boxes(rect[2:], boxes)
    spec = dict(version=1, size=rect[2:], boxes=boxes, **meta)
    spec['key'] = vision._pattern(vision._rgb(shown, vision._patch(rect, boxes['key'])))
    validate_spec(spec)
    # A repeated button is valid here: its row must be unique, and both
    # pieces of evidence must fit the same current UIA row and translation.
    _require(_identity(shown, spec) == (rect,(0,0)), 'row_chosen_target_changed')
    current, delta = _identity(snapshot, spec)
    _require(current == rect, 'row_changed_since_grounding')
    # Compare the originally pointed action patch to the live frame. Do not
    # save it as an actionable pattern; a running device may show Stop here.
    original_button = vision._rgb(shown, vision._patch(rect,boxes['button']))
    live_button = vision._rgb(snapshot, vision._patch(rect,boxes['button'],delta))
    if original_button != live_button:
        _require(vision._match_distance(vision._glyph(original_button),vision._glyph(live_button),
                                        boxes['button'][2]) <= .14, 'row_button_changed_since_grounding')
    return spec


def _confirm(binding, spec, obs):
    rect, point = obs['row_bounds'], obs['point']
    window = vision._window_rect(binding)
    region = rect, (), vision._frame(binding, window, crop=rect)
    _require(all(vision._evidence(region,rect,spec,name,obs['paint_offset']) <= .14 for name in FIELDS),
             'row_pixels_changed')
    hit = bridge.window_from_point(*point)
    _require(vision._window_rect(binding)==window and bridge.root_window(bridge.foreground())==binding.hwnd
             and bridge.root_window(hit)==binding.hwnd and bridge.hwnd_pid(hit)==binding.pid,
             'row_point_not_exposed')


def execute(binding, spec, guard, *, read_only=False, child_open=None):
    """Dispatch once. The caller independently proves the exact opened window."""
    try:
        validate_spec(spec)
        guard()
        warm = bool(child_open and child_open())
        sample = vision._snapshot(binding)
        rect, delta = _identity(sample,spec)
        observed = None if warm else _launch(binding,sample,spec,rect,delta)
        if read_only:
            return dict(status='observed', input_dispatched=False, observed_state='after' if warm else 'before',
                        target_resolution='unique_row_and_window' if warm else 'unique_row_and_button')
        if warm:
            guard()
            _require(child_open(), 'recorded_window_lifetime_changed')
            return dict(status='verified', input_dispatched=False, observed_state='after',
                        evidence='recorded_row_and_window', verification_scope='recorded_window_open',
                        effect_verification='matched')
        with coordination.desktop_input():
            guard()
            row_action.foreground(binding)
            guard()
            bridge._combo_surface(binding)
            tick = row_action._input_tick()
            sample = vision._snapshot(binding)
            rect, delta = _identity(sample,spec)
            current = _launch(binding,sample,spec,rect,delta)
            _require(current == observed, 'row_changed_before_action')
            # Include the *current launch glyph* and row identity in the final
            # physical input check. Translation applies equally to both patches.
            box = row_capture._button(current['point'], rect)
            _require(box is not None, 'row_launch_button_not_found')
            button_mask = vision._pattern(vision._rgb(sample, vision._patch(rect,box)))
            box = [box[0]-delta[0],box[1]-delta[1],*box[2:]]
            runtime_spec = dict(spec, boxes=dict(spec['boxes'],button=box), button=button_mask)
            vision.confirm_point(binding, current)
            guard()
            _require(not (child_open and child_open()), 'expected_window_opened_before_click')
            bridge._combo_surface(binding)
            _confirm(binding, runtime_spec, current)
            _require(row_action._input_tick() == tick, 'foreground_or_input_changed')
            try:
                result = bridge._send_point(*current['point'], 'click')
            except Exception as error:
                # Once the transport was entered even an unclassified error may mean
                # Windows received the input. Never offer a second attempt here.
                result = dict(ok=False, input_dispatched=getattr(error,'input_dispatched',None),
                              diagnostic=getattr(error,'code','row_delivery_uncertain'))
    except (bridge.BridgeError, OSError, ValueError) as error:
        return dict(status='blocked', input_dispatched=False, diagnostic=getattr(error,'code','row_target_unavailable'))
    sent = result.get('input_dispatched')
    delivered = sent is True and result.get('ok') is True
    return dict(status='delivered_unverified' if delivered else 'uncertain', input_dispatched=sent,
                diagnostic='row_open_dispatched' if delivered else result.get('diagnostic','row_delivery_uncertain'),
                verification_scope='click_delivery', effect_verification='unverified',
                input_mode='physical_pointer', physical_input=sent is not False)
