"""Resolve a recorder hint from current window metadata, without starting UIA."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
import re
import unicodedata

import win_bridge as bridge


_SHELL_CLASSES = {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                  'tooltips_class32', '#32768'}
_MAX_WINDOWS = 512


def inventory():
    """Read visible top-level captions, retaining each HWND and its exact identity."""
    bridge._windows()
    records, identities = [], {}
    truncated = False
    caption_length = bridge._sig(bridge.user32, 'GetWindowTextLengthW', C.c_int, W.HWND)

    def visit(hwnd, _):
        nonlocal truncated
        try:
            if not bridge.user32.IsWindowVisible(hwnd) or bridge.root_window(hwnd) != hwnd:
                return True
            pid = bridge.hwnd_pid(hwnd)
            if not pid or pid == os.getpid() or bridge.window_class(hwnd) in _SHELL_CLASSES:
                return True
            # Many native helper windows have no usable caption. Discard them
            # before opening the process to read its executable and start time.
            if caption_length(hwnd) == 0:
                return True
            if pid not in identities:
                try:
                    identities[pid] = bridge._process_identity(pid)
                except (bridge.BridgeError, OSError):
                    identities[pid] = None
            identity = identities[pid]
            if identity is None:
                return True
            title = bridge.window_text(hwnd)
            if (not title.strip() or len(title) >= 1023 or '\0' in title
                    or bridge.hwnd_pid(hwnd) != pid or not bridge.user32.IsWindowVisible(hwnd)):
                return True
            exe, started = identity
            records.append(dict(exe_name=exe, pid=pid, hwnd=int(hwnd), title=title,
                                process_started_utc_ticks=started))
            if len(records) >= _MAX_WINDOWS:
                truncated = True
                return False
        except (bridge.BridgeError, OSError):
            pass  # Closed or inaccessible windows are not candidates.
        return True

    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    enum = bridge._sig(bridge.user32, 'EnumWindows', W.BOOL, callback_type, W.LPARAM)
    if not enum(callback_type(visit), 0) or truncated:
        raise bridge.BridgeError('window_list_incomplete')
    return sorted(records, key=lambda row: (row['exe_name'].casefold(),
                                           row['title'].casefold(), row['hwnd']))


def _words(value):
    return re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold())


def _normalized(value):
    return ''.join(_words(value))


def _stem(exe):
    return exe[:-4] if exe.casefold().endswith('.exe') else exe


def _one_edit(left, right):
    """One insertion, deletion, replacement or adjacent transposition, without scoring."""
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        different = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        if len(different) <= 1:
            return True
        return (len(different) == 2 and different[1] == different[0] + 1
                and left[different[0]] == right[different[1]]
                and left[different[1]] == right[different[0]])
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    index = next((i for i, (a, b) in enumerate(zip(shorter, longer)) if a != b), len(shorter))
    return shorter[index:] == longer[index + 1:]


def match_windows(hint, rows):
    """Return every match in the strongest tier; never break a tie by score or focus."""
    if not isinstance(hint, str) or not hint.strip():
        return []
    rows = list(rows)
    literal = hint.casefold()
    exact = [row for row in rows if literal in (row['exe_name'].casefold(),
                                               _stem(row['exe_name']).casefold(),
                                               row['title'].casefold())]
    if exact:
        return exact
    query = _normalized(hint)
    if not query:
        return []
    partial = [row for row in rows if any(query in _normalized(value)
               for value in (row['exe_name'], _stem(row['exe_name']), row['title']))]
    if partial:
        return partial
    # Fuzzy matching is restricted to one sufficiently long app/name token. A long
    # sentence or an entire document caption is not a fuzzy identifier.
    tokens = _words(_stem(hint))
    if len(tokens) != 1 or not 6 <= len(tokens[0]) <= 80:
        return []
    query = tokens[0]
    result = []
    for row in rows:
        names = set(_words(_stem(row['exe_name'])) + _words(row['title']))
        if any(6 <= len(name) <= 80 and _one_edit(query, name) for name in names):
            result.append(row)
    return result


def choose_window(hint, rows, on_ready=None):
    """Use a unique match, or let the user choose a current window in a local picker."""
    rows = list(rows)
    matches = match_windows(hint, rows)
    if len(matches) == 1:
        return matches[0]
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as error:
        raise bridge.BridgeError('recorder_tk_unavailable') from error
    try:
        return _choose_window(tk, ttk, hint, rows, on_ready)
    except tk.TclError as error:
        raise bridge.BridgeError('recording_ui_failed') from error


def _choose_window(tk, ttk, hint, rows, on_ready):
    import recorder_theme

    root = tk.Tk()
    root.title('Desktop Select 녹화 대상 선택')
    theme = recorder_theme.configure(root)
    font = theme['font']

    def pixels(value, fallback):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return fallback

    scale = root.winfo_fpixels('1i') / 96.0

    def px(value):
        return max(1, round(value * scale))

    # The shared theme measures the same Korean font used by the table. Keep
    # physical dimensions within the screen even when Windows doubles its DPI.
    line_height = pixels(font.metrics('linespace'), px(20))
    row_height = line_height + px(14)
    label_width = pixels(font.measure('가' * 44), px(620))
    screen_width = pixels(root.winfo_screenwidth(), 1280)
    screen_height = pixels(root.winfo_screenheight(), 800)
    width = min(max(px(780), label_width + px(100)), max(320, screen_width - 80))
    height = min(max(px(580), row_height * 6 + line_height * 8 + px(100)),
                 max(240, screen_height - 120))
    root.geometry(f'{width}x{height}')
    root.minsize(min(width, px(640)), min(height, px(500)))
    # Keep useful rows visible on a smaller display at high DPI. Font and
    # control sizes stay with the shared theme; only surrounding whitespace
    # becomes more compact when the screen cannot hold the preferred height.
    scale = min(scale, height / 580)
    selected = None
    failed = False
    current = {}
    frame = ttk.Frame(root, padding=(px(24), px(20)))
    frame.pack(fill='both', expand=True)
    actions = ttk.Frame(frame)
    # Reserve the footer first: long titles and high DPI must never move the
    # recording/cancel buttons below the window's client area.
    actions.pack(side='bottom', fill='x', pady=(px(16), 0))
    detail_card = ttk.Frame(frame, padding=(px(16), px(8)), style='Card.TFrame')
    detail_card.pack(side='bottom', fill='x', pady=(px(12), 0))
    ttk.Label(frame, text='Desktop Select', style='Brand.TLabel').pack(anchor='w', pady=(0, px(3)))
    ttk.Label(frame, text='녹화할 창 선택', style='Title.TLabel').pack(anchor='w')
    search = ttk.Frame(frame, padding=(px(16), px(12)), style='Card.TFrame')
    search.pack(fill='x', pady=(px(18), px(10)))
    query = tk.StringVar(value=hint)
    ttk.Label(search, text='프로그램 또는 창 제목', style='Card.Muted.TLabel').pack(
        side='left', padx=(0, px(12)))
    entry = ttk.Entry(search, textvariable=query)
    entry.pack(side='left', fill='x', expand=True)
    message = tk.StringVar()
    notice = ttk.Label(frame, textvariable=message, wraplength=width - px(48), justify='left',
                       style='Muted.TLabel')
    notice.pack(anchor='w', pady=(0, px(10)))
    table = ttk.Frame(frame, style='Card.TFrame', padding=1)
    listing = ttk.Treeview(table, columns=('title', 'exe', 'pid', 'hwnd'), show='headings',
                           displaycolumns=('title', 'exe'), selectmode='browse', height=7,
                           style='Steps.Treeview')
    for key, title, column_width in [('title', '창 제목', int((width - px(60)) * .64)),
                                     ('exe', '프로그램', int((width - px(60)) * .36))]:
        listing.heading(key, text=title, anchor='w')
        minimum = pixels(font.measure(title), px(60)) + px(24)
        listing.column(key, width=column_width, minwidth=minimum, stretch=True, anchor='w')
    scroll = ttk.Scrollbar(table, orient='vertical', command=listing.yview)
    horizontal = ttk.Scrollbar(table, orient='horizontal', command=listing.xview)
    listing.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
    horizontal.pack(side='bottom', fill='x')
    scroll.pack(side='right', fill='y')
    listing.pack(side='left', fill='both', expand=True)
    detail = tk.StringVar(value='목록에서 녹화할 창을 선택하세요.')
    caption = tk.StringVar()
    ttk.Label(detail_card, text='선택한 창', style='Card.Section.TLabel').pack(anchor='w')
    # A read-only field retains the entire literal caption and lets the user
    # scroll/copy it without a long title expanding the footer beyond the screen.
    caption_field = ttk.Entry(detail_card, textvariable=caption, state='readonly')
    caption_field.pack(fill='x', pady=(px(8), px(7)))
    detail_label = ttk.Label(detail_card, textvariable=detail, wraplength=width - px(80),
                             justify='left', style='Card.Muted.TLabel')
    detail_label.pack(anchor='w')
    table.pack(fill='both', expand=True)

    def cancel():
        root.destroy()

    def choose(*_):
        nonlocal selected
        keys = listing.selection()
        if len(keys) == 1 and keys[0] in current:
            selected = current[keys[0]]
            root.destroy()

    confirm = ttk.Button(actions, text='이 창에서 녹화', command=choose, state='disabled',
                         style='Primary.TButton')
    confirm.pack(side='right')
    ttk.Button(actions, text='취소', command=cancel, style='Quiet.TButton').pack(
        side='right', padx=(0, px(8)))

    def show(*_):
        nonlocal current
        matches = match_windows(query.get(), rows)
        shown = matches or rows
        current = {str(index): row for index, row in enumerate(shown)}
        for key in listing.get_children():
            listing.delete(key)
        for key, row in current.items():
            listing.insert('', 'end', iid=key, values=(row['title'], row['exe_name'], row['pid'], row['hwnd']))
        confirm.configure(state='disabled')
        caption.set('')
        detail.set('목록에서 녹화할 창을 선택하세요.')
        if not rows:
            message.set('선택할 창이 없습니다. 프로그램을 연 뒤 새로 고침을 누르세요.')
        elif matches:
            message.set(f'일치하는 창 {len(matches)}개입니다. 제목과 프로그램을 확인하고 선택하세요.')
        elif query.get().strip():
            message.set('검색어와 일치하는 창이 없어 모든 현재 창을 표시합니다. 검색어를 바꾸거나 창을 직접 선택하세요.')
        else:
            message.set('모든 현재 창입니다. 제목과 프로그램을 확인하고 선택하세요.')

    def refresh():
        nonlocal rows
        try:
            rows = inventory()
        except (bridge.BridgeError, OSError):
            rows = []
            show()
            message.set('현재 창 목록을 읽지 못했습니다. 잠시 뒤 새로 고침을 누르세요.')
        else:
            show()

    def changed(*_):
        keys = listing.selection()
        chosen = current.get(keys[0]) if len(keys) == 1 else None
        confirm.configure(state='normal' if chosen is not None else 'disabled')
        if chosen is None:
            caption.set('')
            detail.set('목록에서 녹화할 창을 선택하세요.')
            return
        caption.set(chosen['title'])
        caption_field.xview_moveto(0)
        text = f"프로그램: {chosen['exe_name']}"
        if sum(row['title'] == chosen['title'] and row['exe_name'].casefold() == chosen['exe_name'].casefold()
               for row in rows) > 1:
            text += f"    창 번호: {chosen['hwnd']}  /  프로세스: {chosen['pid']}"
        detail.set(text)

    def resize(event):
        if event.widget is root:
            notice.configure(wraplength=max(px(160), event.width - px(48)))
            detail_label.configure(wraplength=max(px(160), event.width - px(80)))

    def report_ready(event=None):
        nonlocal on_ready
        if event is not None and event.widget is not root:
            return
        if on_ready is not None and root.winfo_viewable():
            callback, on_ready = on_ready, None
            callback()

    def callback_failed(*_):
        nonlocal failed
        failed = True
        root.destroy()

    ttk.Button(actions, text='새로 고침', command=refresh, style='Quiet.TButton').pack(side='left')
    entry.bind('<KeyRelease>', show)
    listing.bind('<<TreeviewSelect>>', changed)
    listing.bind('<Double-1>', choose)
    root.bind('<Escape>', lambda _: cancel())
    root.bind('<Configure>', resize)
    root.bind('<Map>', report_ready, add='+')
    root.protocol('WM_DELETE_WINDOW', cancel)
    root.report_callback_exception = callback_failed
    show()
    report_ready()
    root.mainloop()
    if failed:
        raise bridge.BridgeError('recording_ui_failed')
    return selected
