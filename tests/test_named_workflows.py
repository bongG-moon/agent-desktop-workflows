"""Name-only recordings retain scoped native lookup and runtime parameters."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Load the checked bridge before the adapters.
import control_lookup as lookup
import learned_tasks as tasks
import python_workflow as flow
import recorded_plan as plans
import win_bridge as win
from test_control_lookup import FakeUIA, Node, Binding


def recording():
    steps = []
    for name, parameter in (('Find what:', 'FindText'), ('Replace with:', 'Replacement')):
        step = dict(action='teach-target',
                    target=dict(kind='element', locator=dict(name=name, role='combobox',
                                ancestors=[dict(name='Conditions', role='group')])),
                    recognition=dict(target='identified', action='requires_instruction',
                                     result='manual', reason='combo_choice_unproven'))
        step['instruction'] = tasks.compile_instruction(step, f'입력받은 {{{parameter}}} 변수를 이 항목에 입력해줘')
        steps.append(step)
    button = dict(action='click', target=dict(kind='element', locator=dict(name='Replace All', role='button')),
                  recognition=dict(target='identified', action='supported', result='manual'))
    button['instruction'] = tasks.compile_instruction(button, '이 버튼을 눌러줘')
    steps.append(button)
    return tasks.from_recording('Variable replace', 'Example.exe', 'Replace', steps)


class SessionUIA(FakeUIA):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def actions(self, element):
        return ['SetValue'] if element.values[30003] == 50003 else ['Click']


class NamedLookupTests(unittest.TestCase):
    def setUp(self):
        self.uia, self.binding = SessionUIA(), Binding()
        self.root = self.uia.root
        for target, replacement in (
                ('_Com', lambda pointer, uia: uia.handles[pointer.value]),
                ('Element', lambda pointer, uia: uia.nodes[pointer.value]),
                ('_sig', self.uia.allocation)):
            handle = patch.object(win, target, replacement, create=True)
            handle.start()
            self.addCleanup(handle.stop)

    def node(self, name, role=50003, parent=None, **kwargs):
        return Node(self.uia, '', role, name=name, parent=self.root if parent is None else parent, **kwargs)

    def resolve(self, name='Find what:', role='combobox', **extra):
        return lookup.resolve(self.binding, self.uia, dict(name=name, role=role, **extra))

    def test_exact_name_and_role_are_filtered_in_native_findall(self):
        wanted = self.node('Find what:')
        lower = self.node('find what:')
        button = self.node('Find what:', 50000)
        self.assertIs(self.resolve(), wanted)
        self.assertEqual(self.uia.queries[0][1:],
                         (6, 4, ('and', ('property', 30003, 50003), ('property', 30005, 'Find what:'))))
        self.assertEqual(lower.reads, [])
        self.assertEqual(button.reads, [])

    def test_same_named_fields_are_disambiguated_only_by_recorded_ancestors(self):
        form = self.node('Form', 50026)
        group = self.node('Conditions', 50026, parent=form)
        wanted = self.node('Find what:', parent=group)
        self.node('Find what:', parent=self.node('Other form', 50026))
        parents = [dict(name='Conditions', role='group'), dict(name='Form', role='group')]
        self.assertIs(self.resolve(ancestors=parents), wanted)
        with self.assertRaisesRegex(win.BridgeError, '^target_ambiguous$'):
            self.resolve()
        with self.assertRaisesRegex(win.BridgeError, '^target_missing$'):
            self.resolve(ancestors=list(reversed(parents)))

    def test_missing_changed_name_and_duplicate_never_pick_first(self):
        wanted = self.node('Find what:')
        self.assertIs(self.resolve(), wanted)
        wanted.values[30005] = 'Renamed'
        with self.assertRaisesRegex(win.BridgeError, '^target_missing$'):
            self.resolve()
        self.node('Renamed')
        with self.assertRaisesRegex(win.BridgeError, '^target_ambiguous$'):
            self.resolve('Renamed')

    def test_protected_unknown_and_foreign_candidates_supply_no_names_or_values(self):
        secret = self.node('Secret', 50026, secure=True)
        unknown = self.node('Unknown', 50026, secure=None)
        hidden = self.node('Find what:', parent=secret)
        unproven = self.node('Find what:', parent=unknown)
        foreign = self.node('Find what:', pid=8)
        other_window = Node(self.uia, 'Other', 50032)
        outside = self.node('Find what:', parent=other_window)
        self.uia.query_rows = [hidden, unproven, foreign, outside]
        with self.assertRaisesRegex(win.BridgeError, '^target_missing$'):
            self.resolve()
        for element in (hidden, unproven, foreign, outside, secret, unknown):
            self.assertFalse({30005, 30011, 30045, 30093} & set(element.reads))

    def test_selector_cannot_mix_name_and_id_or_accept_empty_name(self):
        for selector in ({'name': '', 'role': 'button'}, {'name': ' ', 'role': 'button'},
                         {'name': 'Find what:', 'automation_id': '1601', 'role': 'combobox'}):
            with self.assertRaisesRegex(win.BridgeError, '^invalid_target_selector$'):
                lookup.resolve(self.binding, self.uia, selector)
        self.assertEqual(self.uia.queries, [])


class NamedWorkflowTests(unittest.TestCase):
    node = NamedLookupTests.node

    def setUp(self):
        NamedLookupTests.setUp(self)
        self.group = self.node('Conditions', 50026)
        self.find = self.node('Find what:', parent=self.group)
        self.replace = self.node('Replace with:', parent=self.group)
        self.button = self.node('Replace All', 50000)
        self.source = recording()
        self.recipe = plans.compile_plan(self.source)['envelope']['recipe']
        for target, options in (
                ('bind', dict(return_value=self.binding)),
                ('app.bridge.UIA', dict(return_value=self.uia)),
                ('app.guard', dict(return_value=None)),
                ('app.visibility.restore_for_action', dict(return_value=False)),
                ('live.combo_readback', dict(return_value={'value': '', 'value_evidence': {'status': 'unavailable'}}))):
            handle = patch('python_workflow.' + target, **options)
            handle.start()
            self.addCleanup(handle.stop)

    def test_compilation_preserves_exact_names_ancestors_and_variables(self):
        self.assertEqual(plans.COMPILER, 4)
        first, second, last = self.recipe['steps']
        self.assertEqual(first['selector'], dict(name='Find what:', role='combobox',
                                               ancestors=[dict(name='Conditions', role='group')]))
        self.assertEqual([first['parameter'], second['parameter']], ['FindText', 'Replacement'])
        self.assertEqual(last['selector'], dict(name='Replace All', role='button'))
        self.assertNotIn('automation_id', json.dumps(self.recipe))

    def test_registration_serializes_names_without_numeric_id_or_capture_mutation(self):
        unchanged = deepcopy(self.source)
        with tempfile.TemporaryDirectory() as folder, patch.object(flow.live, 'execute') as execute:
            out = Path(folder) / 'replace-skill'
            result = flow.register(self.recipe, out, source_task=self.source)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual(result['required_parameters'], ['FindText', 'Replacement'])
            self.assertFalse(result['input_dispatched'])
            saved = flow.load(out / 'workflow.json')
            self.assertEqual(saved['steps'][0]['selector'], self.recipe['steps'][0]['selector'])
            self.assertEqual(saved['steps'][-1]['command_name'], 'Replace All')
            self.assertEqual(json.loads((out / 'source-task.json').read_text(encoding='utf8')), unchanged)
            execute.assert_not_called()
        self.assertEqual(self.source, unchanged)

    def test_single_session_replays_current_values_without_full_tree(self):
        calls = []
        def execute(binding, uia, element, node, action, value, *, still_valid):
            still_valid()
            calls.append((element, action, value))
            return dict(status='verified', input_dispatched=True) if action == 'select' else dict(
                status='delivered_unverified', diagnostic='business_result_not_verified', input_dispatched=True)
        with patch.object(flow.live, 'execute', side_effect=execute), patch.object(
                flow.app, 'observe', side_effect=AssertionError('Full tree not required')):
            result = flow.run(self.recipe, {'FindText': 'VALUE_A', 'Replacement': '00123 한글'})
        self.assertEqual(result['status'], 'completed_unverified')
        self.assertEqual([c[2] for c in calls], ['VALUE_A', '00123 한글', None])
        self.assertEqual(flow.app.bridge.UIA.call_count, 1)

    def test_duplicate_or_missing_first_target_blocks_before_input(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), patch.object(flow.live, 'execute') as execute:
                self.find.values[30005] = 'Find what:' if duplicate else 'Renamed'
                extra = self.node('Find what:', parent=self.group) if duplicate else None
                result = flow.run(self.recipe, {'FindText': 'A', 'Replacement': 'B'})
                self.assertEqual(result['diagnostic'], 'target_ambiguous' if duplicate else 'target_missing')
                self.assertFalse(result['input_dispatched'])
                execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
