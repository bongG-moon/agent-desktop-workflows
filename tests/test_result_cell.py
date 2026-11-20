"""Current result-column verification and last-moment double-click guards."""
from contextlib import ExitStack, nullcontext
import unittest
from unittest.mock import patch

from test_control_lookup import Binding, FakeUIA, Node, win
import result_cell


class ResultCellTests(unittest.TestCase):
    def setUp(self):
        self.uia, self.binding = FakeUIA(), Binding()
        self.patches = self.enterContext(ExitStack())
        for name, replacement in (
                ('_Com', lambda pointer, uia: uia.handles[pointer.value]),
                ('Element', lambda pointer, uia: uia.nodes[pointer.value]),
                ('_sig', self.uia.allocation)):
            self.patches.enter_context(patch.object(win, name, replacement, create=True))
        self.uia.bounds = lambda element: element.box
        self.uia.within = lambda a, b: a is b or (a.parent_node is not None and self.uia.within(a.parent_node, b))
        self.grid = self.node(self.uia.root, 50008, 'Results', 0, 240)
        self.header = self.node(self.grid, 50035, 'Order', 0, 120)
        self.other_header = self.node(self.grid, 50035, 'Reference', 120, 120)
        self.row = self.node(self.grid, 50007, 'row', 0, 240)
        self.cell = self.node(self.row, 50020, 'Q42', 8, 36)
        self.hit = self.cell
        self.uia.from_point = lambda *point: self.hit

    def node(self, parent, role, name, x, width, **options):
        element = Node(self.uia, '', role, parent=parent, name=name, **options)
        element.values.update({30022: False, 30010: True})
        element.box = dict(x=x, y=20, width=width, height=18)
        return element

    def find(self):
        return result_cell.find(self.binding, self.uia, self.grid, 'Q42', column='Order')

    def prepare_pointer(self):
        for module, name, result in (
                (result_cell.coordination, 'desktop_input', nullcontext()),
                (result_cell.row_action, 'foreground', None), (win, '_combo_surface', None),
                (result_cell.row_action, '_input_tick', 41), (win, 'window_from_point', 222),
                (win, 'root_window', self.binding.hwnd), (win, 'hwnd_pid', self.binding.pid),
                (win, 'foreground', self.binding.hwnd)):
            self.patches.enter_context(patch.object(module, name, return_value=result))
        return self.patches.enter_context(patch.object(win, '_send_point', return_value=dict(ok=True, input_dispatched=True)))

    def execute(self, mutate=lambda: None):
        guards = iter((lambda: None, mutate))
        return result_cell.execute(self.binding, self.uia, self.grid, 'Q42', lambda: next(guards)(), column='Order')

    def test_same_value_in_another_column_is_not_a_match(self):
        self.node(self.row, 50020, 'Q42', 134, 30)
        self.assertIs(self.find(), self.cell)
        self.cell.values[30005] = 'different'
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_missing'):
            self.find()

    def test_nested_grid_same_x_coordinate_is_not_a_match(self):
        inner = self.node(self.row, 50008, 'Other table', 0, 240)
        inner_row = self.node(inner, 50007, 'row', 0, 240)
        self.node(inner_row, 50020, 'Q42', 8, 36)
        self.assertIs(self.find(), self.cell)

    def test_overlapping_headers_cannot_identify_column(self):
        self.other_header.box['x'] = 12
        with self.assertRaisesRegex(win.BridgeError, 'result_column_ambiguous'):
            self.find()

    def test_same_value_in_two_distinct_rows_is_ambiguous(self):
        duplicate = self.node(self.grid, 50007, 'other row', 0, 240)
        self.node(duplicate, 50020, 'Q42', 8, 36)
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_ambiguous'):
            self.find()

    def test_protected_and_edit_subtrees_are_not_result_evidence(self):
        self.cell.values[30005] = 'different'
        edit = self.node(self.row, 50004, 'Edit', 0, 120)
        self.node(edit, 50020, 'Q42', 8, 36)
        private = self.node(self.row, 50026, 'protected', 0, 120, secure=True)
        secret = self.node(private, 50020, 'Q42', 8, 36)
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_missing'):
            self.find()
        self.assertNotIn(30005, secret.reads)

    def test_double_click_dispatches_once(self):
        pointer = self.prepare_pointer()
        self.assertEqual(self.execute()['status'], 'delivered_unverified')
        pointer.assert_called_once_with(26, 29, 'double-click')

    def test_duplicate_appearing_during_guard_blocks_dispatch(self):
        pointer = self.prepare_pointer()
        def duplicate():
            self.node(self.row, 50020, 'Q42', 70, 40)
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_ambiguous'):
            self.execute(duplicate)
        pointer.assert_not_called()

    def test_replaced_cell_with_same_value_and_bounds_is_not_clicked(self):
        pointer = self.prepare_pointer()
        def recycle():
            self.cell.parent_node = None
            self.hit = self.node(self.row, 50020, 'Q42', 8, 36)
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_changed'):
            self.execute(recycle)
        pointer.assert_not_called()

    def test_hiding_row_during_guard_blocks_dispatch(self):
        pointer = self.prepare_pointer()
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_missing'):
            self.execute(lambda: self.row.values.update({30022: True}))
        pointer.assert_not_called()

    def test_same_native_window_cannot_hide_a_different_or_protected_pointer_hit(self):
        pointer = self.prepare_pointer()
        for protected, expected in ((False, 'target_outside_window'), (True, 'target_protected')):
            with self.subTest(protected=protected), patch.object(self, 'hit', self.cell):
                overlay = self.node(self.row, 50026, 'overlay', 0, 120, secure=protected)
                with self.assertRaisesRegex(win.BridgeError, expected):
                    self.execute(lambda: setattr(self, 'hit', overlay))
                pointer.assert_not_called()
