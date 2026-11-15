"""List only the intended recording windows; bind the same choice on confirmation."""
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import capture_windows as windows
from capture_trail import CaptureTrail
from test_window_recording import Binding, Part, clicked


class OpenWindowTests(TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.rows = {
            1: (100, 'Manager.exe', 'Manager', '10'), 2: (200, 'Device.exe', 'First device', '30'),
            3: (300, 'Device.exe', 'Second device', '31'), 4: (400, 'Editor.exe', 'Private title', '20'),
            5: (500, 'Dialog.exe', 'Owned dialog', '21'),
        }
        self.parents = {200:170, 170:100, 300:100, 400:18, 500:19}
        self.identities = {pid:(exe, created) for pid,exe,_,created in self.rows.values()}
        self.identities[170] = ('Launch.exe','22')
        self.minimized = {2}
        self.visible = lambda: {(hwnd,row[0]) for hwnd,row in self.rows.items()}
        self.parents_read = stack.enter_context(mock.patch.object(windows, '_parents', return_value=self.parents))
        stack.enter_context(mock.patch.object(windows, 'visible_window_keys', side_effect=self.visible))
        stack.enter_context(mock.patch.object(windows, '_key', side_effect=lambda h: (h,self.rows[h][0]) if h in self.rows else None))
        dll = stack.enter_context(mock.patch.object(windows.bridge, 'user32'))
        dll.IsWindowVisible.side_effect = lambda h: h in self.rows
        dll.IsIconic.side_effect = lambda h: h in self.minimized
        stack.enter_context(mock.patch.object(windows.bridge, 'window_class', return_value='App'))
        stack.enter_context(mock.patch.object(windows.bridge, 'owned', side_effect=lambda h,p: (h,p)==(5,1)))
        self.captions = stack.enter_context(mock.patch.object(windows.bridge, 'window_text',
                                                       side_effect=lambda h:self.rows[h][2]))
        stack.enter_context(mock.patch.object(windows.bridge, '_process_identity', side_effect=lambda p:self.identities[p]))
        def listed(exe):
            return [dict(hwnd=h,pid=p,title=t,process_started_utc_ticks=g)
                    for h,(p,e,t,g) in self.rows.items() if e==exe]
        stack.enter_context(mock.patch.object(app, 'windows', side_effect=listed))
        def bind(record):
            h=record['hwnd']
            p,e,t,g=self.rows[h]
            if (record['pid'],record['exe_name'],record['process_started_utc_ticks']) != (p,e,g):
                raise windows.bridge.BridgeError('target_lifetime_changed')
            b=Binding(h,p,t)
            b.exe,b.created=e,g
            return b
        stack.enter_context(mock.patch.object(windows.bridge, 'Binding', side_effect=bind))
        self.manager=bind(dict(hwnd=1,pid=100,exe_name='Manager.exe',process_started_utc_ticks='10'))
        self.trail=CaptureTrail(self.manager, self.manager.title, Part)
        self.trail.start()
        self.addCleanup(self.trail.cancel)

    def choices(self, all_windows=False):
        return windows.window_choices(*self.trail.choice_scope(), all_windows=all_windows)

    def test_scoped_list_checks_relationship_before_reading_any_unrelated_title(self):
        rows=self.choices()
        self.assertEqual([r['title'] for r in rows], ['Manager','First device','Second device','Owned dialog'])
        self.parents_read.assert_called_once()
        self.assertTrue(rows[0]['current'])
        self.assertTrue(rows[1]['minimized'])
        self.assertNotIn(mock.call(4), self.captions.call_args_list)
        self.assertIn('Private title', [r['title'] for r in self.choices(all_windows=True)])
        self.assertTrue(self.trail.connect_choice(rows[1]))
        self.assertEqual(self.trail.scope, dict(mode='existing', exe='Device.exe',title='First device'))
        self.assertEqual(self.minimized, {2})
        self.assertEqual(self.choices()[0]['hwnd'],2)

    def test_recycled_ancestor_pid_cannot_disclose_window_in_related_list(self):
        self.identities[170]=('Reused.exe','40')
        self.assertNotIn(2,[r['hwnd'] for r in self.choices()])
        self.assertNotIn(mock.call(2),self.captions.call_args_list)

    def test_closed_renamed_recycled_and_duplicate_choices_leave_original_window_active(self):
        chosen=next(r for r in self.choices() if r['hwnd']==3)
        original=self.rows[3]
        for other in (None, (300,'Device.exe','Different','31'), (300,'Device.exe','Second device','34')):
            with self.subTest(row=other):
                if other is None:
                    self.rows.pop(3)
                else:
                    self.rows[3]=other
                self.assertFalse(self.trail.connect_choice(chosen))
                self.assertIn('변경',self.trail.active_hint)
        self.rows[3]=original
        self.rows[6]=(600,'Device.exe','Second device','33')
        self.identities[600]=('Device.exe','33')
        self.assertFalse(self.trail.connect_choice(chosen))
        self.assertIn('같은 창',self.trail.active_hint)
        self.assertIs(self.trail.active,self.trail.primary)
        # The duplicate is in another unrelated process, so only explicit all-app listing includes it.
        self.assertEqual(len([r for r in self.choices(True) if r['ambiguous']]), 2)

    def test_separately_opened_app_is_a_prerequisite_despite_prior_click(self):
        chosen=next(r for r in self.choices(True) if r['hwnd']==4)
        self.trail.baseline.remove((4,400))
        clicked(self.trail.primary,'unrelated-click',1.)
        self.trail.appeared[(4,400)]=2.
        self.assertTrue(self.trail.connect_choice(chosen))
        self.assertEqual(self.trail.scope,dict(exe='Editor.exe',title='Private title',mode='existing'))

    def test_related_new_window_keeps_the_demonstrated_opener(self):
        self.trail.baseline.remove((2,200))
        clicked(self.trail.primary,'open-device',1.)
        self.trail.appeared[(2,200)]=2.
        chosen=next(r for r in self.choices() if r['hwnd']==2)
        self.assertTrue(self.trail.connect_choice(chosen))
        self.assertEqual(self.trail.scope,dict(exe='Device.exe',title='First device',after_id='open-device',related=True))
