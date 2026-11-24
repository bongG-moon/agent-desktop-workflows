"""Resolve native dialog shells and passive list peers with missing IsPassword.

Editable inputs require explicit unprotected metadata. Native list captions
and items can also be proved through their current noneditable metadata and
positively unprotected SysListView32 owner.
"""
import re


_WINDOW = 50032
_DIALOG_CLASS = '#32770'
_INPUT_PATTERNS = (30033, 30040, 30043, 30149)  # RangeValue, Text, Value, TextEdit.
_WINFORMS_LIST = re.compile(r'WindowsForms10\.SysListView32\.app\.\d+\.[0-9a-fA-F]+_r\d+_ad\d+')


def _passive_list_peer(win, uia, element):
    """Native list providers omit IsPassword on their noneditable item peers."""
    role = element.property(30003)
    if role not in (50007, 50020):
        return False
    pid = uia.pid(element)
    peers = [element]
    item = element
    if role == 50020:
        item = uia.parent(element)
        if item is None or item.property(30003) != 50007:
            return False
        peers.append(item)
    owner = uia.parent(item)
    if (owner is None or owner.property(30003) != 50008 or owner.property(30019) is not False
            or uia.pid(owner) != pid):
        return False
    hwnd = win.normalize_hwnd(owner.property(30020))
    if (not hwnd or type(pid) is not int or pid <= 0
            or not win.user32.IsWindow(hwnd) or win.hwnd_pid(hwnd) != pid):
        return False
    native_class = win.window_class(hwnd)
    if native_class != 'SysListView32':
        if not _WINFORMS_LIST.fullmatch(native_class):
            return False
        state = owner.property(30096)
        if (owner.property(30090) is not True or owner.property(30095) != 33
                or type(state) is not int or state & 0x20000000
                or any(owner.property(k) is True for k in _INPUT_PATTERNS)):
            return False
    current = uia.from_handle(hwnd)
    if (current is None or not uia.same(current, owner) or uia.pid(current) != pid
            or current.property(30019) is not False):
        return False
    box = uia.bounds(owner)
    for peer in peers:
        state = peer.property(30096)
        rect = uia.bounds(peer)
        if (peer.property(30019) not in (None, False) or uia.pid(peer) != pid
                or peer.property(30090) is not True
                or peer.property(30095) != (34 if peer.property(30003) == 50007 else 41)
                or type(state) is not int or state & 0x20000000
                or any(peer.property(key) not in (None, False) for key in _INPUT_PATTERNS)
                or not all(type(rect[k]) in (int, float) for k in ('x', 'y', 'width', 'height'))
                or rect['width'] <= 0 or rect['height'] <= 0
                or not all(box[a] <= rect[a] and rect[a]+rect[s] <= box[a]+box[s]
                           for a, s in (('x', 'width'), ('y', 'height')))):
            return False
    return (uia.same(uia.parent(item), owner) and owner.property(30019) is False
            and win.user32.IsWindow(hwnd) and win.hwnd_pid(hwnd) == pid)


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
        if protected is None and _passive_list_peer(win, uia, element):
            return False
        if protected is not None or not _shell_metadata(element):
            return True
        hwnd, pid = win.normalize_hwnd(element.property(30020)), uia.pid(element)
        if (not hwnd
                or type(pid) is not int or not 0 < pid <= 0x7fffffff):
            return True

        def native_shell():
            return (bool(win.user32.IsWindow(hwnd)) and win.hwnd_pid(hwnd) == pid
                    and win.root_window(hwnd) == hwnd and win.window_class(hwnd) == _DIALOG_CLASS)

        if not native_shell():
            return True
        current = uia.from_handle(hwnd)
        if (current is None or not uia.same(element, current) or uia.pid(current) != pid
                or win.normalize_hwnd(current.property(30020)) != hwnd or not _shell_metadata(current)):
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
