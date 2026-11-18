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
    root = tk.Tk()
    root.title('Desktop Select 녹화 대상 선택')

    def pixels(value, fallback):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return fallback

    # Tk scales its default font with Windows DPI, but its default Treeview row
    # height stays at 20 pixels. Derive both row height and initial sizing from
    # the actual font so enlarged text never overlaps the next row.
    line_height = pixels(root.tk.call('font', 'metrics', 'TkDefaultFont', '-linespace'), 20)
    row_height = line_height + max(8, line_height // 2)
    label_width = pixels(root.tk.call('font', 'measure', 'TkDefaultFont', '가' * 44), 720)
    screen_width = pixels(root.winfo_screenwidth(), 1280)
    screen_height = pixels(root.winfo_screenheight(), 800)
    width = min(max(760, label_width + 100), max(320, screen_width - 80))
    height = min(max(460, row_height * 7 + line_height * 7 + 90), max(240, screen_height - 120))
    root.geometry(f'{width}x{height}')
    root.minsize(min(width, 640), min(height, max(340, line_height * 12)))
    root.tk.call('ttk::style', 'configure', 'RecorderPicker.Treeview',
                 '-font', 'TkDefaultFont', '-rowheight', row_height)
    root.tk.call('ttk::style', 'configure', 'RecorderPicker.Treeview.Heading',
                 '-font', 'TkDefaultFont', '-padding', (8, max(4, line_height // 6)))
    selected = None
    failed = False
    current = {}
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='녹화할 창을 선택하세요.').pack(anchor='w')
    search = ttk.Frame(frame)
    search.pack(fill='x', pady=(10, 8))
    query = tk.StringVar(value=hint)
    ttk.Label(search, text='프로그램 또는 창 제목').pack(side='left', padx=(0, 8))
    entry = ttk.Entry(search, textvariable=query)
    entry.pack(side='left', fill='x', expand=True)
    message = tk.StringVar()
    notice = ttk.Label(frame, textvariable=message, wraplength=width - 40, justify='left')
    notice.pack(anchor='w', pady=(0, 8))
    table = ttk.Frame(frame)
    listing = ttk.Treeview(table, columns=('title', 'exe', 'pid', 'hwnd'), show='headings',
                           displaycolumns=('title', 'exe'), selectmode='browse', height=7,
                           style='RecorderPicker.Treeview')
    for key, title, column_width in [('title', '창 제목', int((width - 60) * .58)),
                                     ('exe', '프로그램', int((width - 60) * .42))]:
        listing.heading(key, text=title)
        minimum = pixels(root.tk.call('font', 'measure', 'TkDefaultFont', title), 80) + 32
        listing.column(key, width=column_width, minwidth=minimum, stretch=True)
    scroll = ttk.Scrollbar(table, orient='vertical', command=listing.yview)
    horizontal = ttk.Scrollbar(table, orient='horizontal', command=listing.xview)
    listing.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
    horizontal.pack(side='bottom', fill='x')
    scroll.pack(side='right', fill='y')
    listing.pack(side='left', fill='both', expand=True)
    detail = tk.StringVar(value='목록에서 창을 선택하면 전체 제목과 프로그램을 확인할 수 있습니다.')
    detail_label = ttk.Label(frame, textvariable=detail, wraplength=width - 40, justify='left')
    actions = ttk.Frame(frame)
    # Reserve the footer before giving remaining space to the scrollable list.
    # Larger fonts or a multi-line caption must not push the buttons off screen.
    actions.pack(side='bottom', fill='x', pady=(12, 0))
    detail_label.pack(side='bottom', fill='x', pady=(10, 0))
    table.pack(fill='both', expand=True)

    def cancel():
        root.destroy()

    def choose(*_):
        nonlocal selected
        keys = listing.selection()
        if len(keys) == 1 and keys[0] in current:
            selected = current[keys[0]]
            root.destroy()

    confirm = ttk.Button(actions, text='이 창에서 녹화', command=choose, state='disabled')
    confirm.pack(side='right')
    ttk.Button(actions, text='취소', command=cancel).pack(side='right', padx=(0, 8))

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
        detail.set('목록에서 창을 선택하면 전체 제목과 프로그램을 확인할 수 있습니다.')
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
            detail.set('목록에서 창을 선택하면 전체 제목과 프로그램을 확인할 수 있습니다.')
            return
        text = f"창 제목: {chosen['title']}\n프로그램: {chosen['exe_name']}"
        if sum(row['title'] == chosen['title'] and row['exe_name'].casefold() == chosen['exe_name'].casefold()
               for row in rows) > 1:
            text += f"\n창 번호: {chosen['hwnd']}  /  프로세스: {chosen['pid']}"
        detail.set(text)

    def resize(event):
        if event.widget is root:
            wraplength = max(200, event.width - 40)
            notice.configure(wraplength=wraplength)
            detail_label.configure(wraplength=wraplength)

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

    ttk.Button(actions, text='새로 고침', command=refresh).pack(side='left')
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
