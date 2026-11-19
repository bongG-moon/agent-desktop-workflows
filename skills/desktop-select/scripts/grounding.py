"""Let the host associate a recorded instruction with a freshly observed window image."""
from collections import deque
from copy import deepcopy
from hashlib import sha256
import json
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import re
import struct
import tempfile
import time
import zlib

import learned_tasks as tasks
import row_vision
import visual_target
from window_context import WindowRoutes


TTL = 600
MAX_PIXELS = 16_777_216
SIGNATURE = b'\x89PNG\r\n\x1a\n'


def _chunk(kind, data):
    return struct.pack('>I',len(data)) + kind + data + struct.pack('>I',zlib.crc32(kind+data))


def _png(width, height, bgra):
    rgb = bytearray(width*height*3)
    rgb[0::3],rgb[1::3],rgb[2::3] = bgra[2::4],bgra[1::4],bgra[0::4]
    stride = width*3
    rows = b''.join(b'\0'+rgb[offset:offset+stride] for offset in range(0,len(rgb),stride))
    header = struct.pack('>IIBBBBB',width,height,8,2,0,0,0)
    return SIGNATURE+_chunk(b'IHDR',header)+_chunk(b'IDAT',zlib.compress(rows,3))+_chunk(b'IEND',b'')


def _pixels(data):
    # Only called after verifying the digest of the exact image our writer produced.
    width,height = struct.unpack_from('>II',data,16)
    if not width or not height or width*height > MAX_PIXELS:
        raise tasks.TaskError('grounding_frame_invalid')
    length,kind = struct.unpack_from('>I4s',data,33)
    if data[:8] != SIGNATURE or kind != b'IDAT':
        raise tasks.TaskError('grounding_frame_invalid')
    stride = width*3+1
    decoder = zlib.decompressobj()
    rows = decoder.decompress(data[41:41+length],stride*height+1)
    if len(rows) != stride*height or not decoder.eof or any(rows[::stride]):
        raise tasks.TaskError('grounding_frame_invalid')
    rgb = b''.join(rows[i+1:i+stride] for i in range(0,len(rows),stride))
    bgra = bytearray(width*height*4)
    bgra[0::4],bgra[1::4],bgra[2::4] = rgb[2::3],rgb[1::3],rgb[0::3]
    return [0,0,width,height], bytes(bgra)


def _protected(uia, binding, window):
    root = uia.from_handle(binding.hwnd)
    if root is None:
        raise tasks.TaskError('grounding_window_unavailable')
    blocked, pending, count = set(), deque([root]), 0
    while pending:
        node = pending.popleft()
        count += 1
        if count > 2500:
            raise tasks.TaskError('grounding_protection_unknown')
        if visual_target.safe_provider(binding,uia,node):
            pending.extend(uia.children(node))
            continue
        if node.property(30022) is True:
            continue
        x,y,w,h = row_vision._bounds(uia,node)
        if w <= 0 or h <= 0:
            raise tasks.TaskError('grounding_protection_unknown')
        x,y = x-window[0], y-window[1]
        blocked.add((max(0,x),max(0,y),min(window[2],x+w),min(window[3],y+h)))
    return blocked


def _capture(app, binding, check):
    check()
    window = row_vision._window_rect(binding)
    affinity = W.DWORD()
    query = app.bridge._sig(app.bridge.user32,'GetWindowDisplayAffinity',W.BOOL,W.HWND,C.POINTER(W.DWORD))
    if query(binding.hwnd,C.byref(affinity)) and affinity.value:
        raise tasks.TaskError('visual_window_capture_protected')
    with app.bridge.UIA() as uia:
        protected = _protected(uia,binding,window)
        pixels = bytearray(row_vision._frame(binding,window))
        protected |= _protected(uia,binding,window)
    for left,top,right,bottom in protected:
        if right > left and bottom > top:
            mask = bytes((right-left)*4)
            for y in range(top,bottom):
                start=(y*window[2]+left)*4
                pixels[start:start+len(mask)] = mask
    check()
    if window != row_vision._window_rect(binding):
        raise tasks.TaskError('grounding_window_moved')
    return window,_png(*window[2:],pixels)


