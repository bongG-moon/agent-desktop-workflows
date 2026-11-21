"""Small, native Tk theme shared by the recording and review windows."""
from __future__ import annotations


def configure(root):
    """Apply the AX Workspace palette and return colors and the measuring font.

    Point-sized named fonts use Tk's current display scaling. The same body font
    is returned for column measurements, so Korean text is never measured with
    a different font from the one rendered in the table.
    """
    from tkinter import font as tkfont, ttk

    owner = root._root()
    cached = getattr(owner, '_desktop_select_theme', None)
    if cached is not None:
        root.configure(background=cached['bg'])
        return cached

    bg, surface = '#F6F7FB', '#FFFFFF'
    ink, muted, accent = '#252B39', '#626D80', '#626AD7'
    line, tint = '#DCE1EC', '#ECEBFA'
    active, pressed, disabled = '#EEF0F8', '#E6E8F3', '#9AA1AF'

    available = {name.casefold(): name for name in tkfont.families(root)}
    candidates = ('Noto Sans KR', 'Noto Sans CJK KR', 'Malgun Gothic',
                  '맑은 고딕', 'Segoe UI Variable', 'Segoe UI')
    family = next((available[name.casefold()] for name in candidates
                   if name.casefold() in available),
                  tkfont.nametofont('TkDefaultFont', root=root).actual('family'))
    text_size = 10
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
        tkfont.nametofont(name, root=root).configure(
            family=family, size=text_size,
            weight='bold' if name == 'TkHeadingFont' else 'normal')
    font = tkfont.nametofont('TkDefaultFont', root=root)
    scale = root.winfo_fpixels('1i') / 96.0

    def px(value):
        return round(value * scale)

    style = ttk.Style(root)
    if 'clam' in style.theme_names():
        style.theme_use('clam')
    style.configure('.', font='TkDefaultFont', background=bg, foreground=ink)
    style.configure('TFrame', background=bg)
    style.configure('TLabel', background=bg, foreground=ink, font='TkDefaultFont')
    style.configure('Card.TFrame', background=surface, borderwidth=0)
    style.configure('Card.TLabel', background=surface)
    style.configure('Title.TLabel', font=(family, text_size + 5, 'bold'))
    style.configure('Brand.TLabel', foreground=accent,
                    font=(family, text_size, 'bold'))
    style.configure('Section.TLabel', font='TkHeadingFont')
    style.configure('Card.Section.TLabel', background=surface, font='TkHeadingFont')
    style.configure('Muted.TLabel', foreground=muted)
    style.configure('Card.Muted.TLabel', background=surface, foreground=muted)
    style.configure('Small.TLabel', foreground=muted, font=(family, text_size - 1))
    style.configure('Badge.TLabel', background=tint, foreground='#535BBB',
                    padding=(px(9), px(4)))

    for name in ('TButton', 'TMenubutton'):
        style.configure(name, font='TkDefaultFont', background=surface,
                        foreground=ink, padding=(px(12), px(7)),
                        borderwidth=1, relief='flat', bordercolor=line,
                        lightcolor=line, darkcolor=line, focuscolor=accent,
                        focusthickness=1, arrowcolor=muted, arrowsize=8)
        style.map(name,
                  background=[('disabled', '#F0F1F6'), ('pressed', pressed),
                              ('active', active)],
                  foreground=[('disabled', disabled)],
                  bordercolor=[('focus', accent), ('active', '#C8CCE2')],
                  arrowcolor=[('disabled', disabled)])
    style.configure('Quiet.TButton', background=bg, foreground=muted)
    style.configure('Danger.TButton', background=surface, foreground='#BD3B46', borderwidth=0)
    style.map('Danger.TButton', foreground=[('disabled', disabled), ('!disabled', '#BD3B46')],
              background=[('disabled', surface), ('active', '#FFF0F1'), ('!disabled', surface)])
    style.configure('Quiet.TMenubutton', background=surface, foreground=muted, borderwidth=0)
    style.configure('Add.TMenubutton', background=tint, foreground='#535BBB', bordercolor=tint)
    style.configure('Primary.TButton', background=accent, foreground=surface,
                    padding=(px(17), px(8)), bordercolor=accent,
                    lightcolor=accent, darkcolor=accent, focuscolor=surface)
    style.map('Primary.TButton',
              background=[('disabled', '#E1E3F0'), ('pressed', '#4D55BA'),
                          ('active', '#555DC8')],
              foreground=[('disabled', '#8990A7'), ('!disabled', surface)],
              bordercolor=[('disabled', '#E1E3F0'), ('pressed', '#4D55BA'),
                           ('active', '#555DC8'), ('!disabled', accent)])
    style.layout('Summary.TButton', [('Button.focus', {
        'sticky': 'nswe', 'children': [('Button.padding', {
            'sticky': 'nswe', 'children': [('Button.label', {'sticky': 'nswe'})]
        })]
    })])
    style.configure('Summary.TButton', anchor='w', justify='left',
                    padding=(px(2), px(6)), background=surface,
                    foreground='#535BBB', wraplength=px(520), borderwidth=0)
    style.map('Summary.TButton',
              background=[('disabled', surface), ('active', '#F4F3FC')],
              foreground=[('disabled', muted), ('active', '#464EA9')])

    for name in ('TEntry', 'TCombobox'):
        style.configure(name, font='TkTextFont', foreground=ink,
                        fieldbackground=surface, background=surface,
                        selectbackground=tint, selectforeground=ink,
                        insertcolor=ink, bordercolor=line, lightcolor=line,
                        darkcolor=line, borderwidth=1, padding=(px(9), px(7)),
                        arrowcolor=muted, arrowsize=8)
        style.map(name,
                  fieldbackground=[('disabled', '#F0F1F6'), ('readonly', surface)],
                  foreground=[('disabled', disabled)],
                  bordercolor=[('focus', accent), ('active', '#C8CCE2')],
                  background=[('active', active), ('readonly', surface)],
                  arrowcolor=[('disabled', disabled), ('active', accent)])
    style.configure('TCheckbutton', background=bg, foreground=muted,
                    font='TkDefaultFont', padding=(0, px(3)))
    style.map('TCheckbutton', background=[('active', bg)],
              foreground=[('disabled', disabled), ('active', ink)],
              indicatorbackground=[('selected', accent), ('!selected', surface)])
    style.configure('TRadiobutton', background=bg, foreground=ink, font='TkDefaultFont',
                    padding=(0, px(3)))
    style.map('TRadiobutton', background=[('active', bg)], foreground=[('disabled', disabled)])
    style.configure('TLabelframe', background=bg, bordercolor=line)
    style.configure('TLabelframe.Label', background=bg, foreground=muted, font='TkHeadingFont')
    style.configure('TSeparator', background=line)
    style.configure('Vertical.TScrollbar', background='#D8DCE9',
                    troughcolor=bg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                    arrowcolor=muted, width=px(11), arrowsize=px(11))
    style.map('Vertical.TScrollbar', background=[('active', '#BDC3DC'),
                                               ('pressed', '#AAB2D1')])

    style.configure('Steps.Treeview', font='TkDefaultFont', background=surface,
                    fieldbackground=surface, foreground=ink,
                    rowheight=font.metrics('linespace') + px(14),
                    borderwidth=0, relief='flat')
    style.map('Steps.Treeview', background=[('selected', tint)],
              foreground=[('selected', '#464EA9')])
    style.configure('Steps.Treeview.Heading', font='TkHeadingFont', anchor='w',
                    background='#F0F2F8', foreground=muted,
                    padding=(px(10), px(9)), borderwidth=0, relief='flat')
    style.map('Steps.Treeview.Heading', background=[('active', active)],
              foreground=[('active', ink)])

    # The option database is local to this Tk app; OS menu settings stay intact.
    for option, value in {
        '*Menu.font': 'TkMenuFont', '*Menu.background': surface,
        '*Menu.foreground': ink, '*Menu.activeBackground': tint,
        '*Menu.activeForeground': '#464EA9', '*Menu.disabledForeground': disabled,
        '*Menu.selectColor': accent, '*Menu.borderWidth': 1,
        '*Menu.activeBorderWidth': 0, '*Menu.relief': 'flat',
        '*TCombobox*Listbox.font': 'TkTextFont',
        '*TCombobox*Listbox.background': surface,
        '*TCombobox*Listbox.foreground': ink,
        '*TCombobox*Listbox.selectBackground': tint,
        '*TCombobox*Listbox.selectForeground': '#464EA9',
    }.items():
        root.option_add(option, value)
    root.configure(background=bg)
    theme = dict(bg=bg, surface=surface, ink=ink, muted=muted, accent=accent,
                 font=font, text_size=text_size)
    owner._desktop_select_theme = theme
    return theme


