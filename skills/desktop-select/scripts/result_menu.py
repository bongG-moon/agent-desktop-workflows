"""One taught query, one fresh completion signal, and one scoped context-menu route."""
from types import SimpleNamespace
import time

import learned_tasks as tasks
import export_evidence
import context_menu
import row_vision as pixels
import win_bridge as bridge
from provider_visibility import visible


def _need(test, reason):
    if not test:
        raise tasks.TaskError(reason)


TEXT_ROLES = {'statictext', 'textfield', 'statusbar', 'group', 'custom', 'progressbar'}


def _locator(value, roles):
    _need(isinstance(value, dict) and set(value) in ({'role', 'automation_id'}, {'role', 'name'}) and
          isinstance(value['role'], str) and value['role'] in roles, 'invalid_result_locator')
    key = value.get('automation_id', value.get('name'))
    _need(tasks._text(key, 512) and not ('automation_id' in value and key.isdecimal()), 'invalid_result_locator')


def _box(size, box):
    _need(isinstance(size, list) and len(size) == 2 and all(type(x) is int and 0 < x <= 12000 for x in size)
          and isinstance(box, list) and len(box) == 4 and all(type(x) is int for x in box)
          and pixels._inside(box, [0, 0, *size]) and 4 <= box[2] <= 512 and 4 <= box[3] <= 160
          and box[2]*box[3] <= 32768, 'invalid_result_patch')


def validate_spec(spec):
    _need(isinstance(spec, dict) and set(spec) == {'trigger', 'ready', 'menu', 'output'}, 'invalid_result_menu')
    try:
        context_menu.validate_spec(spec['menu'])
        trigger, ready = spec['trigger'], spec['ready']
        if trigger is not None:
            _locator(trigger, {'button'})
            _need(ready is not None, 'query_completion_required')
        if ready is not None:
            _need(isinstance(ready, dict), 'invalid_result_condition')
            if ready.get('kind') == 'text':
                _need(set(ready) == {'kind', 'target', 'expected'} and tasks._text(ready['expected'], 512),
                      'invalid_result_condition')
                _locator(ready['target'], TEXT_ROLES)
            else:
                _need(set(ready) == {'kind', 'target', 'box', 'size', 'sample'} and ready['kind'] == 'visual'
                      and ready['target'] == spec['menu']['target'], 'invalid_result_condition')
                _box(ready['size'], ready['box'])
                pixels._unpack(ready['sample'], ready['box'][2]*ready['box'][3])
        if spec['output'] is not None:
            export_evidence.validate(spec['output'])
    except bridge.BridgeError as e:
        raise tasks.TaskError(e.code) from e


def _resolve(binding, uia, locator):
    from desktop_select import observe
    nodes, elements = observe(binding, uia, identities_only=True)
    hits = [i for i,n in enumerate(nodes) if not n['secure'] and all(n.get(k)==v for k,v in locator.items())]
    if not hits and any(n['secure'] for n in nodes):
        raise tasks.TaskError('result_indicator_cannot_be_resolved_safely')
    _need(len(hits) == 1, 'result_indicator_ambiguous' if hits else 'result_indicator_missing')
    e = elements[hits[0]]
    _need(visible(binding, uia, e, elements[0]) and uia.within(e, elements[0]), 'result_indicator_not_visible')
    return e, [(n, element) for n,element in zip(nodes,elements) if n['secure']]


def _visual(binding, locator, box=None):
    _locator(locator, context_menu.REGIONS)
    window = pixels._window_rect(binding)
    with bridge.UIA() as uia:
        e, protected = _resolve(binding, uia, locator)
        rect = pixels._bounds(uia, e)
        _need(pixels._inside(rect, window) and rect[2]*rect[3] <= 4_194_304, 'result_region_clipped')
        if box is not None:
            _box(rect[2:], box)
        crop = pixels._patch(rect, box) if box is not None else rect
        for _, sensitive in protected:
            r = pixels._bounds(uia, sensitive)
            _need(not pixels._overlaps(r, crop) and (r[2]*r[3] or sensitive.property(30022) is True),
                  'result_region_protected')
        frame = pixels._frame(binding, window)
        _need(window == pixels._window_rect(binding) and rect == pixels._bounds(uia, e), 'result_region_moved')
        return rect, pixels._rgb((window, None, frame), crop)


def visual_preview(binding, locator):
    rect, rgb = _visual(binding, locator)
    return rect, pixels._ppm_header(rect[2:]) + rgb


