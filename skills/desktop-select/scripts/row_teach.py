"""Teach a button and a visible outcome on the same row, without sending input."""
from __future__ import annotations

import uuid
import multiprocessing as mp
import time


PARTS = ('행 식별 표시', '버튼', '상태 표시')
COLORS = ('#007e87', '#a63f00', '#6d44b1')


def _read_sample(sender, target, title, op, args):
    """Contain an unresponsive UIA provider or PrintWindow in this reader."""
    try:
        import desktop_select as app
        import row_vision
        app.bridge.set_dpi_awareness()
        binding = app.bridge.Binding(target)
        app.guard(binding, binding, title)
        if op == 'preview':
            value = row_vision.row_preview(binding, args[0])
        elif op == 'before':
            point, boxes, preview = args
            value = row_vision.teach_before(binding, point, *boxes, preview=preview)
        elif op == 'after':
            value = row_vision.teach_after(binding, args[0])
        else:
            raise ValueError('unknown_row_sample')
        app.guard(binding, binding, title)
        sender.send((True, value))
    except Exception as exc:
        sender.send((False, getattr(exc, 'code', 'row_sample_failed')))
    finally:
        sender.close()


class SampleJob:
    """Only a completed read may replace the user's previously reviewed sample."""
    def __init__(self, binding, op, args, *, reader=_read_sample):
        ctx = mp.get_context('spawn')
        target = dict(pid=binding.pid, hwnd=binding.hwnd, exe_name=binding.exe,
                      process_started_utc_ticks=binding.created)
        self.receiver, sender = ctx.Pipe(duplex=False)
        self.process = ctx.Process(target=reader, args=(sender, target, binding.title, op, args), daemon=True)
        self.started = time.monotonic()
        try:
            self.process.start()
        except BaseException:
            self.receiver.close()
            raise
        finally:
            sender.close()

    def poll(self):
        if self.receiver.poll():
            try:
                ok, result = self.receiver.recv()
            except EOFError as exc:
                raise RuntimeError('row_sample_interrupted') from exc
            if not ok:
                raise ValueError(result)
            return result
        if not self.process.is_alive():
            raise RuntimeError('row_sample_interrupted')
        if time.monotonic() - self.started > 12:
            raise RuntimeError('row_sample_timeout')
        return None

    def close(self):
        self.receiver.close()
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(1)
        if not self.process.is_alive():
            self.process.close()


def selection_box(start, end, size):
    """Turn a drag in either direction into an uncropped row-relative box."""
    left, right = sorted((start[0], end[0]))
    top, bottom = sorted((start[1], end[1]))
    if left < 0 or top < 0 or right > size[0] or bottom > size[1]:
        raise ValueError('행 안에서만 드래그하세요.')
    if right - left < 4 or bottom - top < 4:
        raise ValueError('표시 전체가 들어가도록 조금 더 크게 드래그하세요.')
    return [left, top, right - left, bottom - top]


def raw_row_step(label, spec):
    label = label.strip()
    if not label or len(label) > 200 or any(ord(c) < 32 for c in label):
        raise ValueError('실행할 동작을 1~200자의 한 줄로 입력하세요.')
    return {'id': uuid.uuid4().hex, 'action': 'row-action', 'label': label, 'row': spec}


