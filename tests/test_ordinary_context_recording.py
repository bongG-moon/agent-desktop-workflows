"""Ordinary mouse gestures use the same scoped result-menu recipe as teaching."""
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as cli
import context_menu
import menu_capture
import teach_capture as capture


class Element:
    def __init__(self, parent=None, branch=False):
        self.parent = parent
        self.branch = branch

    def property(self, key):
        return self.branch if key == 30028 else None


class Tree:
    same = staticmethod(lambda a, b: a is b)

    @staticmethod
    def within(item, ancestor):
        while item is not None and item is not ancestor:
            item = item.parent
        return item is not None


class OrdinaryContext(unittest.TestCase):
    anchor = {'target': {'automation_id': 'ResultGrid', 'role': 'group'}, 'point': [.25, .4]}
    event = {'kind': 'down', 'action': 'right-click', 'x': 100, 'y': 200, 'window': 1,
             'scope_proven': True, 'hit_window': 2, 'at': 10}

    def setup_worker(self):
        worker = object.__new__(capture._Worker)
        worker.binding = Mock(hwnd=1, pid=7)
        worker.binding.info.return_value = {'dpi': 96}
        worker.uia = Tree()
        worker.freeze_watch = Mock()
        worker.model = capture.CaptureNotes()
        worker.capture_action(dict(self.event, menu_anchor=self.anchor), None)
        return worker

    def setUp(self):
        self.root = context_menu._Item(20, Element(branch=True), Element(), (101, 200, 100, 60),
                                       (101, 220, 100, 20), 'Export to excel')
        self.leaf = context_menu._Item(30, Element(), Element(), (201, 220, 100, 60),
                                       (201, 220, 100, 20), 'Active sheet')
        self.hit = self.root.surface
        for name, fn in [('win.window_from_point', lambda *coords: self.hit),
                         ('_hit_item', lambda *args: self.hit)]:
            target, attr = (context_menu.win, name.split('.')[1]) if '.' in name else (context_menu, name)
            edit = patch.object(target, attr, side_effect=fn)
            edit.start()
            self.addCleanup(edit.stop)

    def test_ordinary_right_click_hover_submenu_and_leaf_save_one_step(self):
        worker = self.setup_worker()
        route = worker.menu_gesture
        route.sample(worker.binding, worker.uia, [self.root], self.root.center, self.hit)
        # A repeated hover after the child appears keeps the first opener evidence.
        items = [self.root, self.leaf]
        route.sample(worker.binding, worker.uia, items, self.root.center, self.hit)
        self.hit = self.leaf.surface
        picked = route.sample(worker.binding, worker.uia, items, self.leaf.center, self.hit)
        self.assertEqual(picked.path, ('Export to excel', 'Active sheet'))
        at = time.monotonic()
        # The click closed the menu before the worker serviced mouse-down.
        route.history.append((at + .01, None))
        with patch.object(context_menu, '_menus', return_value=[]):
            point = self.leaf.center
            down = dict(kind='down', action='click', x=point[0], y=point[1], at=at,
                        window=self.hit, hit_window=self.hit, scope_proven=False)
            self.assertIsNone(worker.observe_down(down))
            worker.capture_action(down, None)
        task = cli.tasks.from_recording('Export', 'Sample.exe', 'Example', worker.model.snapshot())
        self.assertEqual([s['action'] for s in task['steps']], ['result-menu'])
        context = task['steps'][0]['context']
        self.assertEqual(context, {'trigger': None, 'ready': None, 'output': None,
                                   'menu': dict(self.anchor, path=['Export to excel', 'Active sheet'])})
        self.assertIsNone(worker.menu_gesture)

    def test_logical_parent_without_hover_is_used_not_a_standalone_leaf(self):
        worker = self.setup_worker()
        self.leaf.element.parent = self.root.element
        self.hit = self.leaf.surface
        picked = worker.menu_gesture.sample(worker.binding, worker.uia, [self.root, self.leaf],
                                            self.leaf.center, self.hit)
        self.assertEqual(picked.path, ('Export to excel', 'Active sheet'))

    def test_unrelated_menu_or_two_hits_are_not_an_action(self):
        worker = self.setup_worker()
        route = worker.menu_gesture
        self.hit = self.leaf.surface
        self.assertIsNone(route.sample(worker.binding, worker.uia, [self.leaf], self.leaf.center, self.hit))
        self.assertIsNone(route.sample(worker.binding, worker.uia, [self.root]*2, self.root.center, self.hit))
        with patch.object(context_menu, '_menus', return_value=[]):
            event = dict(self.event, at=time.monotonic(), x=102, y=220, hit_window=99)
            self.assertIsNone(route.at_down(worker.binding, worker.uia, event))
        step = worker.model.snapshot()[0]
        self.assertEqual(cli.tasks.recorded_step(step)['menu_anchor'], self.anchor)
        noted = cli.tasks.recorded_step(dict(step, instruction={'mode':'draft', 'text':'표에서 필요한 시트를 내보내줘'}))
        self.assertEqual(noted['menu_anchor'], self.anchor)
        self.assertEqual(noted['action'], 'manual')
        with self.assertRaisesRegex(cli.tasks.TaskError, 'invalid_context_anchor'):
            cli.tasks.recorded_step(dict(capture.manual('protected_input_not_recorded'), menu_anchor=self.anchor))

    def test_parent_click_then_cancel_cannot_finalize(self):
        worker = self.setup_worker()
        route = worker.menu_gesture
        picked = route.sample(worker.binding, worker.uia, [self.root], self.root.center, self.hit)
        with patch.object(context_menu, '_menus', return_value=[]):
            worker.capture_action({'action': 'click', 'menu_pick': picked}, None)
        self.assertEqual(worker.model.snapshot()[0]['action'], 'manual')

    def test_editing_an_inferred_menu_keeps_the_text_and_surface_for_live_resolution(self):
        worker = self.setup_worker()
        pending, = worker.model.snapshot()
        edits = capture.StepEdits()
        text = '결과 표에서 상세 시트만 내보내줘'
        # Store even while the menu gesture is waiting for its last click.
        edits.drafts[pending['id']] = (edits.identity(pending), ('', text, None, ''))
        route = worker.menu_gesture
        route.clicked = route.sample(worker.binding, worker.uia, [self.root], self.root.center, self.hit)
        completed = route.step()
        self.assertEqual(edits.form(completed)[0], text)
        saved = cli.tasks.recorded_step(edits.apply([completed])[0])
        self.assertEqual(saved['action'], 'manual')
        self.assertEqual(saved['instruction_draft'], text)
        self.assertEqual(saved['menu_anchor'], self.anchor)
        self.assertIn('Export to excel', saved['description'])
        edits.store(completed, '', completed['label'])
        self.assertEqual(cli.tasks.recorded_step(edits.apply([completed])[0])['action'], 'result-menu')

    def test_surface_gesture_is_limited_to_primary_proven_hit(self):
        binding = SimpleNamespace(hwnd=1)
        with patch.object(context_menu, 'teach_surface', return_value=self.anchor) as teach:
            self.assertEqual(menu_capture.capture_surface(binding, None, self.event, ()), self.anchor)
            self.assertIsNone(menu_capture.capture_surface(binding, None, dict(self.event, scope_proven=False), ()))
            self.assertIsNone(menu_capture.capture_surface(binding, None, dict(self.event, window=8), ()))
            teach.assert_called_once_with(binding, (100, 200))

    def test_missing_region_preserves_only_a_window_relative_hint_on_a_scoped_gesture(self):
        import row_vision
        worker = self.setup_worker()
        with (patch.object(menu_capture.win, 'root_window', return_value=1),
              patch.object(menu_capture.win, 'hwnd_pid', return_value=7),
              patch.object(row_vision, '_window_rect', return_value=[20, 80, 200, 300])):
            hint = menu_capture.window_point_hint(worker.binding, self.event)
            self.assertEqual(hint, [.4, .4])
            self.assertIsNone(menu_capture.window_point_hint(worker.binding, dict(self.event, scope_proven=False)))
        key = worker.capture_action(dict(self.event, menu_window_point=hint), None)
        step = worker.model.steps[key]
        self.assertEqual(step['menu_window_point'], hint)
        self.assertNotIn('menu_anchor', step)
        self.assertEqual(cli.tasks.recorded_step(step)['menu_window_point'], hint)
        for invalid in [[float('nan'), .5], [True, .3], [1, 0]]:
            with self.assertRaisesRegex(ValueError, 'invalid_context_window_hint'):
                cli.tasks.recorded_step(dict(step, menu_window_point=invalid))

    def test_fast_popup_reuses_exact_preclick_hit_with_named_parent_group(self):
        binding, uia = Mock(hwnd=1, pid=7), Mock()
        child, parent, root = Mock(), Mock(), Mock()
        child.property.return_value = parent.property.return_value = True
        uia.from_handle.return_value = root
        uia.same.side_effect = lambda a, b: a is b
        uia.within.return_value = True
        uia.secure.return_value = False
        uia.pid.return_value = 7
        uia.basic.side_effect = {
            child: dict(role='group', automation_id='1385', name='Repeated'),
            parent: dict(role='group', name='Results') }.__getitem__
        uia.parent.return_value = parent
        obs = dict(window=1, hit_window=2, pointer=(100, 200), element=child, root=root,
                   node=dict(secure=False, enabled=True))
        history = [(9.9, obs)]
        def resolve(owner, tree, selector):
            if selector.get('name') == 'Repeated':
                raise context_menu.win.BridgeError('context_region_ambiguous')
            self.assertEqual(selector, dict(role='group', name='Results'))
            return parent
        with (patch.object(context_menu, 'teach_surface', side_effect=context_menu.win.BridgeError('context_point_occluded')),
              patch.object(context_menu, '_region', side_effect=resolve),
              patch.object(context_menu, '_rect', return_value=(80, 160, 100, 100)),
              patch.object(context_menu, '_check_region_children') as check):
            self.assertEqual(menu_capture.capture_surface(binding, uia, self.event, history),
                             dict(target=dict(role='group', name='Results'), point=[.2, .4]))
            check.assert_called_once_with(binding, uia, parent, (100, 200))
            self.assertIsNone(menu_capture.capture_surface(binding, uia, dict(self.event, hit_window=99), history))
            self.assertIsNone(menu_capture.capture_surface(binding, uia, dict(self.event, at=11), history))
            check.side_effect = context_menu.win.BridgeError('context_point_protected')
            self.assertIsNone(menu_capture.capture_surface(binding, uia, self.event, history))
