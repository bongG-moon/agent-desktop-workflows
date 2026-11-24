"""Current result-column verification and last-moment double-click guards."""
from contextlib import ExitStack, nullcontext
import unittest
from unittest.mock import patch

from test_control_lookup import Binding, FakeUIA, Node, win
import python_workflow as flow
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

    def test_multiple_result_tables_share_one_window_scan_per_observation(self):
        other = self.node(self.uia.root, 50028, 'History', 250, 120)
        header = self.node(other, 50035, 'History ID', 250, 120)
        step = dict(action='verify-value', selector={'role': 'table'}, result_column='Order', value='Q42')
        for first, second, expected_status in (('Order', 'History ID', 'verified'),
                                               ('Order', 'Order', 'blocked'), ('Part', 'History ID', 'blocked')):
            with (self.subTest(columns=(first, second)),
                  patch.object(flow.lookup, 'find_all', wraps=flow.lookup.find_all) as search):
                self.header.values[30005], header.values[30005] = first, second
                receipt = flow.observe_result(self.binding, self.uia, step, 'Q42', lambda: None, wait_seconds=0)
                self.assertEqual(receipt['status'], expected_status, receipt)
                self.assertFalse(receipt['input_dispatched'])
                if expected_status == 'blocked':
                    self.assertEqual(receipt['diagnostic'], 'result_table_ambiguous')
                else:
                    self.assertEqual(receipt['result_element']['selector'], {'role': 'list', 'name': 'Results'})
                scans = [call for call in search.call_args_list if call.kwargs == {'roles': ('table', 'list')}]
                self.assertEqual(len(scans), 1)
                self.assertIs(scans[0].args[1], self.uia.root)

    def prepare_painted_group(self):
        self.grid.values[30022] = True
        group = self.node(self.uia.root, 50026, 'Painted results', 20, 200)
        group.values[30011] = 'ResultSurface'
        self.uia.children = lambda parent: (e for e in self.uia.nodes.values() if e.parent_node is parent)
        self.uia.from_point = lambda x, y: group
        for name, value in [('window_from_point', 41), ('root_window', self.binding.hwnd), ('hwnd_pid', self.binding.pid)]:
            self.patches.enter_context(patch.object(win, name, return_value=value))
        return group

    def bind_painted(self):
        return result_cell.bound_group(self.binding, self.uia, (100, 21), (90, 32))

    def test_two_distinct_visual_points_bind_a_unique_group_by_identity(self):
        group = self.prepare_painted_group()
        self.assertEqual(self.bind_painted(), ({'role':'group','automation_id':'ResultSurface'}, group.box))
        group.values[30011] = ''
        self.assertEqual(self.bind_painted()[0], {'role':'group','name':'Painted results'})
        self.node(self.uia.root, 50026, 'Painted results', 250, 200)
        with self.assertRaisesRegex(win.BridgeError, 'target_ambiguous'):
            self.bind_painted()

    def test_binding_rejects_protected_or_form_groups_and_another_visible_table(self):
        group = self.prepare_painted_group()
        self.grid.values[30022] = False
        with self.assertRaisesRegex(win.BridgeError, 'result_table_ambiguous'):
            self.bind_painted()
        self.grid.values[30022] = True
        group.values[30019] = True
        with self.assertRaisesRegex(win.BridgeError, 'target_protected'):
            self.bind_painted()
        group.values[30019] = False
        self.node(group, 50004, 'Filter', 25, 70)
        with self.assertRaisesRegex(win.BridgeError, 'result_group_not_proven'):
            self.bind_painted()

    def test_both_points_must_directly_hit_same_group_below_the_reviewed_header(self):
        group = self.prepare_painted_group()
        with self.assertRaisesRegex(win.BridgeError, 'result_group_not_proven'):
            result_cell.bound_group(self.binding, self.uia, (90, 32), (100, 21))
        another = self.node(self.uia.root,50026,'Other',80,200)
        self.uia.from_point = lambda x, y: group if y < 25 else another
        with self.assertRaisesRegex(win.BridgeError, 'result_group_not_proven'):
            self.bind_painted()

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
        with self.assertRaisesRegex(win.BridgeError, 'result_value_not_found'):
            self.find()

    def test_current_single_row_needs_no_recorded_text_or_column_match(self):
        self.cell.values[30005] = 'changed runtime value'
        self.assertIs(result_cell.find_row(self.binding, self.uia, self.grid), self.row)
        self.cell.values[30005] = 'another runtime value'
        self.assertIs(result_cell.find_row(self.binding, self.uia, self.grid), self.row)

    def test_single_row_rejects_multiple_but_explicit_first_uses_current_vertical_order(self):
        lower = self.node(self.grid, 50007, 'next row', 0, 240)
        lower.box['y'] = 50
        with self.assertRaisesRegex(win.BridgeError, 'result_row_ambiguous'):
            result_cell.find_row(self.binding, self.uia, self.grid)
        self.assertIs(result_cell.find_row(self.binding, self.uia, self.grid, mode='first'), self.row)
        lower.box['y'] = 0
        self.assertIs(result_cell.find_row(self.binding, self.uia, self.grid, mode='first'), lower)
        lower.box['y'] = self.row.box['y']
        with self.assertRaisesRegex(win.BridgeError, 'result_row_ambiguous'):
            result_cell.find_row(self.binding, self.uia, self.grid, mode='first')

    def test_first_row_grid_provider_uses_only_current_row_zero_without_text_reads(self):
        self.grid.values.update({30030: True, 30062: 4, 30063: 2})
        self.cell.values.update({30064: 0, 30065: 0, 30005: 'a new value'})
        with patch.object(result_cell.grid_provider, 'column_cells', return_value=iter([self.cell])) as cells:
            self.assertIs(result_cell.find_row(self.binding, self.uia, self.grid, mode='first'), self.cell)
            cells.assert_called_once_with(self.grid, 0, 1)
        with self.assertRaisesRegex(win.BridgeError, 'result_row_ambiguous'):
            result_cell.find_row(self.binding, self.uia, self.grid)
        self.grid.values[30062] = 0
        with self.assertRaisesRegex(win.BridgeError, 'result_row_missing'):
            result_cell.find_row(self.binding, self.uia, self.grid, mode='first')

    def test_current_row_rejects_missing_or_false_grid_row_zero(self):
        self.grid.values.update({30030: True, 30062: 1, 30063: 2})
        self.cell.values[30064] = 1
        with patch.object(result_cell.grid_provider, 'column_cells', return_value=iter([self.cell])):
            with self.assertRaisesRegex(win.BridgeError, 'result_grid_incomplete'):
                result_cell.find_row(self.binding, self.uia, self.grid)
        self.cell.values[30064] = 0
        self.cell.values[30022] = True
        self.grid.values[30063] = 1
        with patch.object(result_cell.grid_provider, 'column_cells', return_value=iter([self.cell])):
            with self.assertRaisesRegex(win.BridgeError, 'result_row_not_visible'):
                result_cell.find_row(self.binding, self.uia, self.grid)

    def test_opaque_table_returns_once_without_subtree_value_scans(self):
        group = self.prepare_painted_group()
        with patch.object(result_cell, '_cells_in', side_effect=AssertionError('unnecessary subtree scan')):
            with self.assertRaisesRegex(win.BridgeError, 'result_column_unavailable'):
                result_cell.find(self.binding, self.uia, group, 'runtime', column='Code')
        with self.assertRaisesRegex(win.BridgeError, 'result_row_unavailable'):
            result_cell.find_row(self.binding, self.uia, group)

    def test_current_row_guard_blocks_a_new_second_row_before_input(self):
        sent = self.prepare_pointer()
        def change():
            self.node(self.grid, 50007, 'new second row', 0, 240).box['y'] = 50
        guards = iter((lambda: None, change))
        with self.assertRaisesRegex(win.BridgeError, 'result_row_ambiguous'):
            result_cell.execute_row(self.binding, self.uia, self.grid, lambda: next(guards)())
        sent.assert_not_called()

    def test_current_row_rejects_visible_peer_under_hidden_row_ancestor(self):
        self.row.values[30022] = True
        child = self.node(self.row, 50007, 'misreported visible row', 0, 240)
        with self.assertRaisesRegex(win.BridgeError, 'result_row_not_visible'):
            result_cell.find_row(self.binding, self.uia, self.grid)
        self.grid.values.update({30030: True, 30062: 1, 30063: 2})
        self.cell.values.update({30064: 0, 30065: 0})
        with patch.object(result_cell.grid_provider, 'column_cells', return_value=iter([self.cell])):
            with self.assertRaisesRegex(win.BridgeError, 'result_grid_incomplete'):
                result_cell.find_row(self.binding, self.uia, self.grid)

    def test_current_row_does_not_hide_additional_offscreen_results(self):
        hidden = self.node(self.grid, 50007, 'offscreen result', 0, 240)
        hidden.values[30022] = True
        hidden.box['y'] = 50
        with self.assertRaisesRegex(win.BridgeError, 'result_row_ambiguous'):
            result_cell.find_row(self.binding, self.uia, self.grid, mode='single')
        with self.assertRaisesRegex(win.BridgeError, 'result_row_not_visible'):
            result_cell.find_row(self.binding, self.uia, self.grid, mode='first')

    def test_current_single_row_sends_one_double_click(self):
        sent = self.prepare_pointer()
        receipt = result_cell.execute_row(self.binding, self.uia, self.grid, lambda: None)
        self.assertEqual(receipt['target_resolution'], 'current_single_result')
        self.assertTrue(receipt['input_dispatched'])
        sent.assert_called_once_with(120, 29, 'double-click')

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
        with self.assertRaisesRegex(win.BridgeError, 'result_value_not_found'):
            self.find()
        self.assertNotIn(30005, secret.reads)

    def test_cell_with_descriptive_name_uses_actual_value_and_rejects_conflict(self):
        self.cell.values.update({30005: 'Description', 30045: 'Q42'})
        self.assertIs(self.find(), self.cell)
        self.cell.values.update({30045: None, 30093: 'Q42'})
        self.assertIs(self.find(), self.cell)
        self.cell.values.update({30005: 'Q42', 30045: 'other'})
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_missing'):
            self.find()

    def test_value_only_cell_duplicates_in_same_column_are_ambiguous(self):
        row = self.node(self.grid, 50007, 'row 2', 0, 240)
        self.node(row, 50025, '', 8, 36).values[30045] = 'Q42'
        with self.assertRaisesRegex(win.BridgeError, 'result_cell_ambiguous'):
            self.find()

    def test_cell_relationship_identifies_logical_column_without_header_in_the_tree(self):
        self.header.values.update({30003: 50025, 30011: 'OrderHeading', 30022: True})
        self.cell.values.update({30039: True, 30005: '', 30093: 'Q42'})
        evidence = {}
        with patch.object(result_cell.grid_provider, 'headers', return_value=[self.header]) as read:
            match = result_cell.find(self.binding,self.uia,self.grid,'Q42',column='Order',evidence=evidence)
            self.assertIs(match, self.cell)
            self.assertEqual(evidence['column_hint'], {'role':'custom','automation_id':'OrderHeading'})
            read.assert_called_once_with(self.cell, cell=True)
        with patch.object(result_cell.grid_provider,'headers',return_value=[self.other_header]):
            with self.assertRaisesRegex(win.BridgeError, 'result_column_unavailable'):
                self.find()

    def test_grid_getitem_revalidates_cached_header_index_and_current_value_after_reorder(self):
        self.grid.values.update({30030: True, 30038: True, 30062: 1, 30063: 2})
        self.header.values[30011] = 'OrderHead'
        self.cell.values.update({30005: '', 30045: 'Q42', 30065: 0})
        headers = [self.header, self.other_header]
        with patch.object(result_cell.grid_provider, 'headers', return_value=headers), \
                patch.object(result_cell.grid_provider,'column_cells',side_effect=lambda *a:[self.cell]) as read:
            evidence = {}
            self.assertIs(result_cell.find(self.binding,self.uia,self.grid,'Q42',column='Order',evidence=evidence),self.cell)
            hint = evidence['column_hint']
            self.assertEqual(hint['index'],0)
            self.assertEqual(evidence['source'],'grid_pattern')
            headers.reverse()
            self.cell.values[30065] = 1
            self.assertIs(result_cell.find(self.binding,self.uia,self.grid,'Q42',column='Order',column_hint=hint,
                                           evidence=evidence),self.cell)
            read.assert_called_with(self.grid,1,1)
            self.assertEqual(evidence['column_hint']['index'],1)
            self.cell.values[30045] = 'Q43'
            with self.assertRaisesRegex(win.BridgeError,'result_value_not_found'):
                self.find()

    def test_nonvisual_griditem_column_verifies_data_but_zero_rect_cannot_receive_input(self):
        self.grid.values.update({30030:True, 30038:True, 30062:1, 30063:2})
        self.header.box.update(width=0, height=0)
        self.header.values[30065] = 0
        self.cell.box.update(width=0, height=0)
        self.cell.values.update({30005:'', 30045:'Q42', 30065:0, 30064:0})
        with patch.object(result_cell.grid_provider, 'headers', return_value=[self.header, self.other_header]), \
                patch.object(result_cell.grid_provider, 'column_cells', return_value=[self.cell]):
            self.assertIs(self.find(), self.cell)
            pointer = self.prepare_pointer()
            with self.assertRaisesRegex(win.BridgeError, 'result_cell_not_clickable'):
                self.execute()
            pointer.assert_not_called()

    def test_gridview_cell_wrapper_and_textblock_with_empty_legacy_value(self):
        self.grid.values.update({30030: True, 30038: True, 30062: 1, 30063: 2})
        wrapper = self.node(self.row, 50025, '', 0, 120)
        wrapper.values.update({30093:'', 30064:0, 30065:0})
        self.cell.parent_node = wrapper
        self.cell.values[30093] = ''
        with patch.object(result_cell.grid_provider,'headers',return_value=[self.header,self.other_header]), \
                patch.object(result_cell.grid_provider,'column_cells',return_value=[wrapper]):
            self.assertIs(self.find(), self.cell)
            self.cell.values[30005] = 'new value'
            with self.assertRaisesRegex(win.BridgeError, 'result_value_not_found'):
                self.find()
            wrapper.values[30005] = 'Q42'
            with self.assertRaisesRegex(win.BridgeError, 'result_value_not_found'):
                self.find()
            self.cell.values[30005] = 'Q42'
            wrapper.values[30005] = 'Cell'
            self.assertIs(self.find(), self.cell)
            wrapper.values[30045] = ''
            with self.assertRaisesRegex(win.BridgeError, 'result_value_not_found'):
                self.find()

    def test_partial_column_cannot_prove_a_readable_match_is_unique(self):
        self.grid.values.update({30030:True,30038:True,30062:2,30063:2})
        other = self.node(self.grid, 50029, '', 4, 36)
        with patch.object(result_cell.grid_provider,'headers',return_value=[self.header,self.other_header]), \
                patch.object(result_cell.grid_provider,'column_cells',return_value=[self.cell,other]):
            with self.assertRaisesRegex(win.BridgeError, 'result_grid_incomplete'):
                self.find()

    def test_oversized_partially_materialized_grid_does_not_scan_every_row_or_click(self):
        self.grid.values.update({30030:True,30038:True,30062:65,30063:2})
        with patch.object(result_cell.grid_provider,'headers',return_value=[self.header,self.other_header]), \
                patch.object(result_cell.grid_provider,'column_cells') as read:
            with self.assertRaisesRegex(win.BridgeError,'result_grid_scan_limit'):
                self.find()
            read.assert_not_called()

    def test_empty_or_known_other_columns_report_mismatch_without_visual_fallback(self):
        self.grid.values.update({30030:True,30038:True,30062:0,30063:2})
        with patch.object(result_cell.grid_provider, 'headers', return_value=[self.header,self.other_header]):
            with self.assertRaisesRegex(win.BridgeError, 'result_value_not_found'):
                self.find()
            self.grid.values[30062] = 2
            self.header.values[30005] = 'Not the requested column'
            with self.assertRaisesRegex(win.BridgeError, 'result_column_not_found'):
                self.find()

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
