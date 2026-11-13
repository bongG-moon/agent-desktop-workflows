"""Keep unknown input protection strict while admitting native dialog shells.

Windows' standard dialog provider can omit IsPassword on the dialog itself.
That shell is not an editable control. Its children still require their own
explicit, unprotected metadata and normal binding/ancestor validation.
"""


_WINDOW = 50032
_DIALOG_CLASS = '#32770'
_INPUT_PATTERNS = (30033, 30040, 30043, 30149)  # RangeValue, Text, Value, TextEdit.


def _shell_metadata(element):
    return (element.property(30019) is None
            and element.property(30003) == _WINDOW
            and element.property(30012) == _DIALOG_CLASS
            and all(element.property(key) is None or element.property(key) is False
                    for key in _INPUT_PATTERNS))


def secure(win, uia, element):
    """Classify protection; this never authorizes a target or widens its scope."""
    try:
        protected = element.property(30019)
        if protected is False:
            return False
        if protected is not None or not _shell_metadata(element):
            return True
        hwnd, pid = element.property(30020), uia.pid(element)
        if (type(hwnd) is not int or not 0 < hwnd <= 0x7fffffffffffffff
                or type(pid) is not int or not 0 < pid <= 0x7fffffff):
            return True

        def native_shell():
            return (bool(win.user32.IsWindow(hwnd)) and win.hwnd_pid(hwnd) == pid
                    and win.root_window(hwnd) == hwnd and win.window_class(hwnd) == _DIALOG_CLASS)

        if not native_shell():
            return True
        current = uia.from_handle(hwnd)
        if (current is None or not uia.same(element, current) or uia.pid(current) != pid
                or current.property(30020) != hwnd or not _shell_metadata(current)):
            return True
        # A destroyed/replaced HWND or a changed protection flag invalidates
        # the evidence gathered above. No permissive result is cached.
        return not (native_shell() and _shell_metadata(element))
    except Exception:
        return True


def install(win):
    """Install once on Desktop Select's bridge module, retaining pinned bytes."""
    if getattr(win, '_desktop_select_security_compat', False):
        return win
    base = win.UIA

    class DesktopSelectUIA(base):
        def secure(self, element):
            return secure(win, self, element)

    win.UIA = DesktopSelectUIA
    win._desktop_select_security_compat = True
    return win
