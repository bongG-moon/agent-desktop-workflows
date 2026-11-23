"""Result recovery follows validated dependencies rather than a named detail recipe."""
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow
import result_cell
import result_trial
import registered_result


class GenericResultContinuation(unittest.TestCase):
    def recipe(self):
        child = dict(exe='Inventory.exe', title='Stock detail', related=True, after_step=2)
        return dict(schema=1, name='Stock review', exe='Inventory.exe', window='Inventory', steps=[
            dict(action='invoke', selector={'role': 'button', 'automation_id': 'refresh'}),
            dict(action='double-click-result', result_row='single',
                 selector={'role': 'group', 'automation_id': 'stock-grid'}, label='Open current stock'),
            dict(action='wait', seconds=.1),
            dict(action='verify-value', selector={'role': 'statictext', 'automation_id': 'state'},
                 value='Ready', window_context=child),
            dict(action='read-value', selector={'role': 'combobox', 'automation_id': 'location'},
                 window_context=child),
            dict(action='invoke', selector={'role': 'button', 'automation_id': 'close'},
                 verify_closed=True, window_context=child),
            dict(action='read-value', selector={'role': 'statictext', 'automation_id': 'main-status'})])

    def test_wait_comparison_and_parent_continuation_do_not_require_comment_exit_shape(self):
        recipe = self.recipe()
        flow.validate(recipe)
        self.assertTrue(result_trial.supported(recipe, 2))
        suffix = result_trial.continuation(recipe, 2)
        self.assertEqual([s['action'] for s in suffix['steps']],
                         ['double-click-result', 'wait', 'verify-value', 'read-value', 'invoke', 'read-value'])
        self.assertEqual(suffix['steps'][2]['window_context']['after_step'], 1)
        self.assertNotIn('window_context', suffix['steps'][-1])
        self.assertEqual(recipe['steps'][3]['window_context']['after_step'], 2)

    def test_row_open_only_is_a_valid_recovery(self):
        recipe = self.recipe()
        recipe['steps'] = recipe['steps'][:2]
        self.assertTrue(result_trial.supported(recipe, 2))
        self.assertEqual(len(result_trial.continuation(recipe, 2)['steps']), 1)

    def test_nested_window_dependencies_are_remapped_individually(self):
        recipe = self.recipe()
        child = recipe['steps'][3]['window_context']
        nested = dict(exe='Inventory.exe', title='Location detail', related=True, after_step=3)
        recipe['steps'] = recipe['steps'][:2] + [
            dict(action='invoke', selector={'role': 'button', 'automation_id': 'location-detail'}, window_context=child),
            dict(action='read-value', selector={'role': 'statictext', 'automation_id': 'address'}, window_context=nested)]
        self.assertTrue(result_trial.supported(recipe, 2))
        suffix = result_trial.continuation(recipe, 2)
        self.assertEqual([s['window_context']['after_step'] for s in suffix['steps'][1:]], [1, 2])

    def test_preceding_opener_is_not_replayed_or_silently_rebound(self):
        recipe = self.recipe()
        recipe['steps'][3]['window_context'] = dict(exe='Inventory.exe', title='Earlier detail', related=True, after_step=1)
        flow.validate(recipe)
        self.assertFalse(result_trial.supported(recipe, 2))
        with self.assertRaisesRegex(ValueError, 'result_resume_prior_window_required'):
            result_trial.continuation(recipe, 2)

    def test_later_parameter_is_not_invented_from_confirmation(self):
        recipe = self.recipe()
        recipe['steps'][3].pop('value')
        recipe['steps'][3]['parameter'] = 'Expected state'
        flow.validate(recipe)
        self.assertFalse(result_trial.supported(recipe, 2))

    def test_legacy_confirmed_parameter_remains_supported(self):
        recipe = self.recipe()
        recipe['steps'].insert(1, dict(action='verify-value', selector={'role':'group','automation_id':'stock-grid'},
                                      result_column='Stock', parameter='Stock'))
        recipe['steps'][2].pop('selector')
        recipe['steps'][2].pop('result_row')
        for step in recipe['steps'][3:]:
            if step.get('window_context'):
                step['window_context'] = dict(step['window_context'], after_step=3)
        self.assertTrue(result_trial.supported(recipe, 2))
        suffix = result_trial.continuation(recipe, 2, confirmed_initial_value=True)
        self.assertEqual(suffix['steps'][0]['parameter'], 'Stock')
        self.assertEqual(suffix['steps'][3]['window_context']['after_step'], 2)

    def test_visual_replacement_preserves_wait_and_parent_and_remaps_nested_windows(self):
        recipe = self.recipe()
        before = deepcopy(recipe)
        with patch.object(result_trial.grounding, '_pixels', return_value=((0, 0, 100, 100), b'')), \
             patch('visual_target._frame_patch', return_value={'marker': 'test'}):
            suffix = result_trial.visual_suffix(recipe, 2, b'image', [30, 40], reviewed=True)
        self.assertEqual(suffix['steps'][0]['action'], 'visual-click')
        self.assertEqual(suffix['steps'][1], {'action': 'wait', 'seconds': .1})
        self.assertEqual(suffix['steps'][2]['window_context']['after_step'], 1)
        self.assertNotIn('window_context', suffix['steps'][-1])
        self.assertEqual(recipe, before)

    def test_registered_provider_recovery_uses_the_same_dependency_graph(self):
        recipe = self.recipe()
        review = SimpleNamespace(recipe=recipe, pending='ticket', _write=MagicMock(), _finish=MagicMock(return_value='done'))
        record = dict(number=2, window={'hwnd':77})
        with patch.object(flow.app.bridge, 'UIA'), \
             patch.object(flow, 'observe_result_row', return_value={'status':'observed'}), \
             patch.object(flow, 'load', return_value=recipe), \
             patch.object(flow, 'run', return_value={'status':'completed_unverified'}) as run:
            result = registered_result.ResultReview._try_elements(review, flow, object(), lambda:None,
                        record, 'workflow.json', recipe['steps'][1], None)
        self.assertEqual(result, 'done')
        suffix = run.call_args.args[0]
        self.assertEqual(suffix['steps'][0]['action'], 'double-click-result')
        self.assertEqual(suffix['steps'][1]['action'], 'wait')
        self.assertEqual(suffix['steps'][2]['window_context']['after_step'], 1)
        self.assertNotIn('window_context', suffix['steps'][-1])
        self.assertFalse(any(s.get('selector', {}).get('automation_id') == 'refresh' for s in suffix['steps']))


