"""Provider omission is not invisibility, and must not bypass window ownership or geometry."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import provider_visibility as visibility


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.binding = Mock(pid=42, hwnd=11)
        self.binding.scoped_windows.return_value = [11]
        self.item, self.root = Mock(), Mock()
        self.item.property.return_value = None
        self.window = dict(x=300, y=200, width=500, height=400)
        self.box = dict(x=350, y=230, width=170, height=19)
        self.uia = Mock()
        self.uia.bounds.side_effect = lambda e:self.window if e is self.root else self.box
        self.uia.pid.return_value = 42
        self.uia.secure.return_value = False
        self.uia.within.return_value = True
        self.uia.from_handle.return_value = self.root
        patch.object(visibility,'_shown',return_value=True).start()
        self.addCleanup(patch.stopall)

    def shown(self):
        return visibility.visible(self.binding,self.uia,self.item)

    def test_missing_property_uses_real_containment_direction(self):
        self.assertTrue(self.shown())
        self.box['x'] = self.window['x']-1
        self.assertFalse(self.shown())
        self.box.update(x=780,width=50)
        self.assertFalse(self.shown())

    def test_missing_window_bounds_do_not_skip_geometry(self):
        self.window['width'] = 0
        with patch.object(visibility,'_window_box',return_value=None):
            self.assertFalse(self.shown())
        with patch.object(visibility,'_window_box',return_value=dict(x=300,y=200,width=500,height=400)):
            self.assertTrue(self.shown())

    def test_minimized_hidden_foreign_password_or_detached_is_blocked(self):
        self.shown_mock(False)
        self.assertFalse(self.shown())
        self.shown_mock(True)
        for setting,value in (('pid',99),('secure',True),('within',False)):
            method=getattr(self.uia,setting)
            prior=method.return_value
            method.return_value=value
            self.assertFalse(self.shown())
            method.return_value=prior

    def shown_mock(self, value):
        visibility._shown.return_value=value

    def test_true_and_zero_bounds_are_never_geometry_override(self):
        for state in (True, 0, 'false'):
            self.item.property.return_value=state
            self.assertFalse(self.shown())
        self.item.property.return_value=None
        self.box['width']=0
        self.assertFalse(self.shown())

    def test_read_only_inspect_does_not_restore(self):
        args=Mock(command='inspect',popup=None,window='Title',target=None)
        with patch.object(desktop_select,'restore_for_action') as restore, \
             patch.object(desktop_select,'bind_surface',return_value=self.binding), \
             patch.object(desktop_select,'guard'), patch.object(desktop_select.bridge,'UIA') as uia, \
             patch.object(desktop_select,'observe',return_value=([],[])), \
             patch.object(desktop_select,'owned_popups',return_value=[]):
            desktop_select.action(args,self.binding)
            restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
