"""Native query ABI and scope contracts, without sending desktop input."""
import ctypes as C
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Loads the pinned bridge before importing adapters.
import control_lookup as native
import win_bridge as win


def assign(output, value):
    output._obj.value = value


class Node:
    def __init__(self, uia, identity, role, *, parent=None, pid=7, secure=False, name=''):
        self.uia, self.parent_node = uia, parent
        self.values = {30011: identity, 30003: role, 30002: pid, 30019: secure, 30005: name}
        self.ptr, self.reads, self.closed = C.c_void_p(uia.next()), [], 0
        uia.nodes[self.ptr.value] = self

    def property(self, key):
        self.reads.append(key)
        return self.values.get(key)

    def close(self):
        self.closed += 1

    def call(self, slot, types, scope, condition, output):
        self.uia.queries.append((self, slot, scope, self.uia.handles[condition.value].value))
        if self.uia.query_error:
            raise win.BridgeError(self.uia.query_error)
        rows = self.uia.query_rows
        if rows is None:
            def under(node):
                parent = node.parent_node
                while parent is not None:
                    if parent is self:
                        return True
                    parent = parent.parent_node
                return False
            def matches(node, expression):
                if expression[0] == 'property':
                    return node.values.get(expression[1]) == expression[2]
                a, b = matches(node, expression[1]), matches(node, expression[2])
                return a and b if expression[0] == 'and' else a or b
            rows = [node for node in self.uia.nodes.values() if under(node) and
                    matches(node, self.uia.handles[condition.value].value)]
        assign(output, self.uia.handle('array', rows).ptr.value)


class Handle:
    def __init__(self, uia, kind, value=None):
        self.uia, self.kind, self.value = uia, kind, value
        self.ptr, self.closed = C.c_void_p(uia.next()), 0
        uia.handles[self.ptr.value] = self

    def close(self):
        self.closed += 1

    def call(self, slot, types, *args):
        if self.kind == 'walker':
            assert slot == 3
            parent = self.uia.nodes[args[0].value].parent_node
            assign(args[1], parent.ptr.value if parent else None)
            return
        if self.kind == 'array':
            if slot == 3:
                assign(args[0], len(self.value))
            else:
                assert slot == 4
                assign(args[1], self.value[args[0]].ptr.value)
            return
        assert self.kind == 'client'
        if slot == 16:
            self.uia.raw_walker_requests += 1
            assign(args[0], self.uia.handle('walker').ptr.value)
            return
        self.uia.condition_slots.append(slot)
        if slot == 23:
            key, variant, output = args
            self.uia.condition_types.append(types)
            value = C.wstring_at(variant.pointer) if variant.vt == 8 else variant.i4
            expression = ('property', key, value)
        else:
            assert slot in (25, 28)
            a, b, output = args
            expression = ('and' if slot == 25 else 'or',
                          self.uia.handles[a.value].value, self.uia.handles[b.value].value)
        assign(output, self.uia.handle('condition', expression).ptr.value)


class FakeUIA:
    def __init__(self):
        self.sequence = 0
        self.nodes, self.handles, self.queries = {}, {}, []
        self.condition_slots, self.condition_types, self.buffers, self.freed = [], [], [], []
        self.query_rows, self.query_error = None, None
        self.raw_walker_requests = 0
        self.client = self.handle('client')
        self.root = Node(self, 'Business', 50032)

    def next(self):
        self.sequence += 1
        return self.sequence

    def handle(self, kind, value=None):
        return Handle(self, kind, value)

    def pid(self, node):
        return node.property(30002)

    def secure(self, node):
        return node.property(30019) is not False

    def same(self, a, b):
        return a is b

    def parent(self, node):
        return node.parent_node

    def from_handle(self, hwnd):
        return self.root if hwnd == 123 else None

    def allocation(self, dll, name, *types):
        if name == 'SysAllocString':
            def allocate(text):
                buffer = C.create_unicode_buffer(text)
                self.buffers.append(buffer)
                return C.cast(buffer, C.c_void_p).value
            return allocate
        assert name == 'SysFreeString'
        return self.freed.append