class ResultRowLookupBudget(unittest.TestCase):
    def test_waiting_for_empty_rows_reuses_unique_table_identity(self):
        table = object()
        selector = {'role': 'table', 'automation_id': 'orders'}
        step = dict(action='double-click-result', result_row='single', selector={'role':'table'})
        calls = []
        def lookup(binding, uia, original, hint, **options):
            calls.append((original, hint, options))
            return table, hint or original
        with patch.object(flow, '_result_target', side_effect=lookup), \
             patch.object(flow, '_stable_result_selector', return_value=selector) as stable, \
             patch.object(result_cell, 'find_row', side_effect=[flow.app.bridge.BridgeError('result_row_missing'), object()]), \
             patch.object(flow.time, 'sleep'):
            receipt = flow.observe_result_row(object(), object(), step, lambda: None, wait_seconds=1)
        self.assertEqual(receipt['status'], 'observed')
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][1])
        self.assertEqual(calls[1][1], selector)
        self.assertTrue(calls[1][2]['element_hint'])
        stable.assert_called_once()


class GridVisibleCellPolicy(unittest.TestCase):
    def setUp(self):
        # Reuse the provider fixture without inheriting and rerunning its tests.
        import test_result_cell
        self.fixture = test_result_cell.ResultCellTests('test_current_single_row_sends_one_double_click')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.grid.values.update({30030:True, 30062:1, 30063:3})

    def column(self, index, *, role=50020, visible=True):
        fixture = self.fixture
        cell = fixture.node(fixture.row, role, 'variable text', index*40+2, 35)
        cell.values.update({30064:0, 30065:index, 30022:not visible})
        return cell

    def find(self, cells):
        fixture = self.fixture
        calls = []
        def read(table, column, rows):
            calls.append((column, rows))
            return iter([cells[column]])
        with patch.object(result_cell.grid_provider, 'column_cells', side_effect=read):
            found = result_cell.find_row(fixture.binding, fixture.uia, fixture.grid)
        return found, calls

    def test_hidden_first_column_uses_visible_cell_in_same_logical_row(self):
        cells = [self.column(0, visible=False), self.column(1), self.column(2)]
        found, calls = self.find(cells)
        self.assertIs(found, cells[1])
        self.assertEqual(calls, [(0,1),(1,1)])
        self.assertTrue(all(30005 not in cell.reads and 30045 not in cell.reads for cell in cells))

    def test_checkbox_column_is_skipped_for_a_data_cell(self):
        cells = [self.column(0, role=50002), self.column(1), self.column(2)]
        found, calls = self.find(cells)
        self.assertIs(found, cells[1])
        self.assertEqual(calls, [(0,1),(1,1)])

    def test_normal_grid_keeps_one_cell_fast_path(self):
        cells = [self.column(0), self.column(1), self.column(2)]
        found, calls = self.find(cells)
        self.assertIs(found, cells[0])
        self.assertEqual(calls, [(0,1)])

    def test_wide_grid_search_is_bounded_and_never_scrolls(self):
        fixture = self.fixture
        fixture.grid.values[30063] = 1000
        cells = [self.column(i, visible=False) for i in range(16)]
        with patch.object(result_cell.grid_provider, 'column_cells',
                          side_effect=lambda table,column,rows: iter([cells[column]])) as read:
            with self.assertRaisesRegex(result_cell.win.BridgeError, 'result_row_not_visible'):
                result_cell.find_row(fixture.binding, fixture.uia, fixture.grid)
        self.assertEqual(read.call_count, 16)


if __name__ == '__main__':
    unittest.main()