def teach_visual(binding, locator, box, *, preview=None):
    box = list(box)
    rect, rgb = _visual(binding, locator, box)
    if preview is not None:
        old, ppm = preview
        head = pixels._ppm_header(rect[2:])
        _need(list(old) == rect and ppm.startswith(head) and len(ppm) == len(head)+rect[2]*rect[3]*3,
              'result_preview_changed')
        x,y,w,h = box
        offset = len(head) + (y*rect[2]+x)*3
        stride = rect[2]*3
        shown = b''.join(ppm[offset+i*stride:offset+i*stride+w*3] for i in range(h))
        _need(pixels._match_distance(pixels._glyph(shown), pixels._glyph(rgb), w) <= .12, 'result_preview_changed')
    return dict(kind='visual', target=locator, box=box, size=rect[2:], sample=pixels._pattern(rgb))


def _matches(binding, ready):
    if ready['kind'] == 'visual':
        rect, rgb = _visual(binding, ready['target'], ready['box'])
        _need(rect[2:] == ready['size'], 'result_region_resized')
        w,h = ready['box'][2:]
        return pixels._match_distance(pixels._unpack(ready['sample'], w*h), pixels._glyph(rgb), w) <= .12
    with bridge.UIA() as uia:
        e,_ = _resolve(binding, uia, ready['target'])
        value = uia._pattern_value(e)
        actual = value if isinstance(value, str) and value.strip() else e.property(30005)
        _need(isinstance(actual, str), 'result_text_unavailable')
        return actual.strip() == ready['expected'].strip()


def _wait_for_ready(read, *, needs_transition, timeout=24, pause=.18):
    deadline = time.monotonic() + timeout
    streak = 0
    while True:
        if read():
            streak += 1
            if not needs_transition and streak >= 2:
                return True
        else:
            needs_transition, streak = False, 0
        if time.monotonic() >= deadline:
            return False
        time.sleep(pause)


def _query(binding, locator, title):
    from desktop_select import action
    key = 'automation_id' if 'automation_id' in locator else 'name'
    step = dict(action='open', target=locator[key], match=key, role='button')
    args = SimpleNamespace(command='open', role='button', window=title, target=locator[key],
                           expand=False, within=None, popup=None)
    return action(args, binding, learned=step)


def execute(binding, spec, guard, title, *, observe_guard=None):
    validate_spec(spec)
    ready, trigger, output = spec['ready'], spec['trigger'], spec['output']
    info = dict(input_dispatched=False, query_dispatched=False, query_verified=False,
                leaf_dispatched=False, menu_path_completed=False, effect_verification='unavailable')
    def read():
        (observe_guard or guard)()
        try:
            return _matches(binding, ready)
        except tasks.TaskError as error:
            # In a complete unprotected tree a result indicator can disappear during a query
            # or be created only after the first result. Ambiguity and read errors do not count.
            if error.code in ('result_indicator_missing', 'result_indicator_not_visible'):
                return False
            raise
    before = None
    def before_leaf():
        nonlocal before
        if output:
            before = export_evidence.baseline(output)
    try:
        guard()
        if output:
            export_evidence.inventory(output)  # Reject misconfigured or inaccessible output before any input.
        if trigger:
            already_ready = read()
            info.update(input_dispatched=None, query_dispatched=None)
            q = _query(binding, trigger, title)
            info.update(input_dispatched=q['input_dispatched'], query_dispatched=q['input_dispatched'])
            if q['input_dispatched'] is not True or q['status'] not in ('verified', 'delivered_unverified'):
                return dict(info, status=q['status'] if q['status'] != 'verified' else 'blocked',
                            diagnostic=q['diagnostic'])
        if ready:
            if not _wait_for_ready(read, needs_transition=bool(trigger and already_ready)):
                return dict(info, status='delivered_unverified' if trigger else 'blocked',
                            diagnostic='query_fresh_result_not_observed')
            info['query_verified'] = bool(trigger)
        guard()
        menu = context_menu.execute(binding, spec['menu'], guard, before_leaf=before_leaf)
        sent = info['input_dispatched']
        info.update(menu)
        if sent is True:
            info['input_dispatched'] = True
        if menu['status'] != 'delivered_unverified' or menu.get('leaf_dispatched') is not True:
            return info
        if not output:
            return dict(info, diagnostic='menu_delivered_no_output_condition')
        _need(before is not None, 'export_baseline_missing')
        ok, diagnostic = export_evidence.wait(output, before)
        return dict(info, status='verified' if ok else 'delivered_unverified', diagnostic=diagnostic,
                    effect_verification='matched' if ok else 'unavailable', workbook_verified=ok)
    except Exception as error:
        sent = info['input_dispatched']
        return dict(info, status='uncertain' if sent is None else 'delivered_unverified' if sent else 'blocked',
                    diagnostic=getattr(error, 'code', 'result_evidence_unavailable'))
