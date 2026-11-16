"""Window linking remains optional and follows the demonstrator's event order."""
from pathlib import Path
import sys
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'skills/desktop-select/scripts'))
import desktop_select
import teach_capture as teach
import capture_trail as trail


class Binding:
    def __init__(self, hwnd, pid, title):
        self.hwnd, self.pid, self.title = hwnd, pid, title
        self.exe = f'App{pid}.exe'
        self.other_roots = frozenset()

    def check(self):
        pass


class Part:
    def __init__(self, binding, title):
        self.binding = binding
        self.notes = teach.CaptureNotes()
        self.started = self.ended = False
        self.pump = mock.Mock()

    def start(self, **kwargs):
        self.started = True

    def poll(self):
        pass

    def cancel(self):
        self.ended = True

    def finish(self, **kwargs):
        self.ended = True


def clicked(part, name, when):
    with part.notes.lock:
        part.notes.steps[name] = dict(id=name, action='click', label=name)
        part.notes.times[name] = when


class WindowRecordingTests(TestCase):
    def setUp(self):
        self.parent = Binding(1, 100, 'Manager')
        self.child = Binding(2, 200, 'Device one')
        self.separate = Binding(3, 300, 'Preexisting editor')
        self.foreground = (1, 100)
        self.keys = {(1, 100), (3, 300)}
        self.now = 1.
        self.pool = {(2, 200): self.child, (3, 300): self.separate}
        def candidate(key, parent, title, *, automatic, baseline):
            if key not in self.pool or automatic and (key in baseline or parent is not self.parent):
                return None
            return self.pool[key], self.pool[key].title
        for name, side_effect in (
                ('visible_window_keys', lambda: set(self.keys)),
                ('foreground_key', lambda: self.foreground),
                ('candidate', candidate)):
            patch = mock.patch.object(trail.windows, name, side_effect=side_effect)
            patch.start()
            self.addCleanup(patch.stop)
        clock = mock.patch.object(trail.time, 'monotonic', side_effect=lambda:self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.session = trail.CaptureTrail(self.parent,self.parent.title,Part)
        self.session.start()
        self.addCleanup(self.session.cancel)

    def test_auto_link_requires_new_window_and_real_prior_step(self):
        self.keys.add((2,200))
        self.foreground = (2,200)
        self.now = 2.
        self.session.poll()
        self.assertIs(self.session.active,self.session.primary)
        clicked(self.session.primary,'later',3.)
        self.now = 4.
        self.session.poll()
        self.assertIs(self.session.active,self.session.primary)

    def test_menu_dialog_uses_the_final_command_instead_of_earlier_fields(self):
        notes = self.session.primary.notes
        for key, action, at, reason in [('field', 'set-value', 1.1, None),
                                       ('context', 'manual', 1.2, 'right_click_requires_menu_teaching'),
                                       ('export', 'manual', 1.3, 'popup_scope_unproven')]:
            notes.steps[key] = dict(id=key, action=action,
                                   recognition=dict(reason=reason) if reason else {})
            notes.times[key] = at
        self.assertEqual([r[2] for r in self.session._openers(1.4)], ['export'])
        notes.steps['export'] = dict(id='export', action='result-menu')
        self.assertEqual([r[2] for r in self.session._openers(1.4)], ['export'])

    def test_independent_launch_buttons_keep_their_existing_ambiguity(self):
        clicked(self.session.primary, 'launch-a', 1.1)
        clicked(self.session.primary, 'launch-b', 1.2)
        self.assertEqual([r[2] for r in self.session._openers(1.4)], ['launch-a', 'launch-b'])

    def test_opener_and_return_order_survive_delayed_capture_and_review_edits(self):
        clicked(self.session.primary,'unrelated',-100.)
        clicked(self.session.primary,'launch',1.2)
        self.keys.add((2,200))
        self.foreground = (2,200)
        self.now = 2.
        self.session.poll()
        self.assertEqual(len(self.session.parts),2)
        self.assertTrue(self.session.primary.started and not self.session.primary.ended)
        # The recorder may consume two independent queues out of chronological order.
        clicked(self.session.primary,'back',3.6)
        clicked(self.session.active,'device-button',3.)
        self.foreground = (1,100)
        self.session.poll()
        self.assertIs(self.session.active,self.session.primary)
        raw = self.session.finish()['steps']
        self.assertEqual([s['id'] for s in raw], ['unrelated','launch','device-button','back'])
        saved = teach.finalize_contexts(raw[1:])
        self.assertEqual(saved[1]['window_context'],dict(exe=self.child.exe,title=self.child.title,after_step=1,related=True))
        self.assertNotIn('window_context',saved[2])
        with self.assertRaisesRegex(ValueError, '연 동작'):
            teach.finalize_contexts(raw[2:])

    def test_preexisting_requires_explicit_link_and_keeps_independent_scope(self):
        self.foreground = (3,300)
        clicked(self.session.primary,'launch',1.5)
        self.now = 2.
        self.session.poll()
        self.assertEqual(len(self.session.parts),1)
        self.assertTrue(self.session.connect(self.foreground))
        clicked(self.session.active,'preexisting',2.1)
        self.assertEqual(teach.finalize_contexts(self.session.snapshot())[1]['window_context'],
                         dict(exe=self.separate.exe,title=self.separate.title,mode='existing'))

    def test_rapid_launches_keep_capturing_and_defer_assignment_for_each_child(self):
        second = Binding(4,400,'Device two')
        self.pool[4,400] = second
        clicked(self.session.primary,'launch-first',1.1)
        clicked(self.session.primary,'launch-second',1.5)
        self.keys.add((2,200))
        self.foreground = (2,200)
        self.now = 2.
        self.session.poll()
        clicked(self.session.active,'first-action',2.1)
        # Even if the second device first appears in a later inventory poll,
        # the ordering of its arrival cannot prove which earlier launch opened it.
        self.keys.add((4,400))
        self.foreground = (4,400)
        self.now = 2.5
        self.session.poll()
        clicked(self.session.active,'second-action',2.6)
        raw = self.session.finish()['steps']
        for row in raw[2:]:
            scope = row['_capture_window']
            self.assertEqual(scope['_openers'], ('launch-first','launch-second'))
            with self.assertRaises(ValueError):
                teach.finalize_contexts(raw)
            scope['after_id'] = 'launch-first' if row['id'] == 'first-action' else 'launch-second'
            scope.pop('_openers')
        self.assertEqual([s['window_context']['after_step'] for s in teach.finalize_contexts(raw)[2:]], [1,2])

    def test_delayed_worker_does_not_bind_to_previous_launch(self):
        clicked(self.session.primary,'launch-first',1.1)
        self.session.primary.pump.last_scoped_down = 1.7
        self.keys.add((2,200))
        self.foreground = (2,200)
        self.now = 2.
        self.session.poll()
        self.assertEqual(len(self.session.parts), 1)
        clicked(self.session.primary,'launch-second',1.7)
        self.now = 2.25
        self.session.poll()
        self.assertEqual(self.session.scope['_openers'], ('launch-first','launch-second'))

    def test_sequential_launches_with_separate_completed_transitions_link_without_question(self):
        clicked(self.session.primary,'launch-first',1.5)
        self.foreground = (2,200)
        self.keys.add(self.foreground)
        self.now = 2.
        self.session.poll()
        clicked(self.session.active,'child-one',2.1)
        self.foreground = (1,100)
        self.now = 2.5
        self.session.poll()
        clicked(self.session.primary,'launch-second',3.)
        self.foreground = (4,400)
        self.pool[self.foreground] = Binding(4,400,'Device two')
        self.keys.add(self.foreground)
        self.now = 3.25
        self.session.poll()
        clicked(self.session.active,'child-two',3.5)
        rows = teach.finalize_contexts(self.session.finish()['steps'])
        self.assertEqual([row['window_context']['after_step'] for row in (rows[1], rows[3])], [1,3])

    def test_unrelated_window_and_early_click_are_not_replayed_as_an_action(self):
        clicked(self.session.primary,'launch',1.2)
        self.foreground = (8,800)
        self.keys.add(self.foreground)
        self.now = 2.
        self.session.poll()
        self.assertEqual(len(self.session.parts),1)
        self.foreground = (2,200)
        self.keys.add(self.foreground)
        self.session.primary.pump.outside_marker(self.foreground,2.8)
        self.now = 3.
        self.session.poll()
        steps = self.session.snapshot()
        self.assertEqual(len(steps),2)
        self.assertEqual(steps[-1]['action'],'manual')
        self.assertTrue(teach.learned_tasks.is_recoverable(steps[-1]))
