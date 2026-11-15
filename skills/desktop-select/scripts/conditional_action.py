"""Interpret a bounded wait, optional dismissal and unconditional open in one captured window."""
import time

import grounding
import learned_tasks as tasks
import visual_target as visual
from win_bridge import BridgeError


def validate_conditional(step):
    required = {'action','label','seconds','when_present','dismiss','always'}
    if (not isinstance(step,dict) or not required <= step.keys() or step.keys() - required - {'window_context','alias'}
            or step['action'] != 'conditional-open' or not tasks._text(step['label'],1000)
            or not (type(step['seconds']) in (int,float) and 0 <= step['seconds'] <= 120)):
        raise tasks.TaskError('invalid_conditional_action')
    tasks._annotations(step)
    for key in ('when_present','dismiss','always'):
        marker = step[key]
        if (not isinstance(marker,dict) or set(marker) not in ({'name'}, {'name','visual'})
                or not tasks._text(marker['name'],200)):
            raise tasks.TaskError('invalid_conditional_marker')
        if 'visual' in marker:
            visual.validate_spec(marker['visual'])
    return step


def _point(frame, marker):
    if 'visual' not in marker:
        return None
    try:
        return visual._locate(*frame, marker['visual'])[1]
    except BridgeError as error:
        if error.code == 'visual_target_not_found':
            return None
        raise


def _wait(seconds,guard):
    deadline = time.monotonic() + seconds
    while True:
        guard()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.25, remaining))


def _dismiss_region(frame, point):
    """Keep context around a close glyph, including distinct nearby panel detail."""
    box, pixels = frame
    x,y = point
    radius = 72
    left,top=max(0,x-radius),max(0,y-radius)
    right,bottom=min(box[2],x+radius),min(box[3],y+radius)
    rows = []
    colors = set()
    for row in range(top,bottom):
        start = (row*box[2] + left)*4
        strip = pixels[start:start+(right-left)*4]
        rows.append(strip)
        if abs(row-y)>visual.SIDE//2 and row%4==0:
            colors.update(strip[i:i+3] for i in range(0,len(strip),16))
    return b''.join(rows) if len(colors)>=12 else None


def _fresh(binding, original, png, guard, app, *, dismiss_point=None):
    # A still-visible icon does not prove that the rest of the scene is unchanged.
    # Re-read the entire private window, including UIA protection masks.
    box, current = grounding._capture(app,binding,guard)
    if box == original:
        if current == png:
            return
        if dismiss_point is not None:
            # Ad video/countdowns elsewhere can keep painting. For a positive
            # dismissal only, keep the exact local point and its broader panel
            # context, in addition to the independent visual + native hit guards.
            region = _dismiss_region(grounding._pixels(png),dismiss_point)
            if region and region == _dismiss_region(grounding._pixels(current),dismiss_point):
                return
    raise tasks.TaskError('conditional_scene_changed')


