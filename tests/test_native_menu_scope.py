"""Ownerless native popup menus need the exact target GUI thread's menu owner."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import context_menu as menu
from test_control_lookup import FakeUIA, Node, Binding, win


class NativeMenuScopeTests(unittest.TestCase):
    def setUp(self):
        self.binding = Mock(hwnd=100, pid=7)
        self.binding.scoped_windows.return_value = [100]
        self.binding.contains_window.side_effect = lambda hwnd: hwnd in (100, 101)
        self.flags, self.owner, self.gui_ok = 0x14, 100, True
        self.identities = {100: (9, 7), 101: (9, 7), 200: (9, 7), 201: (10, 7), 202: (9, 8)}
        self.classes = {200: '#32768', 201: '#32768', 202: '#32768', 203: 'Unrelated'}
        self.enumerated = [200, 201, 202, 203]
        self.shown = set(self.enumerated)
        for target, replacement in (
            ('_thread_identity', lambda hwnd: self.identities.get(hwnd, (9, 7))),
        ):
            p = patch.object(menu, target, replacement)
            p.start()
            self.addCleanup(p.stop)
        user32 = Mock()
        user32.IsWindowVisible.side_effect = lambda hwnd: hwnd in self.shown
        for target, replacement in (
            ('user32', user32), ('window_class', lambda hwnd: self.classes.get(hwnd, 'Main')),
            ('root_window', lambda hwnd: hwnd), ('_sig', self.signature),
        ):
            p = patch.object(menu.win, target, replacement)
            p.start()
            self.addCleanup(p.stop)

    def signature(self, dll, name, *types):
        if name == 'GetGUIThreadInfo':
            def read(thread, output):
                self.assertEqual(thread, 9)
                self.assertGreater(output._obj.cbSize, 0)
                output._obj.flags = self.flags
                output._obj.hwndMenuOwner = self.owner
                return self.gui_ok
            return read
        self.assertEqual(name, 'EnumThreadWindows')
        def enum(thread, callback, context):
            self.assertEqual(thread, 9)
            for hwnd in self.enumerated:
                if not callback(hwnd, context):
                    return False
            return True
        return enum

    def test_real_native_menu_metadata_shape_is_allowed_only_for_target_thread(self):
        # Same shape as standard Win32 menus: owner=0 on the popup HWND,
        # while GUITHREADINFO points to the exact business window.
        self.assertEqual(menu._native_menu_owner(self.binding), (9, 100))
        self.assertEqual(menu._scoped_windows(self.binding), [100, 200])
        self.assertTrue(menu._contains_window(self.binding, 200))
        self.assertFalse(menu._contains_window(self.binding, 201))
        self.assertFalse(menu._contains_window(self.binding, 202))

    def test_nonzero_menu_owner_without_active_menu_flags_is_rejected(self):
        self.flags = 0
        self.assertIsNone(menu._native_menu_owner(self.binding))
        self.assertEqual(menu._scoped_windows(self.binding), [100])

    def test_menu_owned_by_other_same_process_window_is_rejected(self):
        self.owner = 300
        self.assertEqual(menu._scoped_windows(self.binding), [100])

    def test_menu_owner_in_other_thread_and_failed_query_are_rejected(self):
        self.owner = 101
        self.identities[101] = (10, 7)
        self.assertIsNone(menu._native_menu_owner(self.binding))
        self.owner = 100
        self.gui_ok = False
        self.assertEqual(menu._scoped_windows(self.binding), [100])

    def test_hidden_native_menu_and_wrong_window_class_are_excluded(self):
        self.shown.remove(200)
        self.assertEqual(menu._scoped_windows(self.binding), [100])

    def test_menu_owner_is_rechecked_after_enumeration(self):
        with patch.object(menu, '_native_menu_owner', side_effect=[(9, 100), None]), \
             self.assertRaisesRegex(menu.win.BridgeError, 'context_menu_owner_changed'):
            menu._scoped_windows(self.binding)

    def test_target_thread_and_pid_must_match_live_binding(self):
        self.identities[100] = (9, 8)
        self.assertIsNone(menu._native_menu_owner(self.binding))

    def test_no_global_or_same_process_menu_grant_survives_menu_close(self):
        self.assertTrue(menu._contains_window(self.binding, 200))
        self.owner = 0
        self.assertFalse(menu._contains_window(self.binding, 200))

    def test_unknown_visibility_uses_proven_native_surface_and_rechecks_owner(self):
        item, uia = Mock(), Mock()
        item.property.return_value = None
        with patch.object(menu.visibility, 'visible', side_effect=lambda surface, *args: surface.hwnd == 200):
            self.assertTrue(menu._menu_visible(self.binding, uia, item))
        def closes_while_reading(surface, *args):
            if surface.hwnd == 200:
                self.owner = 0
                return True
            return False
        with patch.object(menu.visibility, 'visible', side_effect=closes_while_reading):
            self.assertFalse(menu._menu_visible(self.binding, uia, item))


class HostMenuLookupTests(unittest.TestCase):
    def setUp(self):
        self.uia, self.binding = FakeUIA(), Binding()
        self.root = self.uia.root
        self.frame = self.node('Context', 50009)
        self.item = self.node('Export data', 50011, self.frame)
        self.uia.bounds = lambda node: dict(x=50, y=50, width=200, height=40)
        self.uia.children = Mock(side_effect=AssertionError('must not walk main window children'))
        for target, replacement in (
            ('_Com', lambda pointer, uia: uia.handles[pointer.value]),
            ('Element', lambda pointer, uia: uia.nodes[pointer.value]),
            ('_sig', self.uia.allocation),
        ):
            p = patch.object(win, target, replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(menu, '_scoped_windows', return_value=[123])
        self.scopes = p.start()
        self.addCleanup(p.stop)

    def node(self, name, role, parent=None, **extra):
        node = Node(self.uia, '', role, parent=parent or self.root, name=name, **extra)
        node.values[30022] = False
        return node

    def test_native_query_skips_unrelated_form_tree_and_retains_context_item(self):
        unrelated = [self.node('Field ' + str(i), 50004) for i in range(600)]
        items = menu._menus(self.binding, self.uia)
        self.assertEqual([(x.element, x.menu, x.surface) for x in items], [(self.item, self.frame, 123)])
        self.uia.children.assert_not_called()
        self.assertTrue(all(not node.reads for node in unrelated))
        self.assertEqual(self.uia.queries[0][3], ('property', 30003, 50011))

    def test_menu_bar_and_unframed_same_name_are_excluded(self):
        bar = self.node('Main', 50010)
        command = self.node('Export data', 50011, bar)
        loose = self.node('Export data', 50011)
        items = menu._menus(self.binding, self.uia)
        self.assertEqual([x.element for x in items], [self.item])
        self.assertNotIn(30005, command.reads)
        self.assertNotIn(30005, loose.reads)

    def test_nearest_menu_frame_below_menubar_is_preserved(self):
        bar = self.node('Main', 50010)
        nested = self.node('Nested context', 50009, bar)
        extra = self.node('Second export', 50011, nested)
        self.assertEqual([x.element for x in menu._menus(self.binding, self.uia)], [self.item, extra])

    def test_raw_protected_ancestor_prevents_candidate_name_read(self):
        protected = self.node('Protected', 50026, secure=True)
        hidden = self.node('Secret command', 50011, protected)
        self.assertEqual([x.element for x in menu._menus(self.binding, self.uia)], [self.item])
        self.assertNotIn(30005, hidden.reads)

    def test_popup_bfs_and_popup_first_duplicate_provenance_are_preserved(self):
        popup = Node(self.uia, '', 50009, name='Popup')
        popup.values[30022] = False
        self.scopes.return_value = [123, 456]
        self.uia.from_handle = lambda hwnd: self.root if hwnd == 123 else popup
        def children(node):
            if node is popup:
                return [self.item]
            if node is self.item:
                return []
            raise AssertionError('host full walk')
        self.uia.children.side_effect = children
        items = menu._menus(self.binding, self.uia)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].surface, 456)
        self.assertIs(items[0].menu, popup)

    def test_incomplete_native_search_fails_closed(self):
        self.uia.query_rows = [self.item] * 4097
        with self.assertRaisesRegex(win.BridgeError, 'target_candidates_incomplete'):
            menu._menus(self.binding, self.uia)
        self.assertNotIn(30005, self.item.reads)
        self.uia.query_rows = None
        self.uia.query_error = 'accessibility_unavailable'
        with self.assertRaisesRegex(win.BridgeError, 'accessibility_unavailable'):
            menu._menus(self.binding, self.uia)


if __name__ == '__main__':
    unittest.main()
