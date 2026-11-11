"""Bounds, popup provenance and final-dispatch boundaries for context menu replay."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import context_menu as cm


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.spec = dict(target={'role':'table', 'automation_id':'data'}, point=[.5, .5], path=['Send','Copy'])
        self.uia = Mock()
        self.uia.same.side_effect = lambda a, b: a == b
        self.uia.within.side_effect = lambda a, b: False
        self.parent = cm._Item(10,'open-item','open-menu',(95,99,100,88),(97,101,95,20),'Send')
        self.leaf = cm._Item(20,'nested-item','nested-menu',(191,101,105,56),(192,102,100,25),'Copy')
        self.point = (100,100,1,1)

    def test_rejects_window_fallback_blank_labels_and_edges(self):
        for spec in (dict(self.spec,target={'role':'window','name':'Main'}),
                     dict(self.spec,point=[True,.5]), dict(self.spec,point=[.01,.3]),
                     dict(self.spec,path=['']), dict(self.spec,target={'role':'group','automation_id':'29'})):
            with self.subTest(spec=spec), self.assertRaises(cm.win.BridgeError):
                cm.validate_spec(spec)

    def test_preexisting_or_unrelated_menu_is_never_selected(self):
        choose = lambda items, old: cm._choose(self.uia, items, old,'Send',self.point,None,8)
        self.assertIsNone(choose([self.parent],[self.parent]))
        distant = cm._Item(20,'other','unrelated',(500,500,100,30),(500,500,40,20),'Send')
        self.assertIsNone(choose([distant],[]))
        self.assertEqual(self.parent,choose([distant,self.parent],[]))
        duplicate = cm._Item(30,'twin','twin-menu',self.parent.menu_box,self.parent.box,'Send')
        with self.assertRaisesRegex(cm.win.BridgeError,'item_ambiguous'):
            choose([self.parent,duplicate],[])

    def test_submenu_must_be_new_child_or_adjacent_new_popup(self):
        def choose(item):
            return cm._choose(self.uia, [self.parent,item], [self.parent], 'Copy',
                              self.parent.box, self.parent, 8)
        old_frame = cm._Item(10,'random','open-menu',self.parent.menu_box,self.leaf.box,'Copy')
        self.assertIsNone(choose(old_frame))
        self.assertEqual(self.leaf,choose(self.leaf))
        self.uia.within.side_effect = lambda a,b: (a,b)==('random','open-item')
        self.assertEqual(old_frame,choose(old_frame))

    def run_path(self, *, before_leaf=None, receipts=None, guard=None):
        def apply(p):
            value = p.start()
            self.addCleanup(p.stop)
            return value
        apply(patch.object(cm,'foreground'))
        obj = MagicMock()
        obj.__enter__.return_value = self.uia
        apply(patch.object(cm.win,'UIA',return_value=obj))
        apply(patch.object(cm,'_region',return_value='region'))
        apply(patch.object(cm,'_rect',return_value=(0,0,200,200)))
        apply(patch.object(cm,'_hit_region'))
        apply(patch.object(cm,'_hit_item',return_value=37))
        apply(patch.object(cm.win,'_combo_surface'))
        apply(patch.object(cm.win,'root_window',return_value=37))
        apply(patch.object(cm.win,'window_from_point',return_value=37))
        apply(patch.object(cm,'_menus',side_effect=[[],[self.parent],[self.parent],
                                                   [self.parent,self.leaf],[self.parent,self.leaf]]))
        send = apply(patch.object(cm.win,'_send_point',side_effect=receipts,
                                  return_value={'ok':True,'input_dispatched':True}))
        binding = Mock()
        binding.hwnd = 37
        binding.scoped_windows.return_value = [37]
        binding.info.return_value = {'dpi':96}
        result = cm.execute(binding,self.spec,guard or Mock(),before_leaf=before_leaf)
        return result, send

    def test_leaf_delivery_never_claims_file_verification(self):
        baseline = Mock()
        result, send = self.run_path(before_leaf=baseline)
        self.assertEqual([('right-click',),('click',),('click',)], [c.args[-1:] for c in send.call_args_list])
        baseline.assert_called_once_with()
        self.assertEqual(('delivered_unverified',True,['Send','Copy']),
                         (result['status'],result['leaf_dispatched'],result['menu_items']))

    def test_changed_output_baseline_stops_before_leaf(self):
        result, send = self.run_path(before_leaf=Mock(side_effect=cm.win.BridgeError('output_already_exists')))
        self.assertEqual(2,send.call_count)
        self.assertEqual(('blocked',True,False,['Send']),
                         (result['status'],result['input_dispatched'],result['leaf_dispatched'],result['menu_items']))

    def test_partial_right_click_and_leaf_delivery_not_retried(self):
        yes = {'ok':True,'input_dispatched':True}
        unknown = {'ok':False,'input_dispatched':None}
        result,send = self.run_path(receipts=[yes,yes,unknown])
        self.assertEqual(3,send.call_count)
        self.assertEqual(('uncertain',None,False),(result['status'],result['leaf_dispatched'],result['menu_path_completed']))

    def test_stale_popup_item_with_recreated_accessibility_peer_is_rejected(self):
        new = cm._Item(self.parent.surface,'proxy2','frame2',self.parent.menu_box,self.parent.box,'SEND')
        self.assertIsNone(cm._choose(self.uia,[new],[self.parent],'Send',self.point,None,8))

    def test_other_owned_popup_covering_item_is_not_its_menu(self):
        binding = Mock(hwnd=1,pid=27)
        item = Mock(surface=10,box=(50,50,100,25),center=(100,62))
        item.element.property.side_effect = lambda i: {30010:True,30022:False}[i]
        self.uia.pid.return_value = 27
        self.uia.secure.return_value = False
        self.uia.bounds.return_value = dict(x=50,y=50,width=100,height=25)
        with (patch.object(cm.win,'window_from_point',return_value=30),
              patch.object(cm.win,'foreground',return_value=1),
              patch.object(cm.win,'root_window',return_value=30),
              patch.object(cm,'_rect',return_value=item.box)):
            with self.assertRaisesRegex(cm.win.BridgeError,'context_menu_occluded'):
                cm._hit_item(binding,self.uia,item,None,{1,10},12)

    def test_container_hit_must_not_hide_protected_overlay_child(self):
        owner, region, overlay = Mock(pid=27), Mock(), Mock()
        self.uia.children.side_effect = lambda e: [overlay] if e is region else []
        self.uia.pid.return_value = 27
        self.uia.secure.return_value = True
        with patch.object(cm,'_rect',return_value=(20,30,20,20)):
            with self.assertRaisesRegex(cm.win.BridgeError,'context_point_protected'):
                cm._check_region_children(owner,self.uia,region,(25,35))


if __name__ == '__main__':
    unittest.main()
