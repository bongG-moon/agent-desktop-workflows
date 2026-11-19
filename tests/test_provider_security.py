"""Unknown child protection stays strict when a native dialog omits its flag."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import control_lookup
import provider_security as security


class Node:
    def __init__(self, **changes):
        self.props = {30019: None, 30003: 50032, 30012: '#32770', 30020: 11, 30002: 7}
        self.props.update({int(key): value for key, value in changes.items()})
        self.reads = []

    def property(self, key):
        self.reads.append(key)
        return self.props.get(key)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.element, self.current = Node(), Node()
        self.win = SimpleNamespace(user32=Mock(), hwnd_pid=Mock(return_value=7),
                                   root_window=Mock(return_value=11), window_class=Mock(return_value='#32770'))
        self.win.user32.IsWindow.return_value = True
        self.uia = SimpleNamespace(pid=lambda item: item.property(30002),
                                   from_handle=Mock(return_value=self.current), same=Mock(return_value=True))

    def protected(self, element=None):
        return security.secure(self.win, self.uia, element or self.element)

    def test_missing_flag_on_proven_native_dialog_is_traversable(self):
        self.assertFalse(self.protected())
        self.uia.from_handle.assert_called_once_with(11)
        self.assertNotIn(30005, self.element.reads)  # no name/value reads to prove safety
        self.assertNotIn(30045, self.element.reads)

    def test_explicit_password_flag_is_never_relaxed(self):
        self.element.props[30019] = True
        self.assertTrue(self.protected())
        self.uia.from_handle.assert_not_called()

    def test_explicit_false_keeps_existing_non_dialog_behavior(self):
        self.element.props.update({30019: False, 30003: 50004, 30012: 'Edit'})
        self.assertFalse(self.protected())
        self.win.user32.IsWindow.assert_not_called()

    def test_unknown_children_and_custom_windows_remain_protected(self):
        for role in (50000, 50003, 50004, 50020, 50026):
            with self.subTest(role=role):
                self.assertTrue(self.protected(Node(**{'30003': role})))
        self.element.props[30012] = 'CustomWindow'
        self.assertTrue(self.protected())
        self.uia.from_handle.assert_not_called()

    def test_editable_pattern_on_window_remains_protected(self):
        for key in security._INPUT_PATTERNS:
            with self.subTest(key=key):
                self.assertTrue(self.protected(Node(**{str(key): True})))

    def test_invalid_hwnd_pid_or_nonboolean_security_is_protected(self):
        for key, values in ((30020, (None, 0, -1, True, '11')),
                            (30002, (None, 0, -1, True, '7')),
                            (30019, (0, 1, 'False'))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertTrue(self.protected(Node(**{str(key): value})))

    def test_foreign_or_child_hwnd_and_native_class_mismatch_are_rejected(self):
        for name, value in (('hwnd_pid', 9), ('root_window', 10), ('window_class', 'Edit')):
            method = getattr(self.win, name)
            original = method.return_value
            method.return_value = value
            self.assertTrue(self.protected())
            method.return_value = original
        self.win.user32.IsWindow.return_value = False
        self.assertTrue(self.protected())

    def test_round_trip_rejects_synthetic_or_replaced_uia_element(self):
        self.uia.same.return_value = False
        self.assertTrue(self.protected())
        self.uia.same.return_value = True
        self.uia.from_handle.return_value = None
        self.assertTrue(self.protected())

    def test_fresh_element_must_keep_native_identity_and_noneditable_metadata(self):
        for key, value in ((30019, True), (30003, 50004), (30012, 'Edit'),
                           (30020, 12), (30002, 8), (30043, True)):
            self.uia.from_handle.return_value = Node(**{str(key): value})
            self.assertTrue(self.protected())

    def test_destroyed_hwnd_after_round_trip_is_rejected_without_cache(self):
        self.win.user32.IsWindow.side_effect = [True, False]
        self.assertTrue(self.protected())
        self.win.user32.IsWindow.side_effect = None
        self.assertFalse(self.protected())
        self.win.user32.IsWindow.return_value = False
        self.assertTrue(self.protected())

    def test_provider_or_native_error_keeps_target_protected(self):
        self.uia.from_handle.side_effect = RuntimeError('unavailable')
        self.assertTrue(self.protected())

    def test_ancestry_still_rejects_unknown_and_password_children(self):
        root = self.element
        self.uia.secure = lambda item: self.protected(item)
        self.uia.same.side_effect = lambda a, b: a is b or {id(a), id(b)} == {id(root), id(self.current)}
        for protected in (None, True):
            child = Node(**{'30003': 50004, '30019': protected, '30020': 12})
            with patch('control_lookup._raw_parent', return_value=root):
                with self.assertRaisesRegex(app.bridge.BridgeError, 'target_protected'):
                    control_lookup.ancestry(self.uia, child, root, pid=7)
        child = Node(**{'30003': 50004, '30019': False, '30020': 12})
        with patch('control_lookup._raw_parent', return_value=root):
            self.assertEqual(control_lookup.ancestry(self.uia, child, root, pid=7), [child, root])

    def test_install_is_idempotent_and_leaves_original_class_unchanged(self):
        class Base:
            def secure(self, element):
                return element.property(30019) is not False
        module = SimpleNamespace(UIA=Base)
        self.assertIs(security.install(module), module)
        adapted = module.UIA
        self.assertIs(security.install(module).UIA, adapted)
        self.assertTrue(issubclass(adapted, Base))
        self.assertTrue(Base().secure(self.element))

    def test_existing_bridge_load_uses_same_compatibility_class(self):
        current = app.bridge.UIA
        self.assertIs(app.load_bridge(), app.bridge)
        self.assertIs(app.load_bridge().UIA, current)

    def test_native_passive_list_caption_and_item_inherit_verified_owner_protection(self):
        owner = Node(**{'30003': 50008, '30019': False})
        item = Node(**{'30003': 50007, '30090': True, '30095': 34, '30096': 2})
        caption = Node(**{'30003': 50020, '30090': True, '30095': 41, '30096': 0})
        self.uia.parent = lambda node: {id(caption): item, id(item): owner}.get(id(node))
        self.uia.same.side_effect = lambda a, b: a is b
        self.uia.from_handle.return_value = owner
        self.uia.bounds = lambda node: dict(x=10, y=20, width=200, height=60)
        self.win.window_class.return_value = 'SysListView32'
        self.assertFalse(self.protected(item))
        self.assertFalse(self.protected(caption))
        wrapped = 'WindowsForms10.SysListView32.app.0.34f5582_r6_ad1'
        self.win.window_class.return_value = wrapped
        self.assertTrue(self.protected(caption))
        owner.props.update({30090:True, 30095:33, 30096:1048580})
        self.assertFalse(self.protected(caption))
        for wrong in (wrapped + '.Edit', wrapped.replace('SysListView32', 'Edit')):
            self.win.window_class.return_value = wrong
            self.assertTrue(self.protected(caption))
        self.win.window_class.return_value = wrapped
        for prop, value in ((30043, True), (30095, 41), (30096, 0x20000000)):
            with patch.dict(owner.props, {prop:value}):
                self.assertTrue(self.protected(caption))
        self.win.window_class.return_value = 'SysListView32'
        self.assertNotIn(30005, item.reads)
        self.assertNotIn(30045, item.reads)
        for props in ({30019: True}, {30096: 0x20000000}, {30043: True}, {30002: 99}):
            with self.subTest(props=props):
                previous = dict(item.props)
                item.props.update(props)
                self.assertTrue(self.protected(caption))
                item.props = previous
        for cls in ('Edit', 'OtherList'):
            self.win.window_class.return_value = cls
            self.assertTrue(self.protected(item))
        self.win.window_class.return_value = 'SysListView32'
        owner.props[30019] = None
        self.assertTrue(self.protected(item))


if __name__ == '__main__':
    unittest.main()
