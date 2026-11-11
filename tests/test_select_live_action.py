"""Exercise option ownership, delivery classification and single-dispatch behavior."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'skills/desktop-select/scripts'), str(ROOT / 'scripts')]
import live_action as live
import provider_visibility
import win_bridge as win


class Element:
    def __init__(self, role, parent=None, actions=(), **properties):
        self.props = {30002: 42, 30003: role, 30010: True, 30019: False, 30022: False,
                      **{int(k): v for k, v in properties.items()}}
        self.parent, self.actions, self.children = parent, list(actions), []
        self.rect = {'x': 15, 'y': 10, 'width': 30, 'height': 10}
        if parent:
            parent.children.append(self)

    def property(self, key):
        return self.props.get(key)

    def element_property(self, key):
        return self.props.get(key)


class Binding:
    pid, hwnd = 42, 1

    def __init__(self):
        self.windows = [1]

    def check(self):
        pass

    def scoped_windows(self):
        return self.windows


class LiveActionTests(unittest.TestCase):
    def setUp(self):
        self.binding = Binding()
        self.root = Element(50032)
        self.combo = Element(50003, self.root, ['Expand', 'SetValue'],
                             **{'30070': 0, '30043': True, '30045': 'old'})
        self.windows = {1: self.root}
        self.uia = object.__new__(win.UIA)
        self.uia.from_handle = self.windows.get
        self.uia.children = lambda e: iter(e.children)
        self.uia.parent = lambda e: e.parent
        self.uia.same = lambda a, b: a is not None and a is b
        self.uia.actions = lambda e: list(e.actions)
        self.uia.bounds = lambda e: e.rect
        self.calls = []
        self.after = lambda item, op, arg: None
        for attr in ('OPEN_SECONDS', 'READBACK_SECONDS'):
            p = patch.object(live, attr, 0)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(live, '_call', self.call)
        p.start()
        self.addCleanup(p.stop)

    def call(self, item, op, arg, guard, dispatching):
        guard()
        dispatching(op)
        self.calls.append((item, op, arg))
        self.after(item, op, arg)

    def option(self, parent, value, *, owner=None, visible=True, bounds=True):
        item = Element(50007, parent, ['Select'], **{
            '30005': '[Editor] ValueListItem 1', '30036': True, '30043': True,
            '30045': value, '30022': not visible, '30080': owner or self.combo,
        })
        if not bounds:
            item.rect = {'width': 0, 'height': 0}
        return item

    def run_action(self, action='select', value='wanted', *, item=None, guard=lambda: None):
        item = item or self.combo
        return live.execute(self.binding, self.uia, item, {'role': win.ROLES[item.property(30003)]},
                            action, value, still_valid=guard)

    def test_hidden_cached_row_and_detached_popup_with_internal_names(self):
        stale = self.option(self.combo, 'wanted', visible=False, bounds=False)
        self.combo.props[30022] = True  # Real FX can report the parent offscreen.
        Element(50004, self.combo)  # Its live editor remains visible inside its rectangle.
        other_combo = Element(50003, self.root, ['Expand'])
        def apply(item, op, arg):
            if op == 'Expand':
                self.combo.props[30070] = 1
                popup = self.windows[2] = Element(50032)
                self.binding.windows.append(2)
                self.option(popup, 'wanted', owner=other_combo)
                self.live_row = self.option(popup, 'wanted')
            else:
                self.assertIs(item, self.live_row)
                self.assertIsNot(item, stale)
                self.combo.props[30045] = 'wanted'
        self.after = apply
        self.assertEqual(self.run_action()['status'], 'verified')
        self.assertEqual([op for _, op, _ in self.calls], ['Expand', 'Select'])

    def test_only_hidden_rows_can_prove_domain_for_setvalue_but_not_select(self):
        self.combo.actions = ['SetValue']
        self.option(self.combo, 'wanted', visible=True, bounds=False)
        self.after = lambda item, op, arg: self.combo.props.update({30045: arg})
        result = self.run_action()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.calls, [(self.combo, 'SetValue', 'wanted')])

    def test_repeated_visible_options_do_not_pick_first(self):
        for _ in range(2):
            self.option(self.combo, 'wanted')
        result = self.run_action()
        self.assertEqual((result['diagnostic'], result['input_dispatched']), ('combo_option_ambiguous', False))

    def test_nested_or_foreign_combo_option_cannot_authorize_setvalue(self):
        nested = Element(50003, self.combo, ['SetValue'])
        self.option(nested, 'wanted', owner=nested)
        self.option(self.combo, 'wanted', owner=nested)
        self.combo.actions = ['SetValue']
        self.assertFalse(self.run_action()['input_dispatched'])
        self.assertFalse(self.calls)

    def test_uncertain_select_does_not_retry_setvalue(self):
        self.option(self.combo, 'wanted')
        def fail(*args):
            raise win.BridgeError('hresult_error')
        self.after = fail
        result = self.run_action()
        self.assertEqual((result['status'], result['input_dispatched']), ('uncertain', None))
        self.assertEqual([op for _, op, _ in self.calls], ['Select'])

    def test_obscure_and_rejected_writes_report_different_results(self):
        self.combo.actions = ['SetValue']
        self.option(self.combo, 'wanted', visible=False)
        self.assertEqual(self.run_action()['status'], 'mismatch')
        self.combo.props[30045] = ''
        self.assertEqual(self.run_action()['status'], 'delivered_unverified')
        self.assertEqual(len(self.calls), 2)

    def test_new_popup_unrecognized_reports_prior_expand_and_no_second_input(self):
        result = self.run_action()
        self.assertEqual((result['status'], result['input_dispatched'], result['expanded']), ('blocked', True, True))
        self.assertEqual([op for _, op, _ in self.calls], ['Expand'])

    def test_last_guard_blocks_changed_target_after_reading_domain(self):
        self.option(self.combo, 'wanted')
        attempts = []
        def guard():
            attempts.append(1)
            if len(attempts) == 2:
                raise ValueError('target_changed')
        self.assertEqual(self.run_action(guard=guard)['diagnostic'], 'target_changed')
        self.assertFalse(self.calls)

    def test_menu_invoke_has_no_business_result_and_choose_rejects_offscreen_item(self):
        menu = Element(50011, self.root, ['Click'])
        self.assertEqual(self.run_action('invoke', None, item=menu)['status'], 'delivered_unverified')
        item = Element(50024, self.root, ['Select'], **{'30022': True})
        self.assertEqual(self.run_action('choose', None, item=item)['diagnostic'], 'target_offscreen')
        self.assertEqual(len(self.calls), 1)

    def test_tree_expand_and_tab_choose_verify_current_state(self):
        tree = Element(50024, self.root, ['Expand'], **{'30070': 0})
        tab = Element(50019, self.root, ['Select'], **{'30079': False})
        self.after = lambda e, op, _: e.props.update({30070: 1} if op == 'Expand' else {30079: True})
        self.assertEqual(self.run_action('expand', None, item=tree)['status'], 'verified')
        self.assertEqual(self.run_action('choose', None, item=tab)['status'], 'verified')

    def test_exact_display_alias_writes_provider_code_and_checks_provider_code(self):
        self.combo.actions = ['SetValue']
        item = self.option(self.combo, 'CODE', visible=False)
        item.props[30005] = 'Readable name'
        inventory = live.option_inventory(self.binding, self.uia, self.combo)
        self.assertEqual(inventory, {'options': [{'value': 'CODE', 'name': 'Readable name'}], 'truncated': False})
        self.after = lambda item, op, arg: self.combo.props.update({30045: arg})
        self.assertEqual(self.run_action(value='Readable name')['status'], 'verified')
        self.assertEqual(self.calls, [(self.combo, 'SetValue', 'CODE')])

    def test_code_also_used_as_name_for_another_option_is_ambiguous(self):
        self.option(self.combo, 'A').props[30005] = 'B'
        self.option(self.combo, 'B').props[30005] = 'Beta'
        self.assertEqual(self.run_action(value='B')['diagnostic'], 'combo_option_ambiguous')
        self.assertFalse(self.calls)

    def test_detached_virtual_selection_is_not_a_precondition_for_expansion(self):
        self.combo.props[30037] = True
        stale = Element(50007)
        self.combo.selected_items = lambda: [stale] if self.combo.property(30070) == 0 else []
        secure = self.uia.secure
        def check_security(element):
            if element is stale:
                raise win.BridgeError('accessibility_unavailable')
            return secure(element)
        self.uia.secure = check_security
        real_scan = live._options
        def scan(*args):
            if self.combo.property(30070) == 0:
                raise win.BridgeError('element_disappeared')
            return real_scan(*args)
        def apply(item, operation, value):
            if operation == 'Expand':
                self.combo.props[30070] = 1
                self.option(self.combo, 'wanted')
            else:
                self.combo.props[30045] = 'wanted'
        self.after = apply
        with patch.object(live, '_options', scan):
            self.assertEqual(self.run_action()['status'], 'verified')
        self.assertEqual([op for _, op, _ in self.calls], ['Expand', 'Select'])

    def test_offscreen_zero_size_combo_or_outside_window_does_not_receive_input(self):
        self.combo.props[30022] = True
        self.option(self.combo, 'wanted')
        for x,width in ((1, 0), (400, 40)):
            self.combo.rect.update(x=x, width=width)
            self.assertEqual(self.run_action()['diagnostic'], 'target_offscreen')
            self.assertFalse(self.calls)

    def test_large_read_only_inventory_marks_truncation(self):
        for index in range(130):
            self.option(self.combo, str(index))
        inventory = live.option_inventory(self.binding, self.uia, self.combo)
        self.assertTrue(inventory['truncated'])
        self.assertEqual(len(inventory['options']), 128)
        self.assertFalse(self.calls)

    def test_foreign_selected_child_does_not_verify_or_expose_its_value(self):
        self.combo.props[30045] = ''
        self.combo.actions = ['SetValue']
        other = Element(50003, self.root, ['SetValue'])
        item = self.option(self.combo, 'wanted', owner=other)
        item.props[30079] = True
        with patch.object(self.uia, '_selected_option_observation',
                          side_effect=AssertionError('must not read the foreign value')):
            result = live.combo_readback(self.binding, self.uia, self.combo)
            self.assertEqual((result['value'], result['value_evidence']['status']), (None, 'unavailable'))
            result = self.run_action()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(self.calls)

    def test_hidden_combo_with_old_overlapping_rectangle_cannot_write(self):
        self.combo.props[30022] = True
        self.combo.actions = ['SetValue']
        self.option(self.combo, 'wanted', visible=False)
        Element(50004, self.combo, **{'30022': True})
        result = self.run_action()
        self.assertEqual(result['diagnostic'], 'target_offscreen')
        self.assertFalse(self.calls)

    def test_hidden_name_only_peer_is_not_a_value_to_write(self):
        self.combo.actions = ['SetValue']
        item = self.option(self.combo, '', visible=False)
        result = self.run_action(value=item.props[30005])
        self.assertEqual(result['diagnostic'], 'combo_option_provider_value_required')
        self.assertFalse(self.calls)

    def test_visible_named_option_with_proved_owner_selects_and_reads_back(self):
        self.combo.props[30045] = ''
        row = self.option(self.combo, '')
        row.props[30005] = 'Visible option'
        self.after = lambda item, *_: row.props.update({30079: True})
        result = self.run_action(value='Visible option')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.calls, [(row, 'Select', None)])

    def test_zero_bounds_option_and_noop_expand_write_provider_code_once(self):
        # A real provider can expose every option in a collapsed combo but
        # implement Expand as a no-op. Such rows are a value domain, not hits.
        self.combo.props[30045] = ''
        row = self.option(self.combo, 'wanted', bounds=False)
        editors = [Element(50004, self.combo, **{'30043': True, '30045': 'old'}) for _ in range(2)]
        def apply(item, operation, argument):
            if operation == 'SetValue':
                for editor in editors:
                    editor.props[30045] = argument
        self.after = apply
        result = self.run_action()
        self.assertEqual((result['status'], result['effect_verification']), ('verified', 'matched'))
        self.assertEqual(self.combo.property(30070), 0)
        self.assertEqual([op for _, op, _ in self.calls], ['Expand', 'SetValue'])
        self.assertNotIn(row, [item for item, _, _ in self.calls])

    def test_missing_offscreen_visible_owned_semantic_targets(self):
        self.combo.props[30022] = None
        row = self.option(self.combo, 'wanted')
        row.props[30022] = None
        self.after = lambda item, op, value: self.combo.props.update({30045: 'wanted'})
        with patch.object(provider_visibility, '_shown', return_value=True):
            self.assertEqual(self.run_action()['status'], 'verified')
        self.assertEqual([op for _, op, _ in self.calls], ['Select'])
        button = Element(50000, self.root, ['Click'], **{'30022': None})
        with patch.object(provider_visibility, '_shown', return_value=True):
            self.assertEqual(self.run_action('click', None, item=button)['status'], 'delivered_unverified')
        self.assertEqual([op for _, op, _ in self.calls], ['Select', 'Invoke'])

    def test_missing_offscreen_without_current_visible_scope_cannot_select(self):
        self.combo.props[30022] = None
        self.option(self.combo, 'wanted')
        with patch.object(provider_visibility, '_shown', return_value=False):
            self.assertEqual(self.run_action()['diagnostic'], 'target_offscreen')
        self.assertFalse(self.calls)

    def test_provider_pattern_conflict_never_authorizes_name_only_write(self):
        self.combo.actions = ['SetValue']
        row = self.option(self.combo, 'wanted', bounds=False)
        row.props.update({30005: 'wanted', 30090: True, 30093: 'other'})
        result = self.run_action()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(self.calls)

    def test_uncertain_expand_does_not_fallback_to_provider_value_write(self):
        self.option(self.combo, 'wanted', bounds=False)
        def fail(*_):
            raise win.BridgeError('hresult_error')
        self.after = fail
        result = self.run_action()
        self.assertEqual((result['status'], result['input_dispatched']), ('uncertain', None))
        self.assertEqual([op for _, op, _ in self.calls], ['Expand'])

    def test_unreadable_or_rejected_provider_write_is_not_success(self):
        self.combo.props[30045] = ''
        self.option(self.combo, 'wanted', bounds=False)
        editors = [Element(50004, self.combo, **{'30043': True}) for _ in range(2)]
        for current, status in (('', 'delivered_unverified'), ('old', 'mismatch')):
            with self.subTest(current=current):
                self.calls.clear()
                for editor in editors:
                    editor.props[30045] = current
                result = self.run_action()
                self.assertEqual(result['status'], status)
                self.assertEqual([op for _, op, _ in self.calls], ['Expand', 'SetValue'])

    def test_conflicting_combo_editor_values_block_before_any_mutation(self):
        self.combo.props[30045] = ''
        self.option(self.combo, 'wanted', bounds=False)
        for value in ('N', 'P'):
            Element(50004, self.combo, **{'30043': True, '30045': value})
        result = self.run_action()
        self.assertEqual((result['diagnostic'], result['input_dispatched']), ('combo_value_conflict', False))
        self.assertFalse(self.calls)

    def test_conflict_appearing_before_write_stops_after_one_expand(self):
        self.combo.props[30045] = ''
        self.option(self.combo, 'wanted', bounds=False)
        def changed(*_):
            for value in ('N', 'P'):
                Element(50004, self.combo, **{'30043': True, '30045': value})
        self.after = changed
        result = self.run_action()
        self.assertEqual((result['diagnostic'], result['input_dispatched']), ('combo_value_conflict', True))
        self.assertEqual([op for _, op, _ in self.calls], ['Expand'])

    def test_hidden_name_only_option_can_expand_but_cannot_write(self):
        row = self.option(self.combo, '', bounds=False)
        row.props[30005] = 'wanted'
        result = self.run_action()
        self.assertEqual((result['diagnostic'], result['input_dispatched']),
                         ('combo_option_provider_value_required', True))
        self.assertEqual([op for _, op, _ in self.calls], ['Expand'])


if __name__ == '__main__':
    unittest.main()