def _dialog_shell(win, title, *, width=780, height=720):
    """Keep actions visible while long teaching forms scroll on small displays."""
    import tkinter as tk
    from tkinter import ttk
    from recorder_theme import configure

    theme = configure(win)
    scale = win.winfo_fpixels('1i') / 96.0
    px = lambda value: round(value * scale)
    available_width = max(1, win.winfo_screenwidth() - px(48))
    available_height = max(1, win.winfo_screenheight() - px(70))
    dialog_width = min(px(width), available_width)
    dialog_height = min(px(height), available_height)
    win.geometry(f'{dialog_width}x{dialog_height}')
    win.minsize(min(px(520), dialog_width), min(px(360), dialog_height))
    win.maxsize(available_width, available_height)

    shell = ttk.Frame(win, padding=px(20))
    shell.pack(fill='both', expand=True)
    shell.columnconfigure(0, weight=1)
    shell.rowconfigure(1, weight=1)
    header = ttk.Frame(shell)
    header.grid(row=0, column=0, sticky='ew', pady=(0, px(14)))
    ttk.Label(header, text='Desktop Select', style='Brand.TLabel').pack(anchor='w')
    heading = ttk.Label(header, text=title, style='Title.TLabel', justify='left')
    heading.pack(anchor='w', pady=(px(4), 0))
    viewport = ttk.Frame(shell)
    viewport.grid(row=1, column=0, sticky='nsew')
    viewport.columnconfigure(0, weight=1)
    viewport.rowconfigure(0, weight=1)
    scroll = tk.Canvas(viewport, background=theme['bg'], highlightthickness=0,
                       borderwidth=0, height=px(120))
    scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=scroll.yview)
    scroll.configure(yscrollcommand=scrollbar.set)
    scroll.grid(row=0, column=0, sticky='nsew')
    scrollbar.grid(row=0, column=1, sticky='ns', padx=(px(8), 0))
    body = ttk.Frame(scroll)
    content = scroll.create_window(0, 0, anchor='nw', window=body)
    footer = ttk.Frame(shell)
    footer.grid(row=2, column=0, sticky='ew', pady=(px(14), 0))
    wrapping = [(heading, px(40))]

    def wrap(label, inset=0):
        wrapping.append((label, px(inset)))
        label.configure(wraplength=max(px(100), dialog_width - px(70) - px(inset)))
        return label

    def fit(event):
        scroll.itemconfigure(content, width=event.width)
        for label, inset in wrapping:
            label.configure(wraplength=max(px(100), event.width - inset))

    scroll.bind('<Configure>', fit)
    body.bind('<Configure>', lambda event: scroll.configure(scrollregion=scroll.bbox('all')))

    def wheel(event):
        # Native editors retain their own wheel behavior.
        if event.widget.winfo_class() not in ('Text', 'TCombobox'):
            scroll.yview_scroll(-int(event.delta / 120), 'units')

    win.bind('<MouseWheel>', wheel, add='+')
    return theme, px, body, footer, header, wrap


