"""Small Tk popovers with a native Menu retained as the command data model.

The recorder theme supplies named fonts and ttk colors.  No operating-system
menu is posted, and bindings/grabs live only as long as the open popover.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont, ttk


def _inside(widget, ancestor):
    return widget is not None and (str(widget) == str(ancestor)
                                   or str(widget).startswith(str(ancestor).rstrip('.') + '.'))


def _owner(menu):
    # Tk regards a native Menu itself as a toplevel, even while it is unposted.
    # Its real application owner is needed for transience and window mapping.
    widget = menu
    while isinstance(widget, tk.Menu):
        widget = widget.master
    return widget.winfo_toplevel()


def _screen_bounds(widget, x, y):
    """Use the anchor's monitor work area, including negative monitor origins."""
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                            ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]

            user32 = ctypes.windll.user32
            monitor_from_point = user32.MonitorFromPoint
            monitor_from_point.argtypes = [wintypes.POINT, wintypes.DWORD]
            monitor_from_point.restype = wintypes.HANDLE
            get_monitor_info = user32.GetMonitorInfoW
            get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            get_monitor_info.restype = wintypes.BOOL
            monitor = monitor_from_point(wintypes.POINT(x, y), 2)
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if get_monitor_info(monitor, ctypes.byref(info)):
                rect = info.rcWork
                return rect.left, rect.top, rect.right, rect.bottom
        except (AttributeError, OSError, ValueError):
            pass
    left, top = widget.winfo_vrootx(), widget.winfo_vrooty()
    return (left, top, left + widget.winfo_vrootwidth(),
            top + widget.winfo_vrootheight())


class PopupMenu(tk.Menu):
    """A Menu-compatible model rendered as a themed Toplevel popover.

    ``bind_to(button)`` configures a ttk.Menubutton and replaces its native
    posting bindings. Rows additionally accept ``description`` and
    ``destructive`` in add_command/add_cascade/entryconfigure.
    """

    def __init__(self, master=None, cnf=None, **kwargs):
        self._details = {}
        self._buttons = {}
        self._session = None
        kwargs.setdefault('tearoff', False)
        super().__init__(master, cnf or {}, **kwargs)

    def add(self, itemType, cnf=None, **kwargs):
        options = dict(cnf or {}, **kwargs)
        details = {key: options.pop(key) for key in ('description', 'destructive')
                   if key in options}
        result = super().add(itemType, options)
        self._details[self.index('end')] = details
        self._changed()
        return result

    def delete(self, index1, index2=None):
        first = self.index(index1)
        last = self.index(index2) if index2 is not None else first
        result = super().delete(index1, index2)
        if first is not None and last is not None and last >= first:
            count = last - first + 1
            self._details = {number - (count if number > last else 0): detail
                             for number, detail in self._details.items()
                             if number < first or number > last}
        self._changed()
        return result

    def entryconfigure(self, index, cnf=None, **kwargs):
        if isinstance(cnf, str):
            if cnf in ('description', 'destructive'):
                return (cnf, '', '', '', self.entrycget(index, cnf))
            return super().entryconfigure(index, cnf, **kwargs)
        options = dict(cnf or {}, **kwargs)
        details = {key: options.pop(key) for key in ('description', 'destructive')
                   if key in options}
        if not options and not details:
            return super().entryconfigure(index)
        if options:
            super().entryconfigure(index, options)
        if details:
            self._details.setdefault(self.index(index), {}).update(details)
        self._changed()

    entryconfig = entryconfigure

    def entrycget(self, index, option):
        if option in ('description', 'destructive'):
            return self._details.get(self.index(index), {}).get(
                option, False if option == 'destructive' else '')
        return super().entrycget(index, option)

    def bind_to(self, button):
        """Retain ``button.cget('menu')`` while intercepting native posting."""
        if button in self._buttons:
            return self
        button.configure(menu=self)

        def open_menu(event=None, keyboard=False):
            if button.instate(('disabled',)):
                return 'break'
            if self._session is not None:
                if keyboard:
                    self._session.views[0].move(1)
                else:
                    self.unpost()
            else:
                self._open(button=button, keyboard=keyboard)
            return 'break'

        bindings = [('<ButtonPress-1>', open_menu),
                    ('<ButtonRelease-1>', lambda event: 'break')]
        for key in ('space', 'Return', 'KP_Enter', 'Down', 'F4'):
            bindings.append((f'<KeyPress-{key}>',
                             lambda event: open_menu(event, keyboard=True)))
        bindings.append(('<<Invoke>>', lambda event: open_menu(event, keyboard=True)))
        bindings.append(('<Destroy>', lambda event: self._button_destroyed(button, event)))
        self._buttons[button] = [(sequence, button.bind(sequence, callback, add='+'))
                                 for sequence, callback in bindings]
        return self

    def _button_destroyed(self, button, event):
        if event.widget == button:
            if self._session is not None and self._session.button == button:
                self.unpost()
            self._buttons.pop(button, None)

    @property
    def is_posted(self):
        return self._session is not None

    def post(self, x, y):
        self._open(position=(int(x), int(y)))

    def tk_popup(self, x, y, entry=''):
        self._open(position=(int(x), int(y)), keyboard=bool(entry != ''))
        if entry != '' and self._session is not None:
            self._session.views[0].select(self.index(entry))

    def unpost(self):
        if self._session is not None:
            self._session.dismiss()

    def _open(self, button=None, position=None, keyboard=False):
        self.unpost()
        command = self.cget('postcommand')
        if command:
            self.tk.call(command)
        if self.index('end') is None:
            return
        session = _Session(self, button)
        if session.start(position, keyboard):
            self._session = session

    def _changed(self):
        # Rebuilding a selected step must not leave stale callbacks visible.
        self.unpost()

    def destroy(self):
        self.unpost()
        for button, bindings in list(self._buttons.items()):
            for sequence, binding in bindings:
                try:
                    button.unbind(sequence, binding)
                except tk.TclError:
                    pass
        self._buttons.clear()
        super().destroy()


