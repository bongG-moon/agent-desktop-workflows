"""Teach a result-area context menu from a demonstrated point in a local window."""
from __future__ import annotations

import time
import uuid


def menu_path(lines):
    names = [line.strip() for line in lines.splitlines() if line.strip()]
    if not names:
        raise ValueError('상위 메뉴부터 실행할 마지막 항목까지 한 줄에 하나씩 적으세요.')
    return names


def raw_result_step(label, menu, trigger=None, ready=None, output=None):
    import result_menu
    spec = {'trigger': trigger, 'ready': ready, 'menu': menu, 'output': output}
    result_menu.validate_spec(spec)
    label = label.strip() or ' → '.join(menu['path'])
    if len(label) > 200 or any(ord(c) < 32 for c in label):
        raise ValueError('단계 이름에 200자 이내의 한 줄을 입력하세요.')
    return {'id': uuid.uuid4().hex, 'action': 'result-menu', 'label': label, 'context': spec}


def previous_trigger(step, context):
    """An immediately preceding, identical demonstrated button can be merged on review."""
    target = step.get('target', {})
    trigger = context.get('trigger')
    return bool(trigger and step.get('action') == 'click' and target.get('kind') == 'element'
                and 'window' not in target and all(target.get('locator', {}).get(k) == v
                                                    for k, v in trigger.items()))


def _pointed_control(binding, point, mode):
    import desktop_select as app
    from result_menu import TEXT_ROLES
    b = app.bridge
    x, y = point
    hit = b.window_from_point(x, y)
    if b.root_window(hit) != binding.hwnd or b.hwnd_pid(hit) != binding.pid:
        raise ValueError('업무 창이 보이는 상태에서 그 창의 요소를 가리키세요.')
    with b.UIA() as uia:
        nodes, elements = app.observe(binding, uia, identities_only=True)
        root, current = elements[0], uia.from_point(x, y)
        ancestor = current
        for _ in range(32):
            if ancestor is None or uia.secure(ancestor) or uia.pid(ancestor) != binding.pid:
                raise ValueError('선택한 업무 창 안에서 공개된 요소를 가리키세요.')
            if uia.same(ancestor, root):
                break
            ancestor = uia.parent(ancestor)
        else:
            raise ValueError('선택한 업무 창의 요소를 확인할 수 없습니다.')
        for _ in range(24):
            if not current or uia.same(current, root) or uia.secure(current) or uia.pid(current) != binding.pid:
                break
            basic = uia.basic(current)
            key = 'automation_id' if basic.get('automation_id') else 'name'
            role = basic['role']
            valid_role = role == 'button' if mode == 'trigger' else role in TEXT_ROLES
            if valid_role and basic.get(key):
                locator = {'role': role, key: basic[key]}
                matches = [i for i, n in enumerate(nodes) if all(n.get(k) == v for k, v in locator.items())]
                if len(matches) != 1 or not uia.same(elements[matches[0]], current):
                    raise ValueError('같은 이름의 요소가 여러 개입니다. 고유한 표시나 버튼을 가리키세요.')
                if current.property(30010) is not True or current.property(30022) is not False:
                    raise ValueError('표시되고 사용 가능한 요소를 가리키세요.')
                if mode == 'trigger' and 'Click' not in uia.actions(current):
                    raise ValueError('조회 버튼에서 실행 동작을 확인할 수 없습니다.')
                value = uia._pattern_value(current) if mode == 'ready' else None
                return locator, value if isinstance(value, str) and value.strip() else basic.get('name', '')
            current = uia.parent(current)
    raise ValueError('고유한 조회 버튼이나 상태 표시를 가리키세요.')


def _read_sample(sender, window, title, op, args):
    try:
        import desktop_select as app
        import context_menu
        import result_menu
        app.bridge.set_dpi_awareness()
        binding = app.bridge.Binding(window)
        app.guard(binding, binding, title)
        if op in ('trigger', 'ready'):
            value = _pointed_control(binding, args[0], op)
        elif op == 'menu':
            value = context_menu.teach_at(binding, *args)
        elif op == 'visual-preview':
            value = result_menu.visual_preview(binding, *args)
        elif op == 'visual':
            locator, box, preview = args
            value = result_menu.teach_visual(binding, locator, box, preview=preview)
        else:
            raise ValueError('unknown_sample')
        app.guard(binding, binding, title)
        sender.send((True, value))
    except Exception as error:
        sender.send((False, getattr(error, 'code', str(error))))
    finally:
        sender.close()


def _display(locator):
    return (locator.get('automation_id') or locator.get('name', '')) + ' (' + locator['role'] + ')'


