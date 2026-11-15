"""The witnessed left click keeps a local visual target and the user's original description."""
import base64
from pathlib import Path
import queue
import sys
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import desktop_select  # Load the pinned recorder and bridge from the source checkout.
import learned_tasks as tasks
import teach_capture as capture
from capture_trail import finalize_contexts
from visual_target import HoverTracker, SIDE


class VisualRecordingTests(TestCase):
    def setUp(self):
        self.proof = dict(size=[SIDE,SIDE], offset=[20,20],
                          rgb=base64.b64encode(bytes(i%211 for i in range(SIDE*SIDE*3))).decode())
        self.notes = capture.CaptureNotes()
        self.binding = mock.Mock(hwnd=501, pid=102)
        self.worker = capture._Worker(self.binding, mock.Mock(), self.notes, queue.Queue(), queue.Queue())
        self.worker.uia = mock.Mock()
        self.worker.uia.secure.return_value=False
        self.worker.uia.combo_observation.return_value=None
        self.worker.visual_tracker = mock.Mock()
        self.worker.visual_tracker.before.return_value = self.proof
        self.observed = dict(window=501, element=mock.Mock(), root=mock.Mock(), combo=None,
                             node=dict(enabled=True, secure=False, role='group', available_actions=[]))
        self.event = dict(kind='down', token=42, window=501, hit_window=600,
                          scope_proven=True, at=2., x=208, y=116, action='click')

    def test_first_visual_click_keeps_image_prose_and_linked_window_through_review(self):
        with mock.patch.object(capture.recorder.CaptureWorker,'observe_down',return_value=self.observed):
            observed = self.worker.observe_down(self.event)
        self.worker.capture_action(self.event, observed)
        self.worker.visual_tracker.before.assert_called_once_with(self.event)
        row, = self.notes.snapshot()
        self.assertEqual(row['action'], 'visual-click')
        self.assertNotIn('x', row)
        self.assertEqual(row['visual'], self.proof)
        scope = dict(exe='Viewer.exe',title='Test device',mode='existing')
        row['_capture_window'] = scope
        edits = capture.StepEdits()
        sentence = '포켓 게임 버튼 눌러서 시행 (pocket)'
        edits.store(row,'',sentence)
        final, = finalize_contexts(edits.apply([row]))
        saved = tasks.recorded_step(final)
        self.assertEqual((saved['action'],saved['label'],saved['visual'],saved['window_context']),
                         ('visual-click',sentence,self.proof,scope))

    def test_named_buttons_protected_fields_and_foreign_popups_never_take_visual_fallback(self):
        button = dict(enabled=True, secure=False, role='button', locator=dict(role='button',name='Go'),
                      available_actions=['Click'])
        for node in (button,dict(button,secure=True),dict(button,role='textfield',available_actions=[]),
                     dict(button,role='group',available_actions=['SetValue']),dict(button,enabled=False)):
            self.assertFalse(self.worker._use_visual(self.event,dict(self.observed,node=node)))
        self.assertFalse(self.worker._use_visual(self.event,dict(self.observed,window=303)))
        self.assertFalse(self.worker._use_visual(dict(self.event,window=303),None))
        self.assertFalse(self.worker._use_visual(dict(self.event,scope_proven=False),None))
        self.assertFalse(self.worker._use_visual(dict(self.event,action='right-click'),None))

    def test_disappearing_peer_keeps_only_scope_checked_evidence_from_before_click(self):
        with mock.patch.object(capture.recorder.CaptureWorker, 'observe_down',
                               side_effect=capture.bridge.BridgeError('element_unavailable')):
            observed = self.worker.observe_down(self.event)
        self.assertIsNone(observed)
        self.worker.capture_action(self.event, observed)
        step, = self.notes.snapshot()
        self.assertEqual(step['visual'], self.proof)

    def test_native_embedded_foreign_renderer_uses_only_matched_preclick_without_semantic_access(self):
        native_hit = 600
        bound = capture._BoundWindow(self.binding, 'Device')
        self.binding.contains_window.return_value = False
        self.binding.recording_candidate.return_value = False
        hook = capture._HookWindows(bound)
        tracker = HoverTracker(self.binding)
        tracker.latest = (1.9,1.95,native_hit,[188,96,41,41],(208,116),self.proof)
        pump = capture._RowHookPump(hook,None,queue.Queue(),queue.Queue())
        pump.visual_tracker = self.worker.visual_tracker = tracker
        pump.row_tracker = mock.Mock()
        with (mock.patch('visual_target.native_child',side_effect=lambda _,hwnd:hwnd==native_hit),
              mock.patch('visual_target._safe_patch',side_effect=[native_hit,601,capture.bridge.BridgeError('protected')]) as check_point,
              mock.patch.object(capture.bridge,'hwnd_pid',return_value=920)):
            self.assertFalse(bound.recording_candidate(native_hit))
            self.assertFalse(hook.contains_window(native_hit))
            self.assertTrue(hook.recording_candidate(native_hit))
            self.assertFalse(hook.recording_candidate(601))
            for token, when in ((42,2.),(43,4.),(44,6.),(45,8.)):
                event = dict(self.event,token=token,at=when,scope_proven=False)
                pump._emit(event)
                down = pump.events.get_nowait()
                self.assertTrue(down['visual_embedded'])
                self.worker.handle(down)
                self.worker.handle(dict(self.event,kind='up',inside=True,token=token,at=when+.1))
                self.assertFalse(event['scope_proven'])
        pump.row_tracker.pin_down.assert_not_called()
        self.worker.uia.from_point.assert_not_called()
        first, rapid, changed, protected = self.notes.snapshot()
        self.assertEqual(first['action'],'visual-click')
        self.assertEqual(rapid['recognition']['reason'],'capture_target_unavailable')
        self.assertNotIn('visual',rapid)
        self.assertEqual(changed['recognition']['reason'],'popup_scope_unproven')
        self.assertEqual(protected['recognition']['reason'],'popup_scope_unproven')
        self.assertEqual(check_point.call_count,3)
        check_point.assert_called_with(self.binding,self.worker.uia,[208,116,1,1],(208,116),exposed=True)

    def test_canvas_proxy_reporting_an_edit_pattern_does_not_shadow_a_confirmed_visual_click(self):
        event = dict(self.event,visual=self.proof)
        for enabled in (True,False):
            node = dict(role='group',enabled=enabled,secure=False,available_actions=['SetValue'])
            self.assertTrue(self.worker._use_visual(event,dict(self.observed,node=node)))
            self.assertFalse(self.worker._use_visual(event,dict(self.observed,node=dict(node,role='textfield'))))

    def test_hook_only_pins_proven_left_down_and_invalidates_previous_hover_on_scroll(self):
        pump = capture._RowHookPump(self.binding,None,queue.Queue(),queue.Queue())
        pump.row_tracker = mock.Mock()
        pump.visual_tracker = mock.Mock()
        pump._emit(dict(self.event,scope_proven=False))
        pump._emit(dict(self.event,window=303))
        pump._emit(dict(self.event,action='right-click'))
        pump.visual_tracker.pin_down.assert_not_called()
        pump._emit(self.event)
        pump.visual_tracker.pin_down.assert_called_once_with(self.event)
        pump._emit(dict(kind='scroll',at=3.5))
        pump.visual_tracker.invalidate.assert_called_with(3.5)

    def test_no_sample_does_not_invent_visual_identity_from_after_screen(self):
        self.worker.visual_tracker.before.return_value=None
        with mock.patch.object(capture.recorder.CaptureWorker,'observe_down',return_value=self.observed):
            observed=self.worker.observe_down(self.event)
        self.worker.capture_action(self.event,observed)
        step,=self.notes.snapshot()
        self.assertEqual(step['action'],'manual')
        self.assertNotIn('visual',step)
