"""A demonstrated anchor may narrow a live search only to a witnessed container."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app


def item(path, role, identity, name=''):
    return dict(path=path, parent_path=path.rpartition('/')[0], role=role, automation_id=identity,
                name=name, secure=False, enabled=True)


class NearbyTests(unittest.TestCase):
    def setUp(self):
        self.screen = [item('0', 'window', 'business'), item('0/0', 'group', 'conditions'),
                       item('0/0/0', 'group', 'newWrapper'), item('0/0/0/0', 'combobox', 'state'),
                       item('0/0/1', 'button', 'search1', 'Search'), item('0/1', 'toolbar', 'commands'),
                       item('0/1/0', 'button', 'search2', 'Search')]
        self.anchor = dict(action='select', role='combobox', match='automation_id', target='state',
                           ancestors=[dict(role='group', automation_id='conditions')], within='conditions')

    def test_reordered_field_with_new_wrapper_returns_only_witnessed_area(self):
        scoped, original, anchor = app.near_region(self.screen, self.anchor)
        i, _ = app.resolve(scoped, 'Search', role='button')
        self.assertEqual((3, 'search1'), (anchor, self.screen[original[i]]['automation_id']))
        self.assertNotIn('search2', [n['automation_id'] for n in scoped])
        self.assertEqual('0', self.screen[1]['parent_path'])

    def test_missing_or_ambiguous_anchor_never_widens_search(self):
        self.screen.append(item('0/0/2', 'combobox', 'state'))
        with self.assertRaisesRegex(app.tasks.TaskError, 'target_ambiguous'):
            app.near_region(self.screen, self.anchor)
        with self.assertRaisesRegex(app.SelectionError, 'demonstrated_region_unavailable'):
            app.near_region(self.screen[:7], dict(self.anchor, ancestors=[], within=None))

    def test_reused_control_label_checked_immediately_before_saved_and_near_actions(self):
        step = dict(self.anchor, label='MES Setup')
        owner = object()
        elements = [object() for _ in self.screen]
        for nearby in (False, True):
            uia = MagicMock()
            # First lookup matches. While preparing the action, the host changes
            # the field label without replacing its AutomationId, parent or element.
            uia.semantic_label.side_effect = [
                {'label_source': 'labeled_by', 'semantic_label': n} for n in ('MES Setup', 'Other filter')]
            uia.same.side_effect = lambda a, b: a is b
            uia.__enter__.return_value = uia
            args = SimpleNamespace(command='open' if nearby else 'select', target='Search' if nearby else 'state',
                                   role='button' if nearby else 'combobox', expand=False, within=None, popup=None,
                                   window='Business', value=None if nearby else 'N')
            sent = []
            def execute(*_, still_valid):
                still_valid()
                sent.append(True)
            with self.subTest(nearby=nearby), patch.object(app, 'guard'), patch.object(app, 'observe',
                    return_value=(self.screen, elements)), patch.object(app.bridge, 'UIA', return_value=uia), \
                    patch.object(app, 'add_display_properties'), patch.object(app, 'execute', side_effect=execute), \
                    patch.object(app, 'restore_for_action', return_value=False), \
                    patch.object(app, 'semantic_label', side_effect=uia.semantic_label):
                with self.assertRaisesRegex(app.tasks.TaskError, 'recorded_label_changed'):
                    app.action(args, owner, learned=None if nearby else step, anchor=step if nearby else None)
                self.assertFalse(sent)
                self.assertEqual(uia.semantic_label.call_count, 2)