def teach_context_dialog(parent, binding):
    import tkinter as tk
    from tkinter import ttk, filedialog
    from row_teach import SampleJob, selection_box

    win = tk.Toplevel(parent)
    win.title('조회 결과의 우클릭 메뉴 가르치기')
    form = ttk.Frame(win, padding=12)
    form.pack(fill='both', expand=True)
    form.columnconfigure(1, weight=1)
    target_text = tk.StringVar(win, '등록 전')
    trigger_text = tk.StringVar(win, '버튼 없이 현재 결과 사용')
    ready_text = tk.StringVar(win, '등록 전')
    expected = tk.StringVar(win)
    name = tk.StringVar(win)
    output_kind = tk.StringVar(win, '메뉴 전달만 확인')
    output_path = tk.StringVar(win)
    message = tk.StringVar(win, '조회 버튼을 가리킨 다음 업무 화면에서 조회를 한 번 시연하세요. 결과가 보이면 우클릭할 지점을 지정하세요.')
    trigger = menu = readiness = result = None
    job = pending = None

    def text(text, row, col=0, **kw):
        ttk.Label(form, text=text, **kw).grid(row=row, column=col, sticky='w', pady=3, padx=(0, 6))

    def value(textvar, row, col=1):
        ttk.Label(form, textvariable=textvar, wraplength=440).grid(row=row, column=col, sticky='w', pady=3)

    def close():
        if pending:
            win.after_cancel(pending)
        if job:
            job.close()
        win.destroy()

    def sample(op, args, apply, wait=0):
        nonlocal pending, job
        if job or pending:
            return
        win.withdraw()

        def poll():
            nonlocal pending, job
            pending = None
            try:
                if job is None:
                    job = SampleJob(binding, op, args(), reader=_read_sample)
                answer = job.poll()
                if answer is None:
                    if time.monotonic() - job.started > 2 and win.state() == 'withdrawn':
                        message.set('업무 화면에서 읽는 중입니다. 취소하면 이번 단계만 닫습니다.')
                        win.deiconify()
                    pending = win.after(80, poll)
                    return
                job.close()
                job = None
                win.deiconify()
                win.lift()
                apply(answer)
            except (RuntimeError, OSError, ValueError, tk.TclError) as exc:
                if job:
                    job.close()
                    job = None
                message.set('읽은 대상을 확인하세요. ' + str(exc))
                win.deiconify()
                win.lift()
        pending = win.after(wait, poll)

    def clear_trigger():
        nonlocal trigger, readiness
        trigger = readiness = None
        trigger_text.set('버튼 없이 현재 결과 사용')
        ready_text.set('등록 전')
        expected.set('')

    def set_trigger(answer):
        nonlocal trigger
        clear_trigger()
        trigger = answer[0]
        trigger_text.set(_display(trigger))
        message.set('업무 화면에서 직접 조회하세요. 결과가 준비되면 완료 글자 또는 모양을 지정하세요.')

    def set_menu(answer):
        nonlocal menu, readiness
        if readiness and readiness['kind'] == 'visual':
            readiness = None
            ready_text.set('표 위치를 다시 지정했습니다. 완료 모양도 다시 선택하세요.')
        menu = answer
        x, y = menu['point']
        target_text.set(f"{_display(menu['target'])} / 왼쪽에서 {x:.0%}, 위에서 {y:.0%}")
        message.set('표와 우클릭 위치를 확인하세요. 조회 버튼을 지정했다면 완료 표시도 지정하세요.')

    def set_text_ready(answer):
        nonlocal readiness
        locator, observed = answer
        readiness = {'kind': 'text', 'target': locator}
        ready_text.set(_display(locator))
        expected.set(observed)
        message.set('조회가 끝났을 때만 보이는 기대 글자를 확인하거나 직접 적으세요.')

    def open_visual():
        if not menu or not trigger:
            message.set('먼저 조회 버튼과 결과 표의 우클릭 위치를 지정하세요.')
            return
        sample('visual-preview', lambda: (menu['target'],), select_visual)

    def select_visual(answer):
        rect, ppm = answer
        view = tk.Toplevel(win)
        view.title('조회 완료 표시를 드래그하세요')
        original = tk.PhotoImage(master=view, data=ppm, format='PPM')
        scale = max(1, (rect[2] + 899)//900, (rect[3] + 479)//480)
        scaled = original.subsample(scale, scale)
        ttk.Label(view, text='조회 완료 후 고정된 작은 표시 하나를 드래그하세요.').pack(anchor='w')
        canvas = tk.Canvas(view, width=scaled.width(), height=scaled.height(), highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, image=scaled, anchor='nw')
        start = None
        def mark(e):
            nonlocal start
            start = e.x, e.y
        def draw(e):
            if start:
                canvas.delete('box')
                canvas.create_rectangle(*start, e.x, e.y, outline='red', width=2, tags='box')
        def complete(e):
            if not start:
                return
            try:
                box = selection_box(tuple(x*scale for x in start), (e.x*scale, e.y*scale), rect[2:])
            except ValueError:
                return
            view.destroy()
            sample('visual', lambda: (menu['target'], box, answer), set_visual)
        canvas.bind('<Button-1>', mark)
        canvas.bind('<B1-Motion>', draw)
        canvas.bind('<ButtonRelease-1>', complete)
        ttk.Button(view, text='취소', command=view.destroy).pack()
        view.transient(win)
        view.grab_set()
        win.wait_window(view)

    def set_visual(answer):
        nonlocal readiness
        readiness = answer
        ready_text.set(f"표 안의 완료 모양 ({answer['box'][2]} × {answer['box'][3]})")
        expected.set('')
        message.set('실행 전 상태에서 완료 모양이 이미 보이면 조회 갱신을 구분할 수 없습니다. 실제 완료 표식을 지정하세요.')

    text('조회 버튼', 0)
    value(trigger_text, 0)
    bar = ttk.Frame(form)
    bar.grid(row=1, column=1, sticky='w')
    def point():
        return win.winfo_pointerxy()
    ttk.Button(bar, text='조회 버튼 가리키기 (3초)', command=lambda: sample(
        'trigger', lambda: (point(),), set_trigger, 3000)).pack(side='left')
    ttk.Button(bar, text='조회 버튼 생략', command=clear_trigger).pack(side='left', padx=4)
    text('메뉴 경로', 2)
    names = tk.Text(form, height=3, width=45, wrap='none')
    names.grid(row=2, column=1, sticky='ew', pady=3)
    text('상위 메뉴부터 한 줄씩 입력하세요. 예: 첫 줄 EXPORT, 다음 줄 EXPORT TO EXCEL', 3, 1)
    def teach_point():
        try:
            path = menu_path(names.get('1.0', 'end-1c'))
        except ValueError as exc:
            message.set(str(exc))
            return
        sample('menu', lambda: (point(), path), set_menu, 3000)
    text('우클릭할 표 위치', 4)
    value(target_text, 4)
    ttk.Button(form, text='데이터가 있는 지점 가리키기 (3초)', command=teach_point).grid(row=5, column=1, sticky='w')
    text('조회 완료 표시', 6)
    value(ready_text, 6)
    ready_bar = ttk.Frame(form)
    ready_bar.grid(row=7, column=1, sticky='w')
    ttk.Button(ready_bar, text='상태 글자 가리키기 (3초)', command=lambda: sample(
        'ready', lambda: (point(),), set_text_ready, 3000) if trigger else message.set('먼저 조회 버튼을 지정하세요.')).pack(side='left')
    ttk.Button(ready_bar, text='표 안의 완료 모양 고르기', command=open_visual).pack(side='left', padx=4)
    text('기대하는 상태 글자', 8)
    ttk.Entry(form, textvariable=expected).grid(row=8, column=1, sticky='ew')
    text('내보내기 결과', 9)
    kinds = ('메뉴 전달만 확인', '지정 파일', '폴더의 새 Excel 파일')
    ttk.Combobox(form, values=kinds, textvariable=output_kind, state='readonly').grid(row=9, column=1, sticky='ew')
    file_bar = ttk.Frame(form)
    file_bar.grid(row=10, column=1, sticky='ew')
    def browse():
        if output_kind.get() == kinds[0]:
            output_kind.set(kinds[2])
        path = (filedialog.askdirectory(parent=win, mustexist=True, title='앱에서 Excel 파일을 생성하는 폴더') if output_kind.get() == kinds[2] else
                filedialog.asksaveasfilename(parent=win, title='앱에서 생성할 Excel 파일 경로', confirmoverwrite=False, defaultextension='.xlsx',
                                             filetypes=[('Excel 통합 문서', '*.xlsx')]))
        if path:
            output_path.set(path)
    ttk.Entry(file_bar, textvariable=output_path).pack(side='left', fill='x', expand=True)
    ttk.Button(file_bar, text='경로 선택', command=browse).pack(side='left', padx=(4, 0))
    text('이 단계의 이름', 11)
    ttk.Entry(form, textvariable=name).grid(row=11, column=1, sticky='ew')
    ttk.Label(form, textvariable=message, wraplength=540).grid(row=12, column=0, columnspan=2, sticky='w', pady=8)

    def accept():
        nonlocal result
        if pending or job:
            return
        try:
            if not menu:
                raise ValueError('결과 표의 우클릭 지점을 지정하세요.')
            if trigger and not readiness:
                raise ValueError('조회 완료 상태를 글자나 모양으로 지정하세요.')
            ready = readiness
            if ready and ready['kind'] == 'text':
                ready = dict(ready, expected=expected.get().strip())
            output = None
            if output_kind.get() != kinds[0]:
                output = {'kind': 'file' if output_kind.get() == kinds[1] else 'directory', 'path': output_path.get().strip()}
            result = raw_result_step(name.get(), dict(menu, path=menu_path(names.get('1.0', 'end-1c'))), trigger, ready, output)
        except (ValueError, RuntimeError) as exc:
            message.set('단계를 추가하기 전에 다음 내용을 확인하세요. ' + str(exc))
            return
        close()
    buttons = ttk.Frame(form)
    buttons.grid(row=13, column=1, sticky='e')
    ttk.Button(buttons, text='단계에 추가', command=accept).pack(side='left')
    ttk.Button(buttons, text='취소', command=close).pack(side='left', padx=4)
    parent.withdraw()
    win.protocol('WM_DELETE_WINDOW', close)
    try:
        parent.wait_window(win)
        return result
    finally:
        if parent.winfo_exists():
            parent.deiconify()
            parent.lift()
