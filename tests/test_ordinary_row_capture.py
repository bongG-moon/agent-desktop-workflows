"""Ordinary painted-row clicks need an identity, a button and a settled effect."""
from unittest import TestCase, mock
from types import SimpleNamespace
from queue import Queue

from test_row_vision import frame, vision
import row_capture as rows
import teach_capture
import learned_tasks


def sample(states, at):
    shot = frame(states)
    return rows.Frame(shot[0],shot[1],[vision._rgb(shot,r) for r,_ in shot[1]],at,at+.05)


class OrdinaryRowTests(TestCase):
    def setUp(self):
        self.base = [('a','stopped','start'),('b','stopped','start')]
        self.before = sample(self.base,10)
        self.press = rows.prepare(self.before,[157,205],10.1)

    def test_distinct_row_identity_survives_reorder_and_validates_existing_replay_contract(self):
        changed = [('a','running','stop'),self.base[1]]
        spec = rows.infer(self.press,sample(changed,11))
        self.assertIsNotNone(spec)
        with mock.patch.object(vision,'_snapshot',return_value=frame(list(reversed(changed)))):
            observed = vision.observe_row(None,spec)
        self.assertEqual(observed['kind'],'after')
        self.assertEqual(observed['row_bounds'][1],215)
        self.assertIsNone(rows.infer(self.press,sample(list(reversed(changed)),11)))

    def test_duplicate_rows_unchanged_state_and_post_event_baseline_do_not_teach_an_action(self):
        self.assertIsNone(rows.prepare(sample([self.base[0]]*2,10),[157,205],10.1))
        self.assertIsNone(rows.prepare(self.before,[157,205],10.01))
        self.assertIsNone(rows.infer(self.press,sample([('a','stopped','stop'),self.base[1]],11)))
        self.assertIsNone(rows.infer(self.press,sample([self.base[0],('b','running','stop')],11)))

    def test_two_quick_clicks_settle_independently_in_order_and_save_without_private_frames(self):
        second = rows.prepare(self.before,[157,220],10.15)
        model = teach_capture.CaptureNotes()
        tracker = rows.Tracker(None,model)
        for key,press in (('first',self.press),('second',second)):
            model.row_step(key,press.number,None)
            tracker.follow(key,press)
        changed = [('a','running','stop'),('b','running','stop')]
        tracker.observe(sample(changed,11))
        self.assertTrue(all(s['action']=='manual' for s in model.snapshot()))
        tracker.observe(sample(changed,12))
        task = learned_tasks.from_recording('Rows','Example.exe','Example',model.snapshot())
        self.assertEqual([s['label'] for s in task['steps']],['1번째 행 버튼 누르기','2번째 행 버튼 누르기'])
        self.assertTrue(all(s['action']=='row-action' for s in task['steps']))
        for step in task['steps']:
            vision.validate_spec(step['row'])
        tracker.observe(sample([('a','loading','stop'),changed[1]],13))
        self.assertEqual([s['action'] for s in model.snapshot()],['manual','row-action'])

    def test_amended_double_click_never_gets_overwritten_by_late_sampling(self):
        model = teach_capture.CaptureNotes()
        model.row_step('click',1,None)
        model.add(teach_capture.manual('double_click_needs_review','click'))
        model.row_step('click',1,{'unexpected':'ignored'})
        self.assertEqual(model.snapshot()[0]['recognition']['reason'],'double_click_needs_review')

    def test_scroll_invalidates_an_inflight_sample_and_same_row_click_closes_observation(self):
        tracker = rows.Tracker(None, teach_capture.CaptureNotes())
        tracker.follow('first', self.press)
        tracker.observe(self.before)
        tracker.invalidate(10.04)
        tracker.observe(self.before)
        self.assertIsNone(tracker.before(10.1))
        self.assertFalse(tracker.pending)
        tracker.follow('next', self.press)
        tracker.next_press([157, 220])
        self.assertIn('next', tracker.pending)
        tracker.next_press([157, 205])
        self.assertFalse(tracker.pending)

    def test_hook_pins_the_pre_click_frame_behind_a_slow_worker(self):
        tracker = rows.Tracker(None, teach_capture.CaptureNotes())
        pump = teach_capture._RowHookPump(SimpleNamespace(hwnd=15), None, Queue(), Queue())
        pump.row_tracker = tracker
        tracker.observe(self.before)
        click = dict(kind='down', token=9, scope_proven=True, window=15, action='click', at=10.1)
        pump._emit(click)
        changed = [('a', 'running', 'stop'), self.base[1]]
        for moment in (11, 12, 13):
            tracker.observe(sample(changed, moment))
        self.assertIsNone(tracker.before(click['at']))
        self.assertIs(tracker.before(click['at'], click['token']), self.before)
        tracker.invalidate(14)
        self.assertIsNone(tracker.before(click['at'], click['token']))

    def test_transient_empty_first_sample_is_retried_before_reporting_readiness(self):
        tracker = rows.Tracker(None, teach_capture.CaptureNotes())
        with mock.patch.object(rows, 'sample', side_effect=[None, self.before]) as capture:
            tracker.start()
            try:
                self.assertTrue(tracker.initial_sample.wait(1))
                self.assertIs(tracker.before(10.1), self.before)
                self.assertEqual(capture.call_count, 2)
            finally:
                tracker.stop()
                tracker.join(1)

    def test_normal_recorder_promotes_duplicated_named_listitem_even_with_generic_click_action(self):
        model = teach_capture.CaptureNotes()
        binding = SimpleNamespace(hwnd=15, pid=7)
        worker = teach_capture._Worker(binding, None, model, None, None)
        worker.row_tracker = rows.Tracker(binding, model)
        worker.row_tracker.observe(self.before)
        worker.uia = mock.Mock()
        worker.uia.within.return_value = True
        worker.uia.secure.return_value = False
        worker.uia.pid.return_value = binding.pid
        worker.uia.bounds.return_value = dict(zip(('x', 'y', 'width', 'height'), self.press.rect))
        element = mock.Mock()
        element.property.side_effect = {30010: True, 30003: 50007}.__getitem__
        observation = dict(window=15, element=element, root=mock.Mock(), combo=None, node=dict(
            role='listitem', enabled=True, secure=False, locator={'role': 'listitem', 'name': 'Devices'},
            available_actions=['Click', 'Select', 'SetFocus']))
        event = dict(window=15, kind='down', action='click', at=10.1, x=157, y=205, scope_proven=True)
        with mock.patch.object(teach_capture.recorder.CaptureWorker, 'observe_down', return_value=observation):
            resolved = worker.observe_down(event)
            worker.capture_action(event, resolved)
        changed = [('a', 'running', 'stop'), self.base[1]]
        for at in (11, 12):
            worker.row_tracker.observe(sample(changed, at))
        self.assertEqual(model.snapshot()[0]['action'], 'row-action')

        # No pre-click pixels may be inferred from the already changed screen.
        # The live row layout still supplies a helpful, editable ordinal draft.
        worker.row_tracker = rows.Tracker(binding, model)
        with (mock.patch.object(teach_capture.recorder.CaptureWorker, 'observe_down', return_value=observation),
              mock.patch.object(vision, '_row_layout', return_value=(self.before.window, self.before.rows))):
            resolved = worker.observe_down(event)
            self.assertEqual(resolved['painted_row'], (1, None))
            worker.capture_action(event, resolved)
        step = model.snapshot()[1]
        self.assertEqual(step['action'], 'manual')
        self.assertEqual(teach_capture.default_text(step), '1번째 행 버튼을 눌러줘')