class _Session:
    def __init__(self, menu, button):
        self.menu, self.button = menu, button
        self.views = []
        self.previous_focus = menu.focus_get()
        self.previous_grab = menu.grab_current()
        self.previous_grab_status = (self.previous_grab.grab_status()
                                     if self.previous_grab is not None else None)
        self.focus_timer = None
        self.closed = False

    def start(self, position, keyboard):
        anchor = self.button or self.menu.master
        if (self.previous_grab is not None
                and not _inside(anchor, self.previous_grab)):
            # A separate modal picker/capture retains its existing input grab.
            return False
        view = _Popover(self, self.menu)
        self.views.append(view)
        view.place(position=position, anchor=self.button)
        view.window.grab_set()
        view.window.focus_set()
        if keyboard:
            view.move(1)
        return True

    def contains(self, widget):
        return any(_inside(widget, view.window) for view in self.views)

    def outside_press(self, event):
        for view in self.views:
            window = view.window
            if (window.winfo_rootx() <= event.x_root < window.winfo_rootx() + window.winfo_width()
                    and window.winfo_rooty() <= event.y_root < window.winfo_rooty() + window.winfo_height()):
                return
        self.dismiss()
        return 'break'

    def focus_out(self, event=None):
        if not self.closed and self.focus_timer is None:
            self.focus_timer = self.menu.after_idle(self.check_focus)

    def check_focus(self):
        self.focus_timer = None
        if not self.closed and not self.contains(self.menu.focus_get()):
            self.dismiss(restore_focus=False)

    def close_after(self, view):
        index = self.views.index(view)
        for child in reversed(self.views[index + 1:]):
            child.destroy()
        del self.views[index + 1:]

    def dismiss(self, restore_focus=True):
        if self.closed:
            return
        self.closed = True
        if self.focus_timer is not None:
            self.menu.after_cancel(self.focus_timer)
            self.focus_timer = None
        focused_here = self.contains(self.menu.focus_get())
        grab = self.menu.grab_current()
        if self.contains(grab):
            grab.grab_release()
        for view in reversed(self.views):
            view.destroy()
        self.views.clear()
        self.menu._session = None
        if self.previous_grab is not None:
            try:
                if self.previous_grab.winfo_exists():
                    if self.previous_grab_status == 'global':
                        self.previous_grab.grab_set_global()
                    else:
                        self.previous_grab.grab_set()
            except tk.TclError:
                pass
        if restore_focus and focused_here and self.previous_focus is not None:
            try:
                if self.previous_focus.winfo_exists():
                    self.previous_focus.focus_set()
            except tk.TclError:
                pass


