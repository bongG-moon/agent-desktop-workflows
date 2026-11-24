"""Named, application-scoped shortcuts shared by the recorder and replay."""

TARGET_ROLES = frozenset({
    'button', 'calendar', 'checkbox', 'combobox', 'custom', 'document', 'group',
    'hyperlink', 'list', 'listitem', 'menu', 'menuitem', 'radiobutton', 'row',
    'slider', 'spinner', 'splitbutton', 'tab', 'tabgroup', 'table', 'textfield',
    'toolbar', 'tree', 'treeitem',
})

_NAVIGATION = {
    0x09: 'Tab', 0x21: 'PageUp', 0x22: 'PageDown', 0x23: 'End', 0x24: 'Home',
    0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down',
}
_ARROWS = frozenset(range(0x25, 0x29))
EXTENDED_KEYS = frozenset(range(0x21, 0x29))
_FUNCTIONS = {0x70 + index: f'F{index + 1}' for index in range(12)}
_BASE_NAMES = {0x0D: 'Enter', 0x1B: 'Esc', **_NAVIGATION, **_FUNCTIONS}
# Clipboard and editing shortcuts must not reapply an edit after the recorder
# has already stored the field's final value.
_CONTROL_LETTERS = frozenset(range(ord('A'), ord('Z') + 1)).difference(map(ord, 'ACVXYZ'))
_CONTROL_DIGITS = range(0x30, 0x3A)
_MODIFIERS = {'Ctrl': 0x11, 'Alt': 0x12, 'Shift': 0x10}
_CODES = {name: code for code, name in _BASE_NAMES.items()}
_CODES.update({chr(code): code for code in (*_CONTROL_LETTERS, *_CONTROL_DIGITS)})


def from_vk(vk, *, ctrl=False, shift=False, alt=False, win=False):
    """Return the canonical app shortcut, or None for text, system or unknown input."""
    if win or (alt and (ctrl or shift)):
        return None
    if alt:
        return 'Alt+' + _NAVIGATION[vk] if vk in _ARROWS else None
    if vk == 0x1B:
        return 'Esc' if not (ctrl or shift) else None
    if vk == 0x0D and not (ctrl or shift):
        return None  # Plain Enter is the recorder's existing submit action.
    base = _BASE_NAMES.get(vk)
    if base is None:
        if not ctrl or (vk not in _CONTROL_LETTERS and vk not in _CONTROL_DIGITS):
            return None
        base = chr(vk)
    return ('Ctrl+' if ctrl else '') + ('Shift+' if shift else '') + base


def virtual_keys(keys):
    """Validate persisted data and return modifier-down order followed by the main key."""
    if not isinstance(keys, str) or not 1 <= len(keys) <= 24:
        return ()
    *modifiers, name = keys.split('+')
    code = _CODES.get(name)
    if code is None or keys != from_vk(code, ctrl='Ctrl' in modifiers,
                                      alt='Alt' in modifiers, shift='Shift' in modifiers):
        return ()
    return tuple(_MODIFIERS[part] for part in modifiers) + (code,)