def _directory():
    folder = tasks._directory(Path(tempfile.gettempdir())/'desktop-select-grounding')
    folder.mkdir(exist_ok=True)
    for entry in folder.glob('*.png'):
        if re.fullmatch('[a-f0-9]{64}',entry.stem):
            try:
                tasks._deny_redirect(entry)
                if time.time()-entry.stat().st_mtime > TTL:
                    entry.unlink(missing_ok=True)
            except (OSError,tasks.TaskError):
                continue
    return folder


def store_frame(folder, scope, data):
    token = sha256(scope+data).hexdigest()
    path = folder/(token+'.png')
    tasks._deny_redirect(path)
    try:
        with path.open('xb') as output:
            output.write(data)
    except FileExistsError:
        # Each filename authenticates the scope and the complete PNG, never
        # overwrite an existing file or follow a replacement link.
        if path.stat().st_size!=len(data) or path.read_bytes()!=data:
            raise tasks.TaskError('grounding_frame_changed')
        path.touch()
    return token, path


def read_frame(folder, scope, token):
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{64}',token):
        raise tasks.TaskError('grounding_frame_invalid')
    path = folder/(token+'.png')
    tasks._deny_redirect(path)
    try:
        with path.open('rb') as source:
            if time.time() - os.fstat(source.fileno()).st_mtime > TTL:
                raise tasks.TaskError('grounding_frame_expired')
            data=source.read(MAX_PIXELS*4+1000)
    except FileNotFoundError as exc:
        raise tasks.TaskError('grounding_frame_expired') from exc
    if sha256(scope+data).hexdigest()!=token:
        raise tasks.TaskError('grounding_frame_changed')
    return path, data


def _usable(step):
    return (tasks.is_recoverable(step) or tasks.is_double_click_step(step) or step.get('action')=='visual-click'
            or (step.get('action')=='open' and step.get('match')=='query'))


def _children(plan, number):
    return [s['window_context'] for s in plan['steps'] if s.get('window_context', {}).get('after_step') == number]


def _row_candidate(plan, number):
    from row_intent import conflicts_with_launch
    step = plan['steps'][number-1]
    children = {(s['exe'].casefold(),s['title']) for s in _children(plan,number)}
    return (len(children)==1 and not tasks.is_double_click_step(step) and (step.get('action')=='row-launch' or _usable(step))
            and not conflicts_with_launch(_instruction(step)))


def _instruction(step):
    return (step.get('instruction_draft') or step.get('description') or step.get('instruction',{}).get('text')
            or step.get('label') or step.get('alias') or step.get('target'))


def _row_directory(plan, store):
    return tasks._directory(tasks._directory(store) / 'row-bindings' / tasks._name(plan['name']))


def row_replacements(plan, store):
    """Each grounded row is independent. Concurrent bindings of two rows never overwrite each other."""
    from recorded_plan import fingerprint
    from compound_plan import read_json
    folder = _row_directory(plan, store)
    if not folder.exists():
        return {}
    result = {}
    for n, original in enumerate(plan['steps'], 1):
        path = folder / f'{n}.json'
        tasks._deny_redirect(path)
        if not path.exists():
            continue
        import row_target
        saved = read_json(path)
        if (not isinstance(saved, dict) or set(saved) != {'source_fingerprint','number','row_target'}
                or saved['number'] != n or saved['source_fingerprint'] != fingerprint(plan)
                or not _row_candidate(plan,n)):
            raise tasks.TaskError('row_binding_source_changed')
        row_target.validate_spec(saved['row_target'])
        result[n] = dict(action='row-open', label=_instruction(original), row_target=saved['row_target'])
        if 'alias' in original:
            result[n]['alias'] = original['alias']
    return result


