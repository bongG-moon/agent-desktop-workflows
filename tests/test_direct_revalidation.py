"""Initial 0.4.0 label resolution stays scoped while later checks avoid full snapshots."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import control_lookup
from test_select_targeting import node


class DirectRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [node('0', 'window', name='Business'),
                      node('0/0', identifier='pnlConditionArea'),
                      node('0/0/0', 'statictext', name='MES Setup', rect=(10, 100, 88, 19)),
                      node('0/0/1', 'combobox', identifier='state', rect=(145, 100, 170, 19)),
                      node('0/0/2', 'button', name='조회', identifier='search', rect=(400, 100, 80, 24))]
        self.elements = [Mock() for _ in self.nodes]
        for element, snapshot in zip(self.elements, self.nodes):
            element.property.side_effect = lambda key, n=snapshot: {30005: n['name']}.get(key)
        self.owner, self.uia = Mock(pid=42, hwnd=1), Mock()
        self.uia.__enter__ = Mock(return_value=self.uia)
        self.uia.__exit__ = Mock(return_value=False)
        self.uia.from_handle.return_value = self.elements[0]
        self.uia.same.side_effect = lambda a, b: a is b
        self.sent, self.selectors = [], []

        def lookup(binding, uia, selector, root=None):
            self.selectors.append(selector)
            index = next(i for i, n in enumerate(self.nodes) if n['automation_id'] == selector['automation_id'])
            return self.elements[index]

        def execute(*args, still_valid):
            still_valid()
            still_valid()
            self.sent.append(args[4])
            return {'status': 'delivered_unverified', 'input_dispatched': True}

        replacements = [(app, 'guard', Mock()), (app.visibility, 'restore_for_action', Mock()),
                        (app, 'bind_surface', Mock(return_value=self.owner)),
                        (app.bridge, 'UIA', Mock(return_value=self.uia)),
                        (app, 'observe', Mock(side_effect=lambda *a, **kw: (deepcopy(self.nodes), self.elements))),
                        (app, 'execute', Mock(side_effect=execute)),
                        (app, 'field_label', Mock(return_value={'semantic_label': 'MES Setup',
                                                             'label_source': 'sibling_geometry'})),
                        (control_lookup, 'resolve', Mock(side_effect=lookup))]
        for obj, name, value in replacements:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def action(self, command='select', target='MES Setup', within=None):
        return app.action(argparse.Namespace(command=command, role=None, target=target, within=within,
                          window='Business', value='N' if command == 'select' else None,
                          popup=None, expand=False), self.owner)

    def test_label_resolves_once_then_revalidates_exact_scoped_identity(self):
        self.assertEqual(self.action(within='conditions')['input_dispatched'], True)
        app.observe.assert_called_once()
        self.assertEqual(self.sent, ['select'])
        self.assertEqual(len(self.selectors), 2)
        self.assertEqual(self.selectors[0], {'automation_id': 'state', 'role': 'combobox', 'ancestors': [
            {'role': 'group', 'automation_id': 'pnlConditionArea'}, {'role': 'window', 'name': 'Business'}]})

    def test_initial_ambiguity_or_wrong_scope_never_reaches_native_dispatch(self):
        self.nodes.append(node('0/0/3', 'combobox', identifier='state'))
        with self.assertRaisesRegex(app.SelectionError, 'target_ambiguous'):
            self.action(target='state')
        with self.assertRaisesRegex(app.SelectionError, 'scope_missing'):
            self.action(target='state', within='other')
        control_lookup.resolve.assert_not_called()
        self.assertFalse(self.sent)

    def test_same_id_replacement_or_changed_window_cannot_receive_input(self):
        control_lookup.resolve.side_effect = lambda *a, **kw: Mock()
        with self.assertRaisesRegex(app.SelectionError, 'target_changed'):
            self.action()
        self.uia.from_handle.return_value = Mock()
        with self.assertRaisesRegex(app.SelectionError, 'target_changed'):
            self.action()
        self.assertFalse(self.sent)

    def test_native_ambiguity_does_not_trigger_full_scan_or_retry(self):
        control_lookup.resolve.side_effect = app.bridge.BridgeError('target_ambiguous')
        with self.assertRaisesRegex(app.bridge.BridgeError, 'target_ambiguous'):
            self.action()
        app.observe.assert_called_once()
        self.assertFalse(self.sent)

    def test_changed_field_label_or_command_caption_blocks(self):
        app.field_label.return_value = {'semantic_label': 'Other', 'label_source': 'sibling_geometry'}
        with self.assertRaisesRegex(app.SelectionError, 'target_label_changed'):
            self.action()
        self.elements[4].property.side_effect = lambda key: 'Delete' if key == 30005 else None
        with self.assertRaisesRegex(app.SelectionError, 'target_name_changed'):
            self.action('open', '조회')
        self.assertFalse(self.sent)

    def test_combo_name_may_follow_value_without_changing_field_identity(self):
        self.nodes[3]['name'] = 'W'
        def execute(*args, still_valid):
            still_valid()
            # Some providers use Name for the displayed value. The semantic
            # label, target element, ID and container are all unchanged.
            self.nodes[3]['name'] = 'N'
            still_valid()
            return {'status': 'verified', 'input_dispatched': True}
        app.execute.side_effect = execute
        self.assertEqual(self.action()['status'], 'verified')
        app.observe.assert_called_once()


if __name__ == '__main__':
    unittest.main()