class _Popover:
    def __init__(self, session, menu, parent=None, parent_index=None):
        self.session, self.menu = session, menu
        self.parent, self.parent_index = parent, parent_index
        self.selected = None
        self.rows = {}
        self.hover_timer = None
        master = parent.window if parent is not None else _owner(menu)
        self.window = tk.Toplevel(master, class_='RecorderPopup')
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.transient(_owner(menu))
        self.scale = menu.winfo_fpixels('1i') / 96.0
        style = ttk.Style(menu)
        self.colors = {
            'surface': style.lookup('Card.TFrame', 'background') or '#FFFFFF',
            'ink': style.lookup('TLabel', 'foreground') or '#252B39',
            'muted': style.lookup('Muted.TLabel', 'foreground') or '#626D80',
            'hover': style.lookup('Badge.TLabel', 'background') or '#ECEBFA',
            'line': '#DCE1EC', 'disabled': '#9AA1AF', 'danger': '#BD3B46',
        }
        self.window.configure(background=self.colors['line'], borderwidth=0,
                              highlightthickness=0)
        panel = tk.Frame(self.window, background=self.colors['surface'],
                         borderwidth=0, padx=self.px(4), pady=self.px(4))
        panel.pack(fill='both', expand=True, padx=1, pady=1)
        self._description_font()
        end = menu.index('end')
        for index in range((end if end is not None else -1) + 1):
            kind = menu.type(index)
            if kind == 'tearoff':
                continue
            if kind == 'separator':
                tk.Frame(panel, height=1, background=self.colors['line']).pack(
                    fill='x', padx=self.px(8), pady=self.px(5))
                continue
            self._row(panel, index, kind)
        self.window.bind('<ButtonPress-1>', session.outside_press)
        self.window.bind('<FocusOut>', session.focus_out)
        self.window.bind('<Escape>', lambda event: self._escape())
        self.window.bind('<Up>', lambda event: self.move(-1))
        self.window.bind('<Down>', lambda event: self.move(1))
        self.window.bind('<Home>', lambda event: self.move(1, edge=True))
        self.window.bind('<End>', lambda event: self.move(-1, edge=True))
        self.window.bind('<Right>', lambda event: self.open_child(keyboard=True))
        self.window.bind('<Left>', lambda event: self._back())
        for key in ('Return', 'KP_Enter', 'space'):
            self.window.bind(f'<KeyPress-{key}>', lambda event: self.activate())
        self.window.bind('<Tab>', lambda event: self._escape())

    def px(self, value):
        return max(1, round(value * self.scale))

    def _description_font(self):
        name = 'RecorderMenuDescriptionFont'
        if name not in tkfont.names(self.menu):
            body = tkfont.nametofont('TkMenuFont', root=self.menu).actual()
            body['size'] = max(8, body['size'] - 1) if body['size'] > 0 else body['size'] + 1
            # Tcl owns this named font for the lifetime of this interpreter.
            options = [part for key, value in body.items() for part in (f'-{key}', value)]
            self.menu.tk.call('font', 'create', name, *options)

    def _extra(self, index, option):
        if isinstance(self.menu, PopupMenu):
            return self.menu.entrycget(index, option)
        return False if option == 'destructive' else ''

    def _row(self, panel, index, kind):
        disabled = self.menu.entrycget(index, 'state') == 'disabled'
        foreground = (self.colors['disabled'] if disabled else
                      self.colors['danger'] if self._extra(index, 'destructive') else
                      self.colors['ink'])
        row = tk.Frame(panel, background=self.colors['surface'],
                       padx=self.px(10), pady=self.px(7), takefocus=False)
        row.pack(fill='x')
        label = tk.Label(row, text=self.menu.entrycget(index, 'label'),
                         font='TkMenuFont', background=self.colors['surface'],
                         foreground=foreground, anchor='w', borderwidth=0,
                         padx=0, pady=0)
        label.grid(row=0, column=0, sticky='w')
        description = str(self._extra(index, 'description') or '')
        if description:
            tk.Label(row, text=description, font='RecorderMenuDescriptionFont',
                     foreground=self.colors['disabled'] if disabled else self.colors['muted'],
                     background=self.colors['surface'], anchor='w', justify='left',
                     padx=0, pady=0).grid(row=1, column=0, sticky='w', pady=(self.px(2), 0))
        row.columnconfigure(0, weight=1)
        if kind == 'cascade':
            tk.Label(row, text='›', font='TkMenuFont', foreground=foreground,
                     background=self.colors['surface'], padx=self.px(12), pady=0).grid(
                         row=0, column=1, rowspan=2, sticky='e')
        self.rows[index] = row
        for widget in (row, *row.winfo_children()):
            widget.bind('<Enter>', lambda event, i=index: self.select(i, hover=True))
            widget.bind('<ButtonRelease-1>', lambda event, i=index: self.activate(i))

    def enabled(self):
        return [index for index in self.rows
                if self.menu.entrycget(index, 'state') != 'disabled']

    def select(self, index, hover=False):
        if index not in self.enabled():
            return 'break'
        if self.hover_timer is not None:
            self.window.after_cancel(self.hover_timer)
            self.hover_timer = None
        if index != self.selected:
            self.session.close_after(self)
        self.selected = index
        for number, row in self.rows.items():
            color = self.colors['hover'] if number == index else self.colors['surface']
            row.configure(background=color)
            for label in row.winfo_children():
                label.configure(background=color)
        if hover and self.menu.type(index) == 'cascade':
            self.hover_timer = self.window.after(150, self.open_child)
        return 'break'

    def move(self, direction, edge=False):
        available = self.enabled()
        if available:
            if edge or self.selected not in available:
                index = available[0] if direction > 0 else available[-1]
            else:
                index = available[(available.index(self.selected) + direction) % len(available)]
            self.select(index)
        return 'break'

    def activate(self, index=None):
        if index is None:
            index = self.selected
        if index not in self.enabled():
            return 'break'
        self.select(index)
        if self.menu.type(index) == 'cascade':
            return self.open_child(keyboard=True)
        self.session.dismiss()
        self.menu.invoke(index)
        return 'break'

    def open_child(self, keyboard=False):
        self.hover_timer = None
        index = self.selected
        if index not in self.enabled() or self.menu.type(index) != 'cascade':
            return 'break'
        next_index = self.session.views.index(self) + 1
        if len(self.session.views) > next_index:
            child = self.session.views[next_index]
        else:
            name = self.menu.entrycget(index, 'menu')
            if not name:
                return 'break'
            menu = self.menu.nametowidget(name)
            command = menu.cget('postcommand')
            if command:
                menu.tk.call(command)
            child = _Popover(self.session, menu, self, index)
            self.session.views.append(child)
            if isinstance(menu, PopupMenu):
                menu._session = self.session
            child.place(anchor=self.rows[index])
        if keyboard:
            child.window.focus_set()
            child.move(1)
        return 'break'

    def _back(self):
        if self.parent is not None:
            self.parent.window.focus_set()
            self.session.close_after(self.parent)
        return 'break'

    def _escape(self):
        if self.parent is not None:
            return self._back()
        self.session.dismiss()
        return 'break'

    def place(self, position=None, anchor=None):
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        gap = self.px(3)
        if anchor is not None:
            x, y = anchor.winfo_rootx(), anchor.winfo_rooty()
        else:
            x, y = position
        left, top, right, bottom = _screen_bounds(self.menu, x, y)
        margin = self.px(4)
        if self.parent is not None:
            x = self.parent.window.winfo_rootx() + self.parent.window.winfo_width() + gap
            if x + width > right - margin:
                x = self.parent.window.winfo_rootx() - width - gap
        elif anchor is not None:
            below = y + anchor.winfo_height() + gap
            y = below if below + height <= bottom - margin else y - height - gap
            width = max(width, anchor.winfo_width())
        x = max(left + margin, min(x, right - width - margin))
        y = max(top + margin, min(y, bottom - height - margin))
        # A leading '+' keeps negative virtual-screen coordinates absolute.
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        self.window.deiconify()
        self.window.lift()
        self.window.update_idletasks()

    def destroy(self):
        if self.hover_timer is not None:
            self.window.after_cancel(self.hover_timer)
            self.hover_timer = None
        if isinstance(self.menu, PopupMenu):
            self.menu._session = None
        self.window.destroy()