def _save_row(plan, store, number, spec):
    from recorded_plan import fingerprint
    from compound_plan import load
    if (any(step.get('instruction_draft') for step in plan['steps'])
            and load(plan,store).get(number,{}).get('action') == 'conditional-open'):
        raise tasks.TaskError('row_binding_conflicts_with_interpretation')
    folder = _row_directory(plan, store)
    folder.mkdir(parents=True,exist_ok=True)
    path = folder / f'{number}.json'
    signature = fingerprint(plan)
    data = dict(number=number,source_fingerprint=signature,row_target=spec)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
            temp = Path(output.name)
            output.write(json.dumps(data,ensure_ascii=False).encode('utf8'))
        _row_directory(plan,store)
        tasks._deny_redirect(path)
        if fingerprint(tasks.load_task(plan['name'],store)) != signature:
            raise tasks.TaskError('row_binding_source_changed')
        os.replace(temp,path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _save_bound_copy(doc, plan, rows, number, fingerprint, store):
    """Preserve the form of every unresolved sibling in a new draft if compilation is incomplete."""
    import recovery
    import review_drafts
    problem = next(((i, reason) for i, row in enumerate(rows or [], 1) if i != number
                    and (reason := row.get('scope_problem') or row.get('field_problem'))), None)
    if rows and not problem:
        try:
            tasks._validate(plan)
        except tasks.TaskError as error:
            problem = error.step_number, error.code
    try:
        with review_drafts._edit_lock(store):
            if recovery._fingerprint(tasks.read_document(doc['name'],store)) != fingerprint:
                raise tasks.TaskError('recording_draft_changed')
            if not problem:
                tasks.save_task(plan,store)
                return None
            draft_rows = deepcopy(doc['rows'])
            bound, row = plan['steps'][number-1], draft_rows[number-1]
            old = row['step']
            row['step'] = dict(id=old['id'],action='visual-click',label=bound['label'],visual=bound['visual'])
            if '_capture_window' in old:
                row['step']['_capture_window'] = old['_capture_window']
            row['form'].update(text=bound['label'], mode=None, argument='')
            revised = dict(doc, name=plan['name'], rows=draft_rows, diagnostic=problem[1], step_number=problem[0])
            tasks._write_document(review_drafts.validate(revised), store)
            return review_drafts.receipt(revised, 'recorded')
    except OSError as error:
        raise tasks.TaskError('task_store_unavailable') from error


def run(app, args):
    import recovery
    import review_drafts
    store = app.task_directory()
    doc = tasks.load_task(args.task,store,allow_review=True)
    is_draft = doc.get('kind') == review_drafts.KIND
    rows = recovery._rows(doc) if is_draft else None
    plan = (dict(schema=1, name=doc['name'], exe=doc['exe'], window=doc['window'],
                 steps=[row['step'] for row in rows]) if is_draft else deepcopy(doc))
    number = tasks._step_number(plan,args.only)
    step = plan['steps'][number-1]
    double = tasks.is_double_click_step(step)
    if is_draft:
        if not double:
            raise tasks.TaskError('recording_draft_requires_review')
        if rows[number-1].get('mode') in ('fixed','required') or rows[number-1].get('argument'):
            raise tasks.TaskError('instruction_binding_conflict')
        if rows[number-1].get('scope_problem'):
            raise tasks.TaskError(rows[number-1]['scope_problem'])
    children = _children(plan,number)
    if args.command == 'bind-row' and not _row_candidate(plan,number):
        raise tasks.TaskError('row_binding_requires_recorded_child')
    if not _usable(step) and not children:
        raise tasks.TaskError('grounding_requires_recoverable_target')
    sentence = (rows[number-1]['text'] if is_draft else None) or _instruction(step)
    if not sentence or sentence==tasks.LEGACY_INSTRUCTION_HINT:
        raise tasks.TaskError('grounding_instruction_required')
    if double and step.get('action') == 'manual' and not tasks.double_click_instruction(sentence):
        raise tasks.TaskError('double_click_instruction_requires_review')
    owner = app.bridge.Binding(app.pick_window(app.windows(plan['exe']),plan['window'],None))
    # A subsequent popup can legitimately refer to this as-yet-unbound opener.
    # Only the route to the step being grounded has to exist on the current screen.
    context = step.get('window_context')
    routes = WindowRoutes(plan['steps'][:number],owner,plan['window'],active=[number],read_only=True) if context else None
    binding = routes.resolve(context) if context else owner
    if binding is None:
        return dict(status='needs_grounding',diagnostic='recorded_window_not_open',step=number,
                    instruction=sentence,opener_step=(context or {}).get('after_step'),input_dispatched=False)
    def check():
        app.guard(owner,owner,plan['window'])
        if context:
            routes.check(context,binding)
    fingerprint = recovery._fingerprint(doc)
    scope = json.dumps([fingerprint,number,owner.hwnd,owner.pid,owner.created,
                        binding.hwnd,binding.pid,binding.created]).encode('ascii')
    folder = _directory()
    if args.command=='ground':
        window,data = _capture(app,binding,check)
        token, path = store_frame(folder, scope, data)
        return dict(status='observed',frame=token,image=str(path),width=window[2],height=window[3],
                    step=number,instruction=sentence,expires_seconds=TTL,input_dispatched=False,
                    **({'gesture':'double-click','host_action':'bind_demonstrated_double_click',
                        'bind_args':['bind-visual','--task',doc['name'],'--only',str(number),'--frame',token,
                                     '--point','<x>','<y>','--save-as','<new task>']} if double else {}),
                    **({'expected_windows': children} if children else {}),
                    **({'row_binding_command':'bind-row'} if _row_candidate(plan,number) else {}))
    if args.command == 'bind-visual':
        tasks.check_new_task(args.save_as,store)
    path, data = read_frame(folder, scope, args.frame)
    if args.command == 'bind-row':
        import row_target
        check()
        spec = row_target.bind(binding, data, tuple(args.row_point), tuple(args.point))
        check()
        _save_row(plan,store,number,spec)
        return dict(status='bound',task=plan['name'],step=number,input_dispatched=False,
                    execution_plan=app.execution_plan_info(app.saved_execution_plan(plan)))
    frame,pixels = _pixels(data)
    try:
        spec = visual_target.from_frame(frame,pixels,tuple(args.point))
        if double:
            spec['gesture'] = 'double-click'
        check()
        visual_target.observe(binding,spec)
    except visual_target.bridge.BridgeError as error:
        if double and error.code in ('visual_target_ambiguous','visual_target_lacks_detail','visual_target_too_common'):
            return dict(status='needs_grounding', diagnostic=error.code, input_dispatched=False,
                        host_action='choose_distinct_point_in_same_target', gesture='double-click', step=number,
                        instruction=sentence, frame=args.frame, image=str(path),
                        bind_args=['bind-visual','--task',doc['name'],'--only',str(number),'--frame',args.frame,
                                   '--point','<x>','<y>','--save-as',args.save_as])
        if error.code != 'visual_target_ambiguous' or not _row_candidate(plan,number):
            raise
        return dict(status='needs_grounding', diagnostic=error.code, input_dispatched=False,
                    host_action='ground_named_rows', grounding_steps=[
                        dict(number=number, expected_windows=children, instruction=sentence, frame=args.frame,
                             image=str(path),row_binding_command='bind-row',
                             ground_args=['ground','--task',plan['name'],'--only',str(number)])])
    check()
    step = dict(action='visual-click',label=sentence,visual=spec,**tasks._annotations(step))
    plan['steps'][number-1] = step
    plan['name'] = args.save_as
    draft_receipt = _save_bound_copy(doc, plan, rows, number, fingerprint, store)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # A previewer can keep the PNG open until the next expiry sweep.
    test = {'gesture':'double-click', 'test_step_args':['replay','--task',plan['name'],'--only',str(number),
                                                        '--exe',plan['exe'],'--window',plan['window']]} if double else {}
    if draft_receipt is not None:
        return dict(draft_receipt, bound_step=number, **test)
    return dict(status='recorded',task=plan['name'],step=number,input_dispatched=False,
                **test,
                execution_plan=app.execution_plan_info(app.saved_execution_plan(plan)))
