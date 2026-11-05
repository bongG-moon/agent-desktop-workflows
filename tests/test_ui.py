"""User-facing editor/history regressions in hidden Tk; no desktop input."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import desktop
from storage import Store
import ui


def workflow():
    return {'schema_version': 1, 'id': 'a' * 32, 'revision': 1, 'name': 'Synthetic UI test',
            'target': {'exe_name': 'Fixture.exe', 'title': 'Synthetic fixture', 'class_name': 'Fixture'},
            'parameters': {}, 'steps': [{'id': '1' * 32, 'label': 'Input', 'action': 'set-value',
                'target': {'kind': 'element', 'locator': {'automation_id': 'Input', 'role': 'textfield'}},
                'value': 'old', 'expect': {'kind': 'value', 'value': 'old'},
                'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'}}]}


class HiddenUiTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / '.build/test-ui'
        folder.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / 'store')
        self.saved = self.store.save(workflow())
        original = ui.tk.Tk.__init__
        def hidden(widget, *args, **kwargs):
            original(widget, *args, **kwargs)
            widget.withdraw()
        with patch.object(ui.tk.Tk, '__init__', hidden):
            self.app = ui.WorkflowApp(demo=True)
        self.addCleanup(self.app.destroy)
        self.app.store, self.app.demo = self.store, False
        self.app.refresh_workflows(self.saved['id'])
        self.app._set_busy(False)
        self.app.binding = lambda: {'pid': 123, 'hwnd': 456, 'exe_name': 'Fixture.exe'}

    def editor(self, step):
        original = ui.tk.Toplevel.__init__
        def hidden(widget, *args, **kwargs):
            original(widget, *args, **kwargs)
            widget.withdraw()
        with patch.object(ui.tk.Toplevel, '__init__', hidden):
            editor = ui.StepEditor(self.app, step, self.saved)
        return editor

    def test_clear_and_wait_preserve_literal_empty_condition(self):
        for action in ('clear', 'wait'):
            with self.subTest(action=action):
                editor = self.editor(self.saved['steps'][0])
                editor.vars['action'].set(action)
                editor.vars['expected'].set('')
                editor.save()
                step, _ = editor.result
                self.assertEqual(step['expect'], {'kind': 'value', 'value': ''})
                adapter = object.__new__(desktop.DesktopAdapter)
                adapter._window = lambda _: (None, {'nodes': [
                    {'automation_id': 'Input', 'role': 'textfield', 'value': ''}]})
                self.assertTrue(adapter.check(step)['matched'])

    def test_implicit_input_expectation_survives_editor(self):
        step = copy.deepcopy(self.saved['steps'][0])
        step['expect'] = {'kind': 'value'}
        editor = self.editor(step)
        editor.vars['value'].set('new')
        editor.save()
        self.assertEqual(editor.result[0]['expect'], {'kind': 'value'})
        self.assertEqual(editor.result[0]['value'], 'new')

    def test_parameter_expectation_uses_runtime_input(self):
        editor = self.editor(self.saved['steps'][0])
        editor.vars['parameter'].set('Status')
        editor.vars['value'].set('N')
        editor.vars['expected'].set('old')
        editor.save()
        step, parameters = editor.result
        self.assertEqual(step['expect'], {'kind': 'value'})
        self.assertEqual(step['parameter'], 'Status')
        self.assertEqual(parameters['Status'], 'N')

    def test_empty_combo_cannot_be_saved_as_automatic_value_step(self):
        for parameter in ('', 'Status'):
            step = copy.deepcopy(self.saved['steps'][0])
            step['target']['locator']['role'] = 'combobox'
            editor = self.editor(step)
            editor.vars['value'].set('')
            editor.vars['parameter'].set(parameter)
            with patch.object(ui.messagebox, 'showinfo') as notice:
                editor.save()
            self.assertIsNone(editor.result)
            notice.assert_called_once()
            editor.destroy()

    def test_unobserved_combo_keeps_identity_through_manual_save_and_repair(self):
        step = copy.deepcopy(self.saved['steps'][0])
        step.update(action='manual', expect={'kind': 'manual'}, recognition={
            'target': 'identified', 'action': 'manual', 'result': 'manual', 'reason': 'combo_value_unobserved'})
        step.pop('value')
        step['target']['locator']['role'] = 'combobox'
        self.assertIn('선택값', ui.recognition_text(step))
        editor = self.editor(step)
        editor.save()
        manual, _ = editor.result
        self.assertEqual(manual['target'], step['target'])
        self.assertEqual(manual['recognition']['reason'], 'combo_value_unobserved')
        editor = self.editor(manual)
        editor.vars['action'].set('set-value')
        editor.vars['parameter'].set('Status')
        editor.vars['value'].set('N')
        editor.vars['expect'].set('value')
        editor.save()
        repaired, parameters = editor.result
        self.assertEqual(repaired['id'], step['id'])
        self.assertEqual(repaired['target'], step['target'])
        self.assertEqual(repaired['parameter'], 'Status')
        self.assertEqual(parameters['Status'], 'N')
        self.assertEqual(repaired['expect'], {'kind': 'value'})
        self.assertEqual(repaired['recognition']['result'], 'pending')
        candidate = copy.deepcopy(self.saved)
        candidate['steps'] = [repaired]
        candidate['parameters'] = parameters
        self.store.save(candidate, expected_revision=1)

    def test_independent_target_keeps_literal_even_with_input_parameter(self):
        step = copy.deepcopy(self.saved['steps'][0])
        other = {'kind': 'element', 'locator': {'automation_id': 'Result'}}
        step['expect']['target'] = other
        editor = self.editor(step)
        editor.vars['parameter'].set('Status')
        editor.vars['expected'].set('')
        editor.expect_input_value.set(True)
        editor.save()
        self.assertEqual(editor.result[0]['expect'], {'kind': 'value', 'target': other, 'value': ''})

    def test_changing_observation_kind_retains_independent_target(self):
        step = copy.deepcopy(self.saved['steps'][0])
        other = {'kind': 'element', 'locator': {'automation_id': 'Result'}}
        step['expect']['target'] = other
        editor = self.editor(step)
        editor.vars['expect'].set('exists')
        editor.save()
        self.assertEqual(editor.result[0]['expect'], {'kind': 'exists', 'target': other})

    def test_history_requires_explicit_latest_view_before_new_run(self):
        newer = copy.deepcopy(self.saved)
        newer['steps'].append(dict(copy.deepcopy(newer['steps'][0]), id='2' * 32))
        newer['parameters'] = {'LatestOnly': 'default'}
        latest = self.store.save(newer, expected_revision=1)
        old_run = {'id': 'b' * 32, 'workflow_id': self.saved['id'], 'status': 'completed',
                   'workflow': self.saved, 'parameters': {'OldOnly': 'actual'}, 'steps': []}
        self.app._run_done(old_run)
        self.assertEqual(self.app.workflow['revision'], 1)
        self.assertEqual(str(self.app.run_button['state']), 'disabled')
        self.assertEqual(self.app.param_vars['OldOnly'].get(), 'actual')
        self.app._background = Mock()
        self.app.run_workflow()
        self.app._background.assert_not_called()
        self.app.show_latest()
        self.assertEqual(self.app.workflow['revision'], latest['revision'])
        self.assertEqual(len(self.app.steps.get_children()), 2)
        self.assertFalse(self.app.history_view)
        self.assertIsNone(self.app.run)
        self.assertEqual(str(self.app.run_button['state']), 'normal')
        self.assertEqual(set(self.app.param_vars), {'LatestOnly'})

    def test_new_run_pins_displayed_revision(self):
        runner = Mock()
        self.app._background = lambda function, done: function()
        with patch('workflows.Runner', return_value=runner):
            self.app.run_workflow()
        runner.start.assert_called_once_with(self.saved['id'], self.app.binding(), {}, expected_revision=1)

    def test_external_revision_change_can_reload_after_blocked_start(self):
        newer = copy.deepcopy(self.saved)
        newer['steps'].append(dict(copy.deepcopy(newer['steps'][0]), id='2' * 32))
        latest = self.store.save(newer, expected_revision=1)
        self.assertEqual(self.app.workflow['revision'], 1)
        self.assertFalse(self.app.history_view)
        def immediate(function, done):
            self.app._set_busy(True)
            try:
                result = function()
            except Exception as error:
                self.app.events.put(('error', ui._safe_reason(error)))
            else:
                self.app.events.put(('done', (done, result)))
        self.app._background = immediate
        with patch.object(desktop, 'DesktopAdapter') as adapter, patch.object(ui.messagebox, 'showerror'):
            self.app.run_workflow()
            self.app._drain()
        adapter.assert_not_called()
        self.assertEqual(self.store.list_runs(self.saved['id']), [])
        self.assertEqual(str(self.app.latest_button['state']), 'normal')
        self.app.show_latest()
        self.assertEqual(self.app.workflow['revision'], latest['revision'])
        self.assertEqual(len(self.app.steps.get_children()), 2)

    def test_checkpoint_mismatch_cannot_be_confirmed_away(self):
        run = {'id': 'b' * 32, 'workflow_id': self.saved['id'], 'status': 'needs_review',
               'diagnostic': 'completed_prefix_condition_changed', 'current_step_id': '1' * 32,
               'workflow': self.saved, 'steps': []}
        self.app._run_done(run)
        self.assertEqual(str(self.app.confirm_button['state']), 'disabled')
        self.app._resume = Mock()
        with patch.object(ui.messagebox, 'showinfo'):
            self.app.confirm_pending()
        self.app._resume.assert_not_called()


if __name__ == '__main__':
    unittest.main()