def teach_row_dialog(parent, binding, previous=None):
    """Return a reviewed step and its reusable example, or None."""
    import tkinter as tk
    from tkinter import ttk
    import row_vision

    win = tk.Toplevel(parent)
    win.title('같은 행의 버튼과 완료 상태')
    theme, px, body, bar, header, wrap = _dialog_shell(win, '같은 행의 버튼과 완료 상태')
    message = tk.StringVar(win, '업무 창에 목록을 띄운 뒤 ‘행 가져오기’를 누르세요. 3초 동안 원하는 행 위에 마우스를 올려 두세요.')
    wrap(ttk.Label(header, textvariable=message, style='Muted.TLabel', justify='left')).pack(
        anchor='w', pady=(px(8), 0))
    frm = ttk.Frame(body, style='Card.TFrame', padding=px(16))
    frm.pack(fill='x')
    ttk.Label(frm, text='1. 행의 세 영역 선택', style='Card.Section.TLabel').pack(anchor='w', pady=(0, px(10)))
    group = ttk.Frame(frm, style='Card.TFrame')
    group.pack(fill='both', expand=True)
    canvas = tk.Canvas(group, width=px(660), height=px(85), bg=theme['surface'],
                       highlightbackground='#DCE1EC', highlightthickness=1)
    horizontal = ttk.Scrollbar(group, orient='horizontal', command=canvas.xview)
    vertical = ttk.Scrollbar(group, orient='vertical', command=canvas.yview)
    canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
    canvas.grid(row=0, column=0, sticky='nsew')
    horizontal.grid(row=1, column=0, sticky='ew')
    vertical.grid(row=0, column=1, sticky='ns')
    group.columnconfigure(0, weight=1)
    group.rowconfigure(0, weight=1)

    choice = tk.IntVar(win, 0)
    choices = ttk.Frame(frm, style='Card.TFrame')
    choices.pack(fill='x', pady=(px(10), 0))
    style = ttk.Style(win)
    style.configure('Card.TRadiobutton', background=theme['surface'], foreground=theme['ink'],
                    font='TkDefaultFont', padding=(0, px(3)))
    style.map('Card.TRadiobutton', background=[('active', theme['surface'])])
    for i, title in enumerate(PARTS):
        ttk.Radiobutton(choices, text=title, variable=choice, value=i, style='Card.TRadiobutton').pack(
            anchor='w', pady=px(1))

    capture = ttk.Frame(body, style='Card.TFrame', padding=px(16))
    capture.pack(fill='x', pady=(px(12), 0))
    ttk.Label(capture, text='2. 실행 전후 상태 확인', style='Card.Section.TLabel').pack(anchor='w', pady=(0, px(10)))
    capture_bar = ttk.Frame(capture, style='Card.TFrame')
    capture_bar.pack(fill='x')
    capture_bar.columnconfigure(0, weight=1)
    capture_bar.columnconfigure(1, weight=1)
    evidence = ttk.Frame(capture, style='Card.TFrame')
    evidence.pack(fill='x', pady=(px(10), 0))

    label = tk.StringVar(win)
    details = ttk.Frame(body, style='Card.TFrame', padding=px(16))
    details.pack(fill='x', pady=(px(12), 0))
    ttk.Label(details, text='3. 이 행에서 실행할 동작', style='Card.Section.TLabel').pack(anchor='w', pady=(0, px(8)))
    ttk.Entry(details, textvariable=label, width=40).pack(fill='x')
    wrap(ttk.Label(details, text='예: 장비 1의 시작 버튼을 누르고 실행 중 표시 확인',
                   style='Card.Muted.TLabel'), 32).pack(anchor='w', pady=(px(6), 0))

    preview = point = draft = spec = result = None
    boxes = [None, None, None]
    image = None
    thumbnails = []
    drag_start = None
    pending = None
    job = None
    ready = 'select'

    def close():
        nonlocal pending, job
        if pending:
            win.after_cancel(pending)
            pending = None
        if job is not None:
            job.close()
            job = None
        win.destroy()

    def sample_off_window(op, payload, apply, delay):
        """Allow time to point, then poll the cancellable reader from the dialog."""
        nonlocal pending
        if pending or job is not None:
            return
        for button in (point_btn, before_btn, after_btn, reuse_btn, use_btn):
            button.configure(state='disabled')
        win.withdraw()
        def read():
            nonlocal pending, job
            pending = None
            try:
                if job is None:
                    args = payload()
                    job = SampleJob(binding, op, args)
                value = job.poll()
                if value is None:
                    if time.monotonic() - job.started > 2 and win.state() == 'withdrawn':
                        message.set('선택한 창에서 행을 읽는 중입니다. 취소하면 이번 행만 건너뜁니다.')
                        win.deiconify()
                    pending = win.after(80, read)
                    return
                apply(value)
            except (RuntimeError, OSError, ValueError, tk.TclError) as exc:
                code = getattr(exc, 'code', str(exc))
                message.set('확인하지 못했습니다. 대상 행이 현재 보이는지, 같은 식별 표시가 있는지 확인하고 다시 시도하세요. (' + str(code) + ')')
            if job is not None:
                job.close()
                job = None
            point_btn.configure(state='normal')
            decorate_status()
            win.deiconify()
            win.lift()
        pending = win.after(delay, read)

    def paint_boxes():
        canvas.delete('box')
        for i, box in enumerate(boxes):
            if box:
                x, y, w, h = box
                canvas.create_rectangle(x, y, x + w, y + h, outline=COLORS[i], width=2, tags='box')

    def decorate_status():
        before_btn.configure(state='normal' if ready == 'select' and all(boxes) else 'disabled')
        after_btn.configure(state='normal' if ready == 'before' else 'disabled')
        reuse_btn.configure(state='normal' if ready == 'before' and previous is not None else 'disabled')
        use_btn.configure(state='normal' if ready == 'complete' else 'disabled')
        for radio in choices.winfo_children():
            radio.configure(state='normal' if ready == 'select' and preview else 'disabled')

    def capture_row(candidate):
        nonlocal point, preview, draft, spec, boxes, image, ready
        rect, ppm = candidate
        new_image = tk.PhotoImage(master=win, data=ppm, format='PPM')
        if new_image.width() != rect[2] or new_image.height() != rect[3]:
            raise ValueError('행 크기를 다시 확인하세요.')
        preview, image = candidate, new_image
        point = (rect[0] + rect[2]//2, rect[1] + rect[3]//2)
        draft = spec = None
        ready = 'select'
        same_columns = previous and previous['size'] == list(rect[2:])
        boxes = [previous['boxes'][k].copy() for k in ('key', 'button', 'state')] if same_columns else [None, None, None]
        for child in evidence.winfo_children():
            child.destroy()
        thumbnails.clear()
        canvas.delete('all')
        canvas.create_image(0, 0, anchor='nw', image=image)
        canvas.configure(scrollregion=(0, 0, *rect[2:]),
                         width=min(rect[2], max(px(240), win.winfo_width() - px(110))),
                         height=min(rect[3], px(180), max(px(70), win.winfo_screenheight()//3)))
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)
        choice.set(0)
        paint_boxes()
        decorate_status()
        message.set('이전 행의 세 영역을 표시했습니다. 이 행에도 맞는지 확인하고 필요하면 다시 드래그하세요.' if same_columns else
                    '이 행만의 번호나 이름, 눌러야 할 버튼, 실행 뒤 바뀔 상태 표시를 차례로 드래그하세요. 버튼은 누를 지점이 사각형 중앙에 오게 표시하세요.')

    def mark(event):
        nonlocal drag_start
        if ready == 'select' and preview and job is None:
            drag_start = int(canvas.canvasx(event.x)), int(canvas.canvasy(event.y))

    def trace_drag(event):
        if drag_start is not None:
            canvas.delete('drag')
            canvas.create_rectangle(*drag_start, canvas.canvasx(event.x), canvas.canvasy(event.y),
                                    tags='drag', outline=COLORS[choice.get()], dash=(3, 2))

    def marked(event):
        nonlocal drag_start
        if drag_start is None:
            return
        start, drag_start = drag_start, None
        canvas.delete('drag')
        try:
            box = selection_box(start, (int(canvas.canvasx(event.x)), int(canvas.canvasy(event.y))), preview[0][2:])
        except ValueError as exc:
            message.set(str(exc))
            return
        boxes[choice.get()] = box
        if None in boxes:
            choice.set(boxes.index(None))
        paint_boxes()
        decorate_status()

    def capture_before(received):
        nonlocal draft, ready
        draft = received
        ready = 'before'
        decorate_status()
        message.set('실행 전 상태를 기록했습니다. 업무 창에서 지정한 버튼을 직접 한 번 누르세요. 원하는 완료 표시가 같은 행에 나타나면 돌아와 ‘완료 상태 가져오기’를 누르세요.')

    def show_outcome(observed_spec):
        nonlocal spec, ready
        # The two snippets show exactly the evidence retained for replay, even
        # when the application moved the row after the user's demonstration.
        for child in evidence.winfo_children():
            child.destroy()
        thumbnails.clear()
        for column, (title, key) in enumerate((('실행 전', 'before'), ('기대하는 완료 상태', 'after'))):
            patch = row_vision.template_preview(observed_spec, key)
            thumb = tk.PhotoImage(master=win, data=patch, format='PPM')
            limit = max(px(80), (win.winfo_width() - px(120)) // 2)
            factor = max(1, (thumb.width() + limit - 1) // limit)
            if factor > 1:
                thumb = thumb.subsample(factor, factor)
            thumbnails.append(thumb)
            evidence.columnconfigure(column, weight=1)
            ttk.Label(evidence, text=title, image=thumb, compound='top', style='Card.TLabel').grid(
                row=0, column=column, sticky='n', padx=px(4))
        spec, ready = observed_spec, 'complete'
        decorate_status()
        message.set('저장할 두 상태의 흑백 표시입니다. 의도한 변화가 맞으면 동작을 적고 ‘적용’을 누르세요.')

    def reuse_after():
        try:
            show_outcome(row_vision.transfer_after(draft, previous))
        except (RuntimeError, ValueError) as exc:
            message.set('이전 행과 열 배치, 버튼 또는 시작 상태가 다릅니다. 이 행의 완료 상태를 직접 가져오세요. (' + str(exc) + ')')

    def accept():
        nonlocal result
        if spec is None:
            return
        try:
            step = raw_row_step(label.get(), spec)
        except ValueError as exc:
            message.set(str(exc))
            return
        result = step, spec
        close()

    canvas.bind('<ButtonPress-1>', mark)
    canvas.bind('<B1-Motion>', trace_drag)
    canvas.bind('<ButtonRelease-1>', marked)
    point_btn = ttk.Button(frm, text='행 가져오기 (3초)', command=lambda: sample_off_window(
        'preview', lambda: (win.winfo_pointerxy(),), capture_row, 3000))
    point_btn.pack(fill='x', pady=(px(10), 0))
    before_btn = ttk.Button(capture_bar, text='실행 전 상태 기록', command=lambda: sample_off_window(
        'before', lambda: (point, boxes, preview), capture_before, 650))
    after_btn = ttk.Button(capture_bar, text='완료 상태 가져오기 (3초)',
                           command=lambda: sample_off_window('after', lambda: (draft,), show_outcome, 3000))
    reuse_btn = ttk.Button(capture_bar, text='이전 행의 완료 표시 사용', command=reuse_after)
    before_btn.grid(row=0, column=0, sticky='ew', padx=(0, px(4)))
    after_btn.grid(row=0, column=1, sticky='ew', padx=(px(4), 0))
    reuse_btn.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(px(8), 0))
    use_btn = ttk.Button(bar, text='적용', command=accept, style='Primary.TButton')
    use_btn.pack(side='right')
    ttk.Button(bar, text='취소', command=close, style='Quiet.TButton').pack(side='right', padx=(0, px(8)))
    decorate_status()
    parent.withdraw()
    win.protocol('WM_DELETE_WINDOW', close)
    try:
        parent.wait_window(win)
        return result
    finally:
        if parent.winfo_exists():
            parent.deiconify()
            parent.lift()