def fit_window(window, *, minimum_width=440, minimum_height=240):
    """Keep a dialog's initial size and resize limits within its display."""
    window.update_idletasks()
    scale = window.winfo_fpixels('1i') / 96.0
    maximum_width = max(240, window.winfo_screenwidth() - round(40 * scale))
    maximum_height = max(200, window.winfo_screenheight() - round(64 * scale))
    window.maxsize(maximum_width, maximum_height)
    window.minsize(min(round(minimum_width * scale), maximum_width),
                   min(round(minimum_height * scale), maximum_height))
    width = min(max(window.winfo_reqwidth(), round(minimum_width * scale)), maximum_width)
    height = min(max(window.winfo_reqheight(), round(minimum_height * scale)), maximum_height)
    window.geometry(f'{width}x{height}')


def confirm(parent, title, message, *, confirm_text='적용'):
    """Ask for one editor decision with the same local dialog style."""
    import tkinter as tk
    from tkinter import ttk

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent.winfo_toplevel())
    configure(dialog)
    scale = dialog.winfo_fpixels('1i') / 96.0
    answer = False
    panel = ttk.Frame(dialog, padding=round(24 * scale))
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='Desktop Select', style='Brand.TLabel').pack(anchor='w')
    ttk.Label(panel, text=title, style='Title.TLabel').pack(anchor='w', pady=(round(4 * scale), round(14 * scale)))
    ttk.Label(panel, text=message, wraplength=round(420 * scale),
              style='Muted.TLabel', justify='left').pack(fill='x')
    footer = ttk.Frame(panel)
    footer.pack(fill='x', pady=(round(20 * scale), 0))

    def finish(accepted=False):
        nonlocal answer
        answer = accepted
        dialog.destroy()

    cancel = ttk.Button(footer, text='취소', style='Quiet.TButton', command=finish)
    cancel.pack(side='left')
    ttk.Button(footer, text=confirm_text, style='Primary.TButton',
               command=lambda: finish(True)).pack(side='right')
    dialog.bind('<Escape>', lambda _event: finish())
    dialog.protocol('WM_DELETE_WINDOW', finish)
    previous_grab = parent.grab_current()
    fit_window(dialog)
    dialog.grab_set()
    cancel.focus_set()
    try:
        parent.wait_window(dialog)
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
    return answer
