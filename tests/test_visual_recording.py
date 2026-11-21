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
from visual_target import HoverTracker, NOTEPAD_FIND_NEXT, SIDE, validate_spec


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

    def test_named_container_does_not_replace_its_unexposed_button_with_a_relative_point(self):
        for role in ('toolbar','pane','document'):
            notes = capture.CaptureNotes()
            self.worker.model = notes
            node = dict(self.observed['node'], role=role, locator=dict(role=role, name='Drawing area'),
                        available_actions=['SetFocus'])
            with self.subTest(role=role):
                self.worker.capture_action(dict(self.event,visual=self.proof), dict(self.observed,node=node))
                step, = notes.snapshot()
                self.assertEqual((step['action'],step['visual']), ('visual-click',self.proof))

    def test_pair_replaces_only_first_click_using_its_pre_press_sample(self):
        first = dict(self.proof, gesture='double-click')
        self.worker.visual_tracker.before.side_effect = [first, None]
        up = dict(self.event,kind='up',inside=True)
        with (mock.patch.object(capture.recorder.CaptureWorker, 'observe_down',return_value=None),
              mock.patch.object(capture.recorder.time,'sleep')):
            self.worker.handle(dict(self.event))
            self.worker.handle(up)
            original, = self.notes.snapshot()
            self.assertEqual(original['action'], 'manual')
            self.worker.handle(dict(self.event,token=43,previous_token=42,double=True,at=2.08))
            self.worker.handle(dict(up,token=43,at=2.1))
        bound, = self.notes.snapshot()
        self.assertEqual((bound['id'],bound['action'],bound['visual']), (original['id'],'visual-click',first))

    def test_second_press_drag_does_not_create_double_click(self):
        self.worker.visual_tracker.before.return_value = dict(self.proof,gesture='double-click')
        up = dict(self.event,kind='up',inside=True)
        with (mock.patch.object(capture.recorder.CaptureWorker, 'observe_down',return_value=None),
              mock.patch.object(capture.recorder.time,'sleep')):
            self.worker.handle(dict(self.event))
            self.worker.handle(up)
            self.worker.handle(dict(self.event,token=43,previous_token=42,double=True,at=2.08))
            self.worker.handle(dict(up,token=43,x=350,at=2.1))
        first,second = self.notes.snapshot()
        self.assertEqual([r['action'] for r in (first,second)], ['manual','manual'])
        self.assertEqual(second['recognition']['reason'], 'drag_needs_review')

    def test_gesture_prose_accepts_natural_open_phrasing_and_preserves_changed_intent_for_review(self):
        recorded = dict(action='visual-click',visual=dict(self.proof,gesture='double-click'),label='녹화한 항목 더블클릭')
        for text in ('이 행을 두 번 눌러 상세 창을 열어 줘','결과 행 더블클릭해서 상세 팝업을 띄워 줘',
                     '나온 테이블 결과를 더블 클릭해서 팝업 창을 띄워줘','이 항목을 두 번 눌러 주세요'):
            with self.subTest(text=text):
                self.assertEqual(tasks.compile_instruction(recorded,text)['mode'],'visual-click')
        for text in ('이 행을 한 번만 클릭해줘','더블 클릭하지 말고 이 행을 삭제해줘','열렸으면 이 행을 더블 클릭해줘',
                     '팝업을 닫고 이 행을 두 번 눌러줘','이 행을 두 번 눌러 상세를 삭제해줘'):
            with self.subTest(text=text):
                self.assertEqual(tasks.compile_instruction(recorded,text)['mode'],'draft')

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

    def test_notepad_find_next_keeps_observed_action_and_korean_instruction(self):
        self.binding.exe = 'Notepad.exe'
        self.worker.visual_tracker.before.return_value = None
        document_hit = dict(self.observed, node=dict(self.observed['node'], role='document'))
        with (mock.patch.object(capture.recorder.CaptureWorker, 'observe_down', return_value=document_hit),
              mock.patch('notepad_find.locate', return_value=dict(hit=600)) as locate,
              mock.patch('notepad_find.contains_click', return_value=True)):
            observed = self.worker.observe_down(self.event)
        locate.assert_called_once_with(self.binding, exposed=True)
        self.worker.capture_action(self.event, observed)
        row, = self.notes.snapshot()
        sentence = '메모장에서 다음 찾기 버튼을 눌러줘'
        edits = capture.StepEdits()
        edits.store(row, '', sentence)
        saved = tasks.recorded_step(edits.apply([row])[0])
        self.assertEqual((saved['action'], saved['label'], saved['visual']),
                         ('visual-click', sentence, NOTEPAD_FIND_NEXT))
        self.assertEqual(tasks.compile_instruction(row, '이전 찾기 버튼을 눌러줘')['mode'], 'draft')
        self.assertRaises(capture.bridge.BridgeError, validate_spec, dict(NOTEPAD_FIND_NEXT, gesture='double-click'))
