"""A taught field caption should include its local form without widening real surfaces."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import intent_replay as replay
import intent_tasks as intent
import learned_tasks as tasks


def node(path, role, identity='', name='', **extra):
    return dict(path=path, parent_path=path.rpartition('/')[0], role=role,
                automation_id=identity, name=name, enabled=True, secure=False, **extra)


class LabelScopeTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [node('0', 'window', 'Business'),
                      node('0/0', 'group', 'Filters'),
                      node('0/0/0', 'group', 'StateLabel', 'MES Setup'),
                      node('0/0/0/0', 'statictext', 'StateText', 'MES Setup'),
                      node('0/0/1', 'combobox', 'State'),
                      node('0/0/2', 'button', 'LocalQuery', 'Query'),
                      node('0/1', 'toolbar', 'Commands'),
                      node('0/1/0', 'button', 'OtherQuery', 'Query')]

    def step(self, role='group', identity='StateLabel'):
        return intent.make_step({'anchor': {'role': role, 'automation_id': identity}},
                                '이 라벨 옆 조건을 설정하고 조회해 줘')

    def test_label_group_includes_adjacent_field_in_named_form_only(self):
        scoped, indexes, anchor = replay.scope(self.nodes, self.step())
        field, _ = tasks.locate_target(scoped, dict(role='combobox', match='automation_id', target='State'))
        self.assertEqual(self.nodes[indexes[field]]['automation_id'], 'State')
        self.assertEqual((scoped[0]['automation_id'], anchor), ('Filters', 2))
        self.assertNotIn('OtherQuery', [n['automation_id'] for n in scoped])
        self.assertEqual(scoped[0]['parent_path'], '')
        self.assertEqual(self.nodes[1]['parent_path'], '0')

    def test_direct_statictext_anchor_skips_caption_wrappers(self):
        scoped, _, anchor = replay.scope(self.nodes, self.step('statictext', 'StateText'))
        self.assertEqual((scoped[0]['automation_id'], anchor), ('Filters', 3))
        self.assertIn('State', [n['automation_id'] for n in scoped])

    def test_caption_at_window_root_does_not_authorize_whole_window(self):
        nodes = [node('0', 'window', 'Business'),
                 node('0/0', 'group', 'StateLabel', 'MES Setup'),
                 node('0/0/0', 'statictext', 'StateText', 'MES Setup'),
                 node('0/1', 'combobox', 'State')]
        for step in (self.step(), self.step('statictext', 'StateText')):
            scoped, _, _ = replay.scope(nodes, step)
            self.assertNotIn('State', [n['automation_id'] for n in scoped])
            self.assertNotEqual(scoped[0]['role'], 'window')

    def test_real_surfaces_remain_their_own_region_even_with_one_caption(self):
        for role in ('table', 'custom', 'toolbar', 'list', 'tree', 'document', 'menu'):
            nodes = deepcopy(self.nodes)
            nodes[2]['role'] = role
            scoped, _, _ = replay.scope(nodes, self.step(role))
            self.assertEqual(scoped[0]['automation_id'], 'StateLabel')
            self.assertNotIn('State', [n['automation_id'] for n in scoped])

    def test_empty_result_group_and_one_cell_group_are_not_labels(self):
        for label, children in (('', False), ('Results', True), ('', True)):
            nodes = deepcopy(self.nodes)
            nodes[2]['name'] = label
            if not children:
                del nodes[3]
            scoped, _, _ = replay.scope(nodes, self.step())
            self.assertEqual(scoped[0]['automation_id'], 'StateLabel')
            self.assertNotIn('State', [n['automation_id'] for n in scoped])

    def test_protected_or_actionable_descendant_prevents_label_widening(self):
        variants = [dict(secure=True), dict(role='textfield'), dict(role='button'),
                    dict(available_actions=['Click'])]
        for change in variants:
            nodes = deepcopy(self.nodes)
            nodes[3].update(change)
            scoped, _, _ = replay.scope(nodes, self.step())
            self.assertEqual(scoped[0]['automation_id'], 'StateLabel')
            self.assertNotIn('State', [n['automation_id'] for n in scoped])

    def test_multiple_captions_are_content_instead_of_one_label(self):
        nodes = deepcopy(self.nodes)
        nodes.insert(4, node('0/0/0/1', 'statictext', 'OtherText', 'A different cell'))
        scoped, _, _ = replay.scope(nodes, self.step())
        self.assertEqual(scoped[0]['automation_id'], 'StateLabel')


if __name__ == '__main__':
    unittest.main()
