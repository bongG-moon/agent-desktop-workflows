"""Let the host associate a recorded instruction with a freshly observed window image."""
from collections import deque
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


def _usable(step):
    return (tasks.is_recoverable(step) or step.get('action')=='visual-click'
            or (step.get('action')=='open' and step.get('match')=='query'))


def run(app, args):
    from recorded_plan import fingerprint
    store = app.task_directory()
    plan = tasks.load_task(args.task,store)
    number = tasks._step_number(plan,args.only)
    step = plan['steps'][number-1]
    if not _usable(step):
        raise tasks.TaskError('grounding_requires_recoverable_target')
    sentence = (step.get('instruction_draft') or step.get('description') or step.get('instruction',{}).get('text')
                or (step.get('label') if step.get('action')=='visual-click' else None) or step.get('alias'))
    if not sentence or sentence==tasks.LEGACY_INSTRUCTION_HINT:
        raise tasks.TaskError('grounding_instruction_required')
    owner = app.bridge.Binding(app.pick_window(app.windows(plan['exe']),plan['window'],None))
    routes = WindowRoutes(plan['steps'],owner,plan['window'],active=[number],read_only=True)
    context = step.get('window_context')
    binding = routes.resolve(context) if context else owner
    if binding is None:
        return dict(status='needs_grounding',diagnostic='recorded_window_not_open',step=number,
                    instruction=sentence,opener_step=(context or {}).get('after_step'),input_dispatched=False)
    def check():
        app.guard(owner,owner,plan['window'])
        if context:
            routes.check(context,binding)
    scope = json.dumps([fingerprint(plan),number,owner.hwnd,owner.pid,owner.created,
                        binding.hwnd,binding.pid,binding.created]).encode('ascii')
    folder = _directory()
    if args.command=='ground':
        window,data = _capture(app,binding,check)
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
        return dict(status='observed',frame=token,image=str(path),width=window[2],height=window[3],
                    step=number,instruction=sentence,expires_seconds=TTL,input_dispatched=False)
    tasks.check_new_task(args.save_as,store)
    if not re.fullmatch('[a-f0-9]{64}',args.frame):
        raise tasks.TaskError('grounding_frame_invalid')
    path = folder/(args.frame+'.png')
    tasks._deny_redirect(path)
    try:
        with path.open('rb') as source:
            if time.time() - os.fstat(source.fileno()).st_mtime > TTL:
                raise tasks.TaskError('grounding_frame_expired')
            data=source.read(MAX_PIXELS*4+1000)
    except FileNotFoundError as exc:
        raise tasks.TaskError('grounding_frame_expired') from exc
    if sha256(scope+data).hexdigest()!=args.frame:
        raise tasks.TaskError('grounding_frame_changed')
    frame,pixels = _pixels(data)
    spec = visual_target.from_frame(frame,pixels,tuple(args.point))
    check()
    visual_target.observe(binding,spec)
    check()
    step = dict(action='visual-click',label=sentence,visual=spec,**tasks._annotations(step))
    plan['steps'][number-1] = step
    plan['name'] = args.save_as
    tasks.save_task(plan,store)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # A previewer can keep the PNG open until the next expiry sweep.
    return dict(status='recorded',task=plan['name'],step=number,input_dispatched=False,
                execution_plan=app.execution_plan_info(app.saved_execution_plan(plan)))