def execute(binding, step, guard, session, progress, number, title):
    import desktop_select as app
    import row_action
    from python_workflow import combine
    dispatched = False
    clicks = []
    dismissed = False
    decision = None
    def receipt(status, diagnostic):
        return dict(status=status,diagnostic=diagnostic,input_dispatched=dispatched,
                    waited_seconds=step['seconds'],condition=decision,actions=clicks,
                    physical_input=dispatched is not False,business_commit='not_verified')
    try:
        _wait(step['seconds'],guard)
        # Activate before offering the screenshot so normal activation chrome
        # is already in the user's observed scene during revalidation.
        guard()
        row_action.foreground(binding)
        for _ in range(8):
            window,png = grounding._capture(app,binding,guard)
            frame = grounding._pixels(png)
            target = None
            # Two independently taught markers can identify a known intervening
            # panel and its close target. Absence of an element never proves absence of a panel.
            if not dismissed and _point(frame,step['when_present']) is not None:
                target = _point(frame,step['dismiss'])
            if target is not None:
                branch = 'present'
            else:
                phase = 'after_dismiss' if dismissed else 'condition'
                pending = dict(step=number, phase=phase, window=title, width=window[2],height=window[3],
                               instruction=step['label'], condition_name=step['when_present']['name'],
                               close_target=step['dismiss']['name'], final_target=step['always']['name'])
                answer = session.ask(png,pending,dict(progress,input_dispatched=combine(progress['input_dispatched'],dispatched),
                                                      current_step_actions=list(clicks)))
                branch,target = answer['decision'],answer['point']
                if branch == 'unknown':
                    return receipt('blocked','conditional_scene_unresolved')
                if dismissed and branch == 'present':
                    return receipt('blocked','condition_still_present_after_dismiss')
            if branch not in ('present','absent') or not (isinstance(target,(list,tuple)) and len(target)==2
                    and all(type(c) is int for c in target) and 0 <= target[0] < window[2] and 0 <= target[1] < window[3]):
                raise tasks.TaskError('invalid_scene_answer')
            marker = step['dismiss'] if branch == 'present' else step['always']
            match = _point(frame,marker)
            if match is not None:
                if max(abs(match[i]-target[i]) for i in (0,1)) > visual.SIDE//2:
                    return receipt('blocked','scene_point_conflicts_with_target')
                spec = marker['visual']
            else:
                spec = visual.from_frame(*frame,target)
            def recheck(point):
                if branch == 'present':
                    if max(abs(point[i]-window[i]-(match or target)[i]) for i in (0,1))>2:
                        raise tasks.TaskError('conditional_scene_changed')
                    _reject_window_chrome(binding, point, app)
                _fresh(binding,window,png,guard,app,dismiss_point=(match or target) if branch=='present' else None)
            click = visual.execute(binding,spec,guard,before_input=recheck)
            if click.get('input_dispatched') is False and click.get('diagnostic') in (
                    'conditional_scene_changed','visual_target_not_found','visual_target_changed_before_input'):
                # Nothing was sent. Ask the host about the new frame; never
                # retry a dispatch that might already have reached the app.
                continue
            clicks.append(dict(action='dismiss' if branch=='present' else 'open',**click))
            dispatched = combine(dispatched,click['input_dispatched'])
            if not (click['status']=='delivered_unverified' and click['input_dispatched'] is True
                    and click.get('diagnostic')=='visual_click_dispatched'):
                return receipt(click['status'],click.get('diagnostic','conditional_click_unconfirmed'))
            if branch == 'absent':
                decision = 'present' if dismissed else 'absent'
                return receipt('delivered_unverified','conditional_open_dispatched')
            decision,dismissed = 'present',True
            _wait(.35,guard)
        return receipt('blocked','conditional_scene_unstable')
    except (OSError,ValueError,BridgeError) as error:
        return receipt('blocked',getattr(error,'code','conditional_scene_unavailable'))


def _reject_window_chrome(binding, point, app):
    """Require a client hit; never treat Windows caption buttons as in-app dismissals."""
    import ctypes as C
    from ctypes import wintypes as W
    win=app.bridge
    hit=win.window_from_point(*point)
    result=C.c_size_t()
    send=win._sig(win.user32,'SendMessageTimeoutW',C.c_ssize_t,W.HWND,W.UINT,W.WPARAM,W.LPARAM,W.UINT,W.UINT,
                  C.POINTER(C.c_size_t))
    packed=(point[0]&0xffff) | ((point[1]&0xffff)<<16)
    for hwnd in set((hit,binding.hwnd)):
        if not hwnd or not send(hwnd,0x84,0,packed,2,200,C.byref(result)) or result.value != 1:
            raise tasks.TaskError('conditional_native_chrome')
    with win.UIA() as uia:
        node,root=uia.from_point(*point),uia.from_handle(binding.hwnd)
        for _ in range(48):
            if node is None or node.property(30003) == 50037:  # UIA_TitleBarControlTypeId
                break
            if root is not None and uia.same(node,root):
                return
            node=uia.parent(node)
    raise tasks.TaskError('conditional_native_chrome')
