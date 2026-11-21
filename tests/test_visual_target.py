"""An unsupported painted target can be demonstrated, moved, and clicked only once."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import desktop_select
import visual_target as visual
import row_action


def image(*icons, delta=0, width=170, height=120):
    data = bytearray(bytes((12, 15, 11, 0))*width*height)
    for cx,cy in icons:
        for dy in range(-15,16):
            for dx in range(-15,16):
                if dx*dx+dy*dy > 225:
                    continue
                offset = ((cy+dy)*width+cx+dx)*4
                color = [30 + ((dx+15)*9 % 195), 20 + ((dy+15)*5 % 195),
                         15 + ((dy*dx+223) % 200)]
                data[offset:offset+3] = bytes(v+delta for v in color)
    return bytes(data)


class VisualTargets(unittest.TestCase):
    window = [200, 120, 170, 120]

    @staticmethod
    def toolbar(*buttons):
        w, h = 170, 120
        data = bytearray(bytes([241,241,241,0])*(w*h))
        for x, y, fill, flip in buttons:
            for dy in range(-21,22):
                for dx in range(-21,22):
                    stroke = abs(dx) <= 1 and abs(dy) <= 8 or abs(dx) <= 6 and abs(dy-(7-abs(dx))*(-1 if flip else 1)) <= 1
                    color = 57 if stroke else fill
                    i = 4*((y+dy)*w+x+dx)
                    data[i:i+3] = bytes([color]*3)
        return data

    def test_relocated_toolbar_icon_allows_hover_fill_but_not_another_glyph_or_second_match(self):
        spec = visual.from_frame(self.window, self.toolbar((35,42,213,False)), (235,162))
        self.assertIn('rgb', spec)
        wanted = (106,72,241,False)
        wrong = (35,42,213,True)
        self.assertEqual(visual._locate(self.window, self.toolbar(wrong, wanted), spec)[1], [306,192])
        for incorrect, error in (([wrong], 'not_found'), ([(35,42,213,False), wanted], 'ambiguous'),
                                 ([(106,72,120,False)], 'not_found')):
            with self.assertRaisesRegex(visual.bridge.BridgeError, error):
                visual._locate(self.window, self.toolbar(*incorrect), spec)

    def test_pointer_relocation_wakes_sampler_after_settling_without_ui_lookups(self):
        tracker = visual.HoverTracker(SimpleNamespace(hwnd=2))
        tracker.stopping = mock.Mock()
        tracker.stopping.is_set.return_value = tracker.stopping.wait.return_value = False
        with mock.patch.object(visual.bridge,'cursor',side_effect=[(4,4),(10,10),(10,10)]):
            tracker._wait_for_hover((1,1), .35)
        self.assertEqual(tracker.stopping.wait.call_count,3)
        self.assertTrue(all(call.args == (.03,) for call in tracker.stopping.wait.call_args_list))

    def test_moved_icon_matches_at_its_new_window_position_with_small_color_drift(self):
        spec = visual.from_frame(self.window, image((35,42)), (236,162))
        self.assertEqual(set(spec), {'size','offset','rgb'})
        shifted = [400, 180, 170, 120]
        self.assertEqual(visual._locate(shifted, image((102,70),delta=4), spec)[1], [503,250])

    def test_duplicate_missing_flat_and_corrupt_templates(self):
        spec = visual.from_frame(self.window, image((35,42)), (235,162))
        with self.assertRaisesRegex(visual.bridge.BridgeError, 'visual_target_ambiguous'):
            visual._locate(self.window, image((35,42),(113,70)), spec)
        with self.assertRaisesRegex(visual.bridge.BridgeError, 'visual_target_not_found'):
            visual._locate(self.window, image(), spec)
        with self.assertRaisesRegex(visual.bridge.BridgeError, 'lacks_detail'):
            visual.from_frame(self.window, image(), (235,162))
        with self.assertRaisesRegex(visual.bridge.BridgeError, 'invalid_visual_target'):
            visual.validate_spec(dict(spec, rgb='a' * 100000))

    def test_click_pins_only_contemporary_pre_down_crop_and_respects_other_input(self):
        tracker = visual.HoverTracker(SimpleNamespace(hwnd=2))
        spec = visual.from_frame(self.window, image((35,42)), (235,162))
        tracker.latest = (10.,10.1,3,[215,142,41,41],[235,162],spec)
        event = dict(kind='down', token=4, at=10.2, scope_proven=True, action='click',
                     window=2, hit_window=3, x=238, y=162)
        for changed in (dict(at=10.05), dict(at=11.6), dict(hit_window=99), dict(x=254),
                        dict(scope_proven=False), dict(action='right-click')):
            wrong = dict(event, **changed)
            tracker.pin_down(wrong)
            self.assertIsNone(tracker.before(wrong))
        tracker.pin_down(event)
        tracker.invalidate(10.3)
        self.assertEqual(tracker.before(event), dict(spec, offset=[23,20]))
        later = dict(event, token=5, at=10.4)
        tracker.pin_down(later)
        self.assertIsNone(tracker.before(later))

    def test_delivery_uses_fresh_surface_once_and_reports_no_unobserved_effect(self):
        spec = visual.from_frame(self.window,image((35,42)),(235,162))
        binding = SimpleNamespace(hwnd=1,pid=2)
        with ExitStack() as stack:
            def patch(module, name, **kwargs):
                return stack.enter_context(mock.patch.object(module,name,**kwargs))
            patch(visual.vision,'_window_rect',return_value=self.window)
            def capture(_, window, crop=None):
                full = image((35,42))
                if crop is None:
                    return full
                x, y = crop[0]-window[0], crop[1]-window[1]
                return b''.join(full[(i*window[2]+x)*4:(i*window[2]+x+crop[2])*4]
                                for i in range(y,y+crop[3]))
            patch(visual.vision,'_frame',side_effect=capture)
            patch(visual,'_safe_patch',return_value=23)
            patch(visual.bridge,'UIA')
            activate = patch(row_action,'foreground')
            patch(row_action,'_input_tick',return_value=10)
            patch(visual.bridge,'_combo_surface')
            patch(visual.bridge,'window_from_point',return_value=23)
            patch(visual.bridge,'root_window',return_value=1)
            patch(visual.bridge,'foreground',return_value=1)
            patch(visual.bridge,'hwnd_pid',return_value=2)
            send = patch(visual.bridge,'_send_point',return_value=dict(ok=True,input_dispatched=True))
            self.assertEqual(visual.execute(binding,spec,lambda:None,background_only=True)['status'],'blocked')
            self.assertEqual(visual.execute(binding,spec,lambda:None,read_only=True)['status'],'observed')
            activate.assert_not_called()
            send.assert_not_called()
            delivered = visual.execute(binding,spec,lambda:None)
            self.assertEqual(delivered['status'],'delivered_unverified')
            send.assert_called_once_with(235,162,'click')
            send.reset_mock()
            double = dict(spec, gesture='double-click')
            self.assertEqual(visual.execute(binding,double,lambda:None,background_only=True)['status'],'blocked')
            self.assertEqual(visual.execute(binding,double,lambda:None,read_only=True)['status'],'observed')
            send.assert_not_called()
            delivered = visual.execute(binding,double,lambda:None)
            self.assertEqual(delivered['diagnostic'],'visual_double_click_dispatched')
            self.assertEqual(delivered['status'],'delivered_unverified')
            send.assert_called_once_with(235,162,'double-click')
            send.reset_mock()
            send.return_value = dict(ok=False, input_dispatched=None, diagnostic='delivery_unknown')
            uncertain = visual.execute(binding,double,lambda:None,first_match_timeout=30)
            self.assertEqual(uncertain['status'],'uncertain')
            send.assert_called_once_with(235,162,'double-click')
            send.reset_mock()
            send.side_effect = ValueError('transport failed after dispatch began')
            uncertain = visual.execute(binding,spec,lambda:None)
            self.assertEqual(uncertain['status'],'uncertain')
            self.assertIsNone(uncertain['input_dispatched'])
            send.assert_called_once_with(235,162,'click')
            send.reset_mock(side_effect=True)
            visual.vision._frame.side_effect = [image((35,42)), image((35,42)), bytes(41*41*4)]
            self.assertFalse(visual.execute(binding,spec,lambda:None)['input_dispatched'])
            send.assert_not_called()

    def test_cold_start_polls_only_for_missing_icon_before_activation_or_delivery(self):
        binding = SimpleNamespace(hwnd=1, pid=2)
        target = dict(window=self.window, box=[214,142,41,41], point=[235,162])
        guard = mock.Mock()
        missing = visual.bridge.BridgeError('visual_target_not_found')
        ambiguous = visual.bridge.BridgeError('visual_target_ambiguous')
        with mock.patch.object(visual,'observe',side_effect=[missing, target, ambiguous]) as observe, \
             mock.patch.object(visual.time,'sleep') as wait, \
             mock.patch.object(row_action,'foreground') as activate, \
             mock.patch.object(row_action,'_input_tick'), mock.patch.object(visual.bridge,'_combo_surface'), \
             mock.patch.object(visual.bridge,'_send_point') as send:
            result = visual.execute(binding,{},guard,first_match_timeout=2)
            self.assertEqual(result['diagnostic'],'visual_target_ambiguous')
            self.assertEqual(observe.call_count, 3)
            # Two observation attempts, then guard the leased window before and after activation.
            self.assertEqual(guard.call_count, 4)
            activate.assert_called_once()
            wait.assert_called_once()
            send.assert_not_called()
        with mock.patch.object(visual,'observe',side_effect=missing) as observe, \
             mock.patch.object(visual.time,'monotonic',side_effect=[0, 31]), \
             mock.patch.object(row_action,'foreground') as activate:
            result = visual.execute(binding,{},guard,first_match_timeout=30)
            self.assertEqual(result['diagnostic'],'visual_target_not_found')
            observe.assert_called_once()
            activate.assert_not_called()

    def test_only_explicitly_unprotected_noneditable_embedded_peer_can_cross_process(self):
        binding = SimpleNamespace(hwnd=1,pid=2)
        props = {30003:50033,30020:30,30019:False}
        el = SimpleNamespace(property=props.get)
        uia = mock.Mock()
        uia.secure.return_value = False
        uia.pid.return_value = 42
        with (mock.patch.object(visual.bridge, 'root_window', return_value=1) as root,
              mock.patch.object(visual.bridge,'hwnd_pid',return_value=42) as pid,
              mock.patch.object(visual.bridge,'user32',create=True) as user):
            user.IsChild.return_value = user.IsWindowVisible.return_value = True
            self.assertTrue(visual.safe_provider(binding,uia,el))
            for key, value in ((30019,None),(30019,True),(30040,True),(30043,True),(30003,50004),(30020,None)):
                with mock.patch.dict(props,{key:value}):
                    self.assertFalse(visual.safe_provider(binding,uia,el))
            root.return_value = 3
            self.assertFalse(visual.safe_provider(binding,uia,el))
            root.return_value = 1
            user.IsChild.return_value = False
            self.assertFalse(visual.safe_provider(binding,uia,el))
            user.IsChild.return_value = True
            pid.return_value = 2
            self.assertFalse(visual.safe_provider(binding,uia,el))

    def test_row_double_click_patch_requires_noneditable_nonprotected_data(self):
        binding = SimpleNamespace(hwnd=1, pid=2, check=lambda: None)
        props = {30003:50029, 30010:True, 30040:True, 30043:True, 30046:True}
        row = SimpleNamespace(property=props.get)
        uia = mock.Mock()
        uia.from_point.return_value = uia.from_handle.return_value = row
        uia.secure.return_value = False
        uia.within.return_value = True
        uia.pid.return_value = 2
        uia.children.return_value = []
        def client(_hwnd, rect):
            rect._obj.right = rect._obj.bottom = 150
            return True
        def native(_user, name, *_):
            return client if name == 'GetClientRect' else lambda *args: True
        with (mock.patch.object(visual.bridge, '_sig', side_effect=native),
              mock.patch.object(visual.bridge, 'window_from_point', return_value=1),
              mock.patch.object(visual.bridge, 'foreground', return_value=1),
              mock.patch.object(visual.bridge, 'root_window', return_value=1),
              mock.patch.object(visual.bridge, 'hwnd_pid', return_value=2),
              mock.patch.object(visual.vision, '_bounds', return_value=[0,0,140,140])):
            args = (binding, uia, [10,10,41,41], (30,30))
            marked = set()
            self.assertEqual(visual._safe_patch(*args, exposed=True, gesture='double-click', data_surface=marked), 1)
            self.assertEqual(marked, {50029})
            with self.assertRaisesRegex(visual.bridge.BridgeError, 'visual_hit_not_actionable'):
                visual._safe_patch(*args, exposed=True)
            for change in ({30046:False}, {30046:None}, {30149:True}, {30003:50004}):
                with mock.patch.dict(props, change), self.assertRaises(visual.bridge.BridgeError):
                    visual._safe_patch(*args, exposed=True, gesture='double-click')
            uia.secure.return_value = True
            with self.assertRaises(visual.bridge.BridgeError):
                visual._safe_patch(*args, exposed=True, gesture='double-click')

    def test_one_click_on_toolbar_and_explicitly_read_only_document_rechecks_protection(self):
        binding = SimpleNamespace(hwnd=1,pid=2,check=lambda:None)
        props = {30003:50021,30010:True}
        el = SimpleNamespace(property=props.get)
        uia = mock.Mock()
        uia.from_point.return_value = uia.from_handle.return_value = el
        uia.pid.return_value, uia.within.return_value, uia.secure.return_value = 2, True, False
        uia.children.return_value = []
        def client(_hwnd, rect):
            rect._obj.right = rect._obj.bottom = 150
            return True
        with (mock.patch.object(visual.bridge, '_sig', side_effect=lambda u,n,*args: client if n=='GetClientRect' else lambda *args:True),
              mock.patch.object(visual.bridge,'root_window',return_value=1),
              mock.patch.object(visual.bridge,'foreground',return_value=1),
              mock.patch.object(visual.bridge,'hwnd_pid',return_value=2),
              mock.patch.object(visual.bridge,'window_from_point',return_value=1),
              mock.patch.object(visual.vision,'_bounds',return_value=[0,0,140,140])):
            args = (binding,uia,[10,10,41,41],(30,30))
            self.assertEqual(visual._safe_patch(*args,exposed=True),1)
            props.update({30003:50030,30040:True,30043:True,30046:True})
            self.assertEqual(visual._safe_patch(*args,exposed=True),1)
            for invalid in ({30046:False},{30046:None},{30043:False},{30149:True}):
                with mock.patch.dict(props,invalid), self.assertRaises(visual.bridge.BridgeError):
                    visual._safe_patch(*args,exposed=True)

    def test_disabled_renderer_must_have_the_actual_enabled_input_parent(self):
        binding = SimpleNamespace(hwnd=1,pid=2)
        image_props = {30003:50033,30020:31,30010:False,30019:False}
        receiver_props = {30003:50033,30020:21,30010:True,30019:False}
        painted = SimpleNamespace(property=image_props.get)
        receiver = SimpleNamespace(property=receiver_props.get)
        uia = mock.Mock()
        uia.pid.side_effect = lambda e: 42 if e is painted else 2
        uia.from_handle.return_value = receiver
        uia.within.return_value = True
        uia.secure.return_value = False
        with (mock.patch.object(visual.bridge, 'root_window',return_value=1),
              mock.patch.object(visual.bridge, 'hwnd_pid',side_effect={21:2,31:42}.get),
              mock.patch.object(visual.bridge,'user32',create=True) as user):
            valid_children = {(1,31),(1,21),(21,31)}
            user.IsChild.side_effect = lambda a,b:(a,b) in valid_children
            user.IsWindowEnabled.side_effect = lambda h:h in (1,21)
            user.IsWindowVisible.return_value = True
            self.assertTrue(visual._enabled_canvas(binding,uia,painted,21))
            with mock.patch.dict(image_props,{30003:50000}):
                self.assertFalse(visual._enabled_canvas(binding,uia,painted,21))
            with mock.patch.dict(receiver_props,{30010:False}):
                self.assertFalse(visual._enabled_canvas(binding,uia,painted,21))
            valid_children.remove((21,31))
            self.assertFalse(visual._enabled_canvas(binding,uia,painted,21))
            valid_children.add((21,31))
            uia.within.return_value = False
            self.assertFalse(visual._enabled_canvas(binding,uia,painted,21))
