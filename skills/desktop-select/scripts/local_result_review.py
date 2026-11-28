"""Review one result column in a local snapshot before resuming a registered job."""
import tkinter as tk
from tkinter import ttk

import recorder_theme


class ResultReviewError(ValueError):
    def __init__(self, code, **geometry):
        super().__init__(code)
        self.code = code
        self.geometry = geometry


def valid_choice(header, point, value, bounds):
    if not header or not point or not value or not value.strip() or value != value.strip():
        return False
    x, y, w, h = bounds
    left, top, right = header
    px, py = point
    return (x <= left and left + 4 <= right < x + w and y <= top < py < y + h
            and left <= px <= right)


def valid_row_choice(point, bounds, confirmed):
    if (confirmed is not True or not isinstance(point, (list, tuple)) or len(point) != 2
            or any(type(v) is not int for v in point)):
        return False
    x, y, w, h = bounds
    return x <= point[0] < x + w and y <= point[1] < y + h


class ResultReview:
    def __init__(self, root, image_path, column, table_box, expected=None, *, frame_size=None, row_policy=None,
                 step_number=None):
        if row_policy is not None and (row_policy not in ('single', 'first', 'recorded') or table_box is None):
            raise ResultReviewError('result_row_review_scope_required')
        self.root = root
        self.expected = expected
        self.column = column
        self.row_policy = row_policy
        self.result = self.header = self.point = self.start = None
        self.theme = recorder_theme.configure(root)
        scale = root.winfo_fpixels('1i') / 96.0
        px = lambda value: round(value * scale)
        root.title('Desktop Select 결과 표 확인')
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        body = ttk.Frame(root, padding=(px(20), px(14), px(20), px(8)))
        body.grid(row=0, column=0, sticky='ew')
        title = '더블클릭할 위치 선택' if row_policy else '요청값이 보이는 셀 확인'
        if step_number is not None:
            title = f'{step_number}단계  {title}'
        ttk.Label(body, text=title, name='step_title', style='Title.TLabel').pack(
            anchor='w', pady=(0, px(6)))
        self.instruction = tk.StringVar(master=root)
        first_instruction = ttk.Label(body, textvariable=self.instruction, wraplength=px(760))
        first_instruction.pack(anchor='w')
        second_instruction = ttk.Label(
            body, text='선택을 확정하면 실제 업무 창에서 그 위치를 다시 확인하고 더블클릭한 뒤 남은 단계를 실행합니다.',
            style='Muted.TLabel', wraplength=px(760))
        second_instruction.pack(anchor='w', pady=(px(4), 0))
        source = tk.PhotoImage(master=root, file=str(image_path))
        full_w, full_h = source.width(), source.height()
        if frame_size is not None and list(frame_size) != [full_w, full_h]:
            raise ResultReviewError('result_capture_size_mismatch',
                                    capture_size=list(frame_size), image_size=[full_w, full_h])
        x, y, w, h = table_box if table_box is not None else (0, 0, full_w, full_h)
        if not (0 <= x < x + w <= full_w and 0 <= y < y + h <= full_h):
            raise ResultReviewError('result_table_outside_frame',
                                    crop_relative=[x, y, w, h], image_size=[full_w, full_h])
        self.bounds = (x, y, w, h)
        self.photo = source
        if table_box is not None:
            self.photo = tk.PhotoImage(master=root, width=w, height=h)
            root.tk.call(str(self.photo), 'copy', str(source), '-from', x, y, x+w, y+h)
        frame = ttk.Frame(root, padding=(px(20), 0))
        frame.grid(row=1, column=0, sticky='nsew')
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text='업무 화면 캡처', style='Section.TLabel').grid(
            row=0, column=0, sticky='w', pady=(0, px(8)))
        self.canvas = tk.Canvas(frame, background=self.theme['surface'],
                                highlightthickness=1, highlightbackground='#DCE1EC',
                                width=min(w, int(root.winfo_screenwidth()*.78)),
                                height=min(h, int(root.winfo_screenheight()*.45)), cursor='crosshair')
        self.canvas.grid(row=1, column=0, sticky='nsew')
        self.canvas.create_image(0, 0, anchor='nw', image=self.photo)
        self.canvas.configure(scrollregion=(0,0,w,h))
        for axis, orient, row, col, edges in [('x','horizontal',2,0,'ew'),('y','vertical',1,1,'ns')]:
            bar = ttk.Scrollbar(frame, orient=orient, command=getattr(self.canvas, axis+'view'))
            bar.grid(row=row,column=col,sticky=edges)
            self.canvas.configure(**{axis+'scrollcommand':bar.set})
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        footer = ttk.Frame(root, padding=(px(20), px(10), px(20), px(12)))
        footer.grid(row=2, column=0, sticky='ew')
        footer.columnconfigure(0, weight=1)
        value_card = ttk.Frame(footer, style='Card.TFrame', padding=(px(14), px(8)))
        value_card.grid(row=0, column=0, sticky='ew')
        value_card.columnconfigure(1, weight=1)
        ttk.Label(value_card, text='선택한 위치' if row_policy else '선택한 셀에 보이는 글자', style='Card.Section.TLabel').grid(
            row=0, column=0, sticky='w', pady=(0, px(6)))
        self.status = tk.StringVar(master=root)
        ttk.Label(value_card, textvariable=self.status, style='Badge.TLabel').grid(
            row=0, column=1, sticky='e', pady=(0, px(6)))
        self.value = tk.StringVar(master=root)
        self.confirmed = tk.BooleanVar(master=root, value=False)
        self.entry = None
        if row_policy in ('single', 'first'):
            self.row_check = ttk.Checkbutton(value_card, variable=self.confirmed, command=self.update_button,
                text='데이터 행이 한 개만 있고, 그 행을 선택했습니다.' if row_policy == 'single' else
                     '현재 표의 첫 번째 데이터 행을 선택했습니다.')
            self.row_check.grid(row=1, column=0, columnspan=2, sticky='w')
        elif not row_policy:
            self.entry = ttk.Entry(value_card, textvariable=self.value)
            self.entry.grid(row=1, column=0, columnspan=2, sticky='ew')
        hint_label = ttk.Label(value_card, text=('보정에 성공하면 다음 실행에도 이 위치를 사용합니다.'
                               if row_policy == 'recorded' else '행을 구별하기 어렵다면 취소하세요.'
                               if row_policy else '같은 값이 여러 행에 있거나 글자를 읽기 어려우면 취소하세요.'),
                               style='Card.Muted.TLabel', wraplength=px(740))
        hint_label.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(px(6), 0))
        actions = ttk.Frame(footer)
        actions.grid(row=1, column=0, sticky='ew', pady=(px(8), 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text='표 새로 보기', style='Quiet.TButton', width=0,
                   command=self.refresh).grid(row=0, column=0, sticky='w')
        ttk.Button(actions, text='취소', style='Quiet.TButton', width=0,
                   command=root.destroy).grid(row=0, column=1, padx=(px(8), px(8)))
        self.confirm = ttk.Button(actions, name='confirm', text='더블클릭하고 계속',
                                  style='Primary.TButton', command=self.accept)
        self.confirm.grid(row=0, column=2, sticky='e')

        def fit_text(event):
            available = max(1, event.width - px(40))
            first_instruction.configure(wraplength=available)
            second_instruction.configure(wraplength=available)
            hint_label.configure(wraplength=max(1, available - px(28)))

        root.bind('<Configure>', lambda event: fit_text(event) if event.widget is root else None,
                  add='+')
        self.value.trace_add('write', lambda *_: self.update_button())
        self.update_button()
        root.bind('<Return>',lambda _: self.accept())
        root.bind('<Escape>',lambda _: root.destroy())
        root.update_idletasks()
        window_width = min(max(px(760), root.winfo_reqwidth()),
                           int(root.winfo_screenwidth() * .90))
        fixed_height = body.winfo_reqheight() + footer.winfo_reqheight()
        window_height = min(root.winfo_reqheight(), int(root.winfo_screenheight() * .86))
        root.geometry(f'{window_width}x{window_height}')
        root.minsize(min(px(560), window_width),
                     min(fixed_height + px(80), window_height))

    def xy(self, event):
        return int(self.canvas.canvasx(event.x)), int(self.canvas.canvasy(event.y))

    def press(self, event):
        self.start = self.xy(event)

    def drag(self, event):
        if self.start is None or self.row_policy:
            return
        self.canvas.delete('preview')
        x,y = self.start
        self.canvas.create_line(x,y,self.xy(event)[0],y,width=3,fill=self.theme['accent'],tags='preview')

    def release(self, event):
        if self.start is None:
            return
        sx,sy = self.start
        px,py = self.xy(event)
        ox,oy,w,h = self.bounds
        self.start = None
        self.canvas.delete('preview')
        if self.row_policy:
            self.point = None
            self.confirmed.set(False)
            self.canvas.delete('cell')
            point = [ox+px, oy+py]
            if abs(px-sx) < 4 and abs(py-sy) < 4 and valid_row_choice(point, self.bounds, True):
                self.point = point
                if self.row_policy == 'recorded':
                    self.confirmed.set(True)
                self.canvas.create_oval(px-6,py-6,px+6,py+6,outline=self.theme['accent'],width=2,tags='cell')
            self.update_button()
            return
        if abs(px-sx) >= 4:
            self.canvas.delete('selection')
            self.header = self.point = None
            self.value.set('')
            left,right = sorted((sx,px))
            if 0 <= left < right < w and 0 <= sy < h:
                self.header = [ox+left,oy+sy,ox+right]
                self.canvas.create_line(left,sy,right,sy,width=3,fill=self.theme['accent'],tags='selection')
        else:
            self.point = None
            self.value.set('')
            self.canvas.delete('cell')
            point = [ox+px,oy+py]
            if valid_choice(self.header, point, 'x', self.bounds):
                self.point = point
                self.canvas.create_oval(px-6,py-6,px+6,py+6,outline=self.theme['accent'],width=2,tags=('cell','selection'))
                self.update_button()
                self.entry.focus_set()
                return
        self.update_button()

    def update_button(self):
        if self.row_policy:
            enabled = valid_row_choice(self.point, self.bounds, self.confirmed.get())
            if self.row_policy in ('single', 'first'):
                self.row_check.state(['!disabled'] if self.point else ['disabled'])
            if enabled:
                instruction = '원으로 표시한 위치가 맞으면 ‘더블클릭하고 계속’을 누르세요.'
            elif self.point:
                instruction = '원으로 표시한 행이 맞으면 아래 확인란에 체크하세요.'
            else:
                instruction = {
                    'single': '캡처에 결과 행이 하나만 있으면 그 행의 셀을 한 번 클릭하세요. 여러 행이면 취소하세요.',
                    'first': '아래 캡처에서 열 제목 바로 아래, 첫 번째 결과 행의 셀을 한 번 클릭하세요.',
                    'recorded': '아래 캡처에서 더블클릭할 데이터 셀을 한 번 클릭하세요.',
                }[self.row_policy]
            status = '위치 확인 완료' if enabled else '행 확인 필요' if self.point else '위치 선택 전'
        else:
            value = self.value.get()
            exact = self.expected is None or self.expected == value
            enabled = exact and valid_choice(self.header, self.point, value, self.bounds)
            self.entry.state(['!disabled'] if self.point else ['disabled'])
            if not self.header:
                instruction = f'아래 캡처에서 「{self.column}」 열 제목의 왼쪽 끝부터 오른쪽 끝까지 드래그하세요.'
            elif not self.point:
                target = f'「{self.expected}」' if self.expected is not None else '요청한 값'
                instruction = f'표시한 열 아래에서 {target}이 보이는 셀의 글자를 한 번 클릭하세요.'
            elif enabled:
                instruction = '입력한 글자와 선택한 행이 맞으면 ‘더블클릭하고 계속’을 누르세요.'
            elif value and not exact:
                instruction = '입력한 글자가 요청값과 다릅니다. 선택한 셀의 글자를 다시 확인하세요.'
            else:
                instruction = '선택한 셀에 보이는 글자를 아래 입력란에 그대로 입력하세요.'
            status = '값 확인 완료' if enabled else '글자 입력' if self.point else '셀 선택' if self.header else '열 제목 지정'
        self.status.set(status)
        self.instruction.set(instruction)
        self.confirm.state(['!disabled'] if enabled else ['disabled'])

    def accept(self):
        if self.confirm.instate(['!disabled']):
            self.result = (dict(point=self.point, row_policy=self.row_policy, confirmed=True) if self.row_policy else
                           dict(header=self.header, point=self.point, value=self.value.get()))
            self.root.destroy()

    def refresh(self):
        self.result = {'refresh': True}
        self.root.destroy()


def choose(image_path, column=None, *, table_box=None, expected=None, frame_size=None, row_policy=None, step_number=None):
    """Return a local selection, a refresh request, or None on cancel."""
    root = tk.Tk()
    try:
        review = ResultReview(root, image_path, column, table_box, expected, frame_size=frame_size,
                              row_policy=row_policy, step_number=step_number)
        root.mainloop()
        return review.result
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