class Binding:
    pid, hwnd = 7, 123

    def __init__(self, fail_after=None):
        self.calls, self.fail_after = 0, fail_after

    def check(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise win.BridgeError('target_lifetime_changed')


class NativeTargetsTests(unittest.TestCase):
    def setUp(self):
        self.uia, self.binding = FakeUIA(), Binding()
        self.root = self.uia.root
        for target, replacement in (
            ('_Com', lambda pointer, uia: uia.handles[pointer.value]),
            ('Element', lambda pointer, uia: uia.nodes[pointer.value]),
            ('_sig', self.uia.allocation),
        ):
            handle = patch.object(win, target, replacement, create=True)
            handle.start()
            self.addCleanup(handle.stop)

    def node(self, identity='Field', role=50003, **kwargs):
        return Node(self.uia, identity, role, parent=kwargs.pop('parent', self.root), **kwargs)

    def resolve(self, **selector):
        return native.resolve(self.binding, self.uia, dict(automation_id='Field', role='combobox', **selector))

    def assert_closed(self):
        self.assertTrue(all(handle.closed == 1 for handle in self.uia.handles.values()
                            if handle.kind not in ('client', 'walker')))
        self.assertEqual(len(self.uia.buffers), len(self.uia.freed))

    def test_query_is_native_descendants_with_variant_by_value_and_role_filter(self):
        wanted, other = self.node(), self.node(role=50000)
        self.assertEqual(native.find_all(self.uia, self.root, automation_id='Field', roles=('combobox',)), [wanted])
        self.assertEqual(self.uia.queries[0][1:3], (6, 4))
        self.assertEqual(self.uia.condition_slots, [23, 23, 25])
        self.assertTrue(all(signature[1] is win.VARIANT for signature in self.uia.condition_types))
        self.assertEqual(other.reads, [])
        self.assert_closed()

    def test_multiple_role_codes_are_or_conditions(self):
        group1, group2, button = self.node('One', 50026), self.node('Two', 50033), self.node('Three', 50000)
        self.assertEqual(native.find_all(self.uia, self.root, roles=('group',)), [group1, group2])
        self.assertIn(28, self.uia.condition_slots)
        self.assertEqual(button.reads, [])
        self.assert_closed()

    def test_exact_id_match_and_duplicate_ids_are_not_first_match(self):
        wanted = self.node()
        self.node('field')
        self.assertIs(self.resolve(), wanted)
        self.node()
        with self.assertRaisesRegex(win.BridgeError, '^target_ambiguous$'):
            self.resolve()

    def test_missing_target_is_distinct_from_ambiguous(self):
        self.node('Other')
        with self.assertRaisesRegex(win.BridgeError, '^target_missing$'):
            self.resolve()
        self.assert_closed()

    def test_scoped_root_disambiguates_equal_ids_in_other_groups(self):
        group, other = self.node('Form', 50026), self.node('Other', 50026)
        wanted = self.node(parent=group)
        self.node(parent=other)
        self.assertIs(native.resolve(self.binding, self.uia,
                                    dict(automation_id='Field', role='combobox'), root=group), wanted)

    def test_nearest_first_ancestors_support_id_or_name(self):
        form = self.node('Form', 50026)
        group = self.node('Row', 50026, parent=form, name='Conditions')
        wanted = self.node(parent=group)
        self.node(parent=self.node('Elsewhere', 50026))
        self.assertIs(self.resolve(ancestors=[dict(role='group', name='Conditions'),
                                             dict(role='group', automation_id='Form')]), wanted)
        with self.assertRaisesRegex(win.BridgeError, '^target_missing$'):
            self.resolve(ancestors=[dict(role='group', automation_id='Form'),
                                    dict(role='group', name='Conditions')])

    def test_protected_or_unknown_ancestor_skips_candidate_before_identity_reads(self):
        for secure in (True, None):
            parent = self.node('Secret', 50026, secure=secure)
            child = self.node(parent=parent)
            self.uia.query_rows = [child]
            self.assertEqual(native.find_all(self.uia, self.root, roles=('combobox',)), [])
            self.assertFalse({30005, 30011, 30045, 30093} & set(child.reads + parent.reads))
            self.assertEqual(child.closed, 1)

    def test_foreign_pid_or_another_window_is_not_target_scope(self):
        foreign = self.node(pid=8)
        outside = Node(self.uia, 'Field', 50003, parent=Node(self.uia, 'Second', 50032))
        self.uia.query_rows = [foreign, outside]
        self.assertEqual(native.find_all(self.uia, self.root, roles=('combobox',)), [])
        self.assertFalse({30005, 30011} & set(foreign.reads + outside.reads))

    def test_limit_is_checked_before_candidate_metadata_and_handles_are_freed(self):
        a, b = self.node(), self.node()
        with self.assertRaisesRegex(win.BridgeError, '^target_candidates_incomplete$'):
            native.find_all(self.uia, self.root, automation_id='Field', limit=1)
        self.assertEqual((a.reads, b.reads), ([], []))
        self.assert_closed()

    def test_failed_find_closes_conditions_and_bstr(self):
        self.uia.query_error = 'accessibility_unavailable'
        with self.assertRaisesRegex(win.BridgeError, '^accessibility_unavailable$'):
            self.resolve()
        self.assert_closed()

    def test_process_lifetime_is_rechecked_after_search(self):
        wanted = self.node()
        self.binding.fail_after = 1
        with self.assertRaisesRegex(win.BridgeError, '^target_lifetime_changed$'):
            self.resolve()
        self.assertEqual(wanted.closed, 1)

    def test_selected_root_can_itself_be_the_registered_target(self):
        group = self.node('Form', 50026)
        self.assertIs(native.resolve(self.binding, self.uia,
                                    dict(automation_id='Form', role='group'), root=group), group)
        self.assertEqual(group.closed, 0)

    def test_invalid_selectors_do_not_search(self):
        for selector in ({'name': 'Field', 'automation_id': 'Field', 'role': 'combobox'},
                         {'automation_id': 'Field', 'role': 'unknown'},
                         {'automation_id': 'Field', 'role': 'combobox', 'ancestors': [{'name': 'Row'}]},
                         {'automation_id': 'Field', 'role': 'combobox', 'ancestors': [{'role': 'group'}]}):
            with self.assertRaises(win.BridgeError):
                native.resolve(self.binding, self.uia, selector)
        self.assertEqual(self.uia.queries, [])

    def test_no_unbounded_empty_query_and_no_invalid_limit(self):
        for query in ({}, {'roles': ('unknown',)}, {'roles': ('button',), 'limit': True},
                      {'automation_id': '  '}, {'automation_id': 'x\0y'}):
            with self.assertRaises(win.BridgeError):
                native.find_all(self.uia, self.root, **query)
        self.assertEqual(self.uia.queries, [])

    def test_protected_root_never_runs_native_search(self):
        self.root.values[30019] = True
        with self.assertRaisesRegex(win.BridgeError, '^target_protected$'):
            native.find_all(self.uia, self.root, roles=('combobox',))
        self.assertEqual(self.uia.queries, [])

    def test_raw_protected_ancestor_cannot_be_skipped_by_control_walker(self):
        hidden = self.node('PasswordWrapper', 50026, secure=True)
        child = self.node(parent=hidden)
        self.uia.parent = lambda element: self.root
        self.assertEqual(native.find_all(self.uia, self.root, roles=('combobox',)), [])
        self.assertNotIn(30011, hidden.reads)
        self.assertNotIn(30005, child.reads)

    def test_raw_walker_is_reused_only_in_the_same_uia_context(self):
        child = self.node()
        self.assertEqual(native.ancestry(self.uia, child, self.root), [child, self.root])
        self.assertEqual(native.ancestry(self.uia, child, self.root), [child, self.root])
        self.assertEqual(self.uia.raw_walker_requests, 1)


if __name__ == '__main__':
    unittest.main()
