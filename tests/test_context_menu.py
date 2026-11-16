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

    def teach_in_groups(self, identities, resolve, **kwargs):
        from contextlib import nullcontext
        child, outer, root = 'inner', 'table', 'root'
        scope = Mock(hwnd=10, pid=42)
        self.uia.from_handle.return_value = root
        self.uia.from_point.return_value = child
        self.uia.within.return_value = True
        self.uia.within.side_effect = None
        self.uia.pid.return_value = scope.pid
        self.uia.secure.return_value = False
        self.uia.point_descendant.return_value = child
        self.uia.basic.side_effect = identities.__getitem__
        self.uia.parent.side_effect = {child: outer, outer: root}.__getitem__
        with (patch.object(cm.win, 'UIA', return_value=nullcontext(self.uia)),
              patch.object(cm, '_region', side_effect=resolve),
              patch.object(cm, '_hit_region') as hit,
              patch.object(cm, '_rect', return_value=(0, 0, 200, 100))):
            anchor = cm.teach_surface(scope, (80, 40), **kwargs)
            hit.assert_called_once()
        return anchor

    def test_record_surface_uses_unique_name_when_inner_id_is_transient(self):
        identities = {'inner': dict(role='group', automation_id='1393', name='Results panel'),
                      'table': dict(role='group', name='')}
        def resolve(scope, uia, selector):
            self.assertEqual(selector, dict(role='group', name='Results panel'))
            return 'inner'
        anchor = self.teach_in_groups(identities, resolve)
        self.assertEqual(anchor, dict(target=dict(role='group', name='Results panel'), point=[.4, .4]))

    def test_named_outer_surface_survives_the_current_row_caption(self):
        identities = {'inner': dict(role='group', automation_id='1393', name='Row value at row 2'),
                      'table': dict(role='group', automation_id='9184', name='Results')}
        def resolve(scope, uia, selector):
            self.assertEqual(selector, dict(role='group', name='Results'))
            return 'table'
        anchor = self.teach_in_groups(identities, resolve)
        self.assertEqual(anchor['target'], dict(role='group', name='Results'))

    def test_shell_wide_wrapper_does_not_replace_the_results_region(self):
        from contextlib import nullcontext
        scope = Mock(hwnd=10, pid=42)
        self.uia.from_handle.return_value = 'root'
        self.uia.from_point.return_value = 'row'
        self.uia.point_descendant.return_value = 'row'
        self.uia.pid.return_value = 42
        self.uia.secure.return_value = False
        self.uia.within.side_effect = None
        self.uia.within.return_value = True
        self.uia.basic.side_effect = lambda item: dict(role='group', name={
            'row': 'Device A at row 1', 'results': 'Results', 'shell': 'Application'}[item])
        self.uia.parent.side_effect = {'row':'results', 'results':'shell', 'shell':'root'}.__getitem__
        bounds = {'root': (0,0,1000,700), 'shell': (0,100,990,590),
                  'results': (100,100,600,400), 'row': (120,150,560,30)}
        self.uia.bounds.side_effect = lambda item: dict(zip(('x','y','width','height'), bounds[item]))
        with (patch.object(cm.win, 'UIA', return_value=nullcontext(self.uia)),
              patch.object(cm, '_region', return_value='results') as lookup,
              patch.object(cm, '_hit_region'), patch.object(cm, '_rect', return_value=bounds['results'])):
            anchor = cm.teach_surface(scope, (300,160))
        self.assertEqual(anchor['target'], dict(role='group',name='Results'))
        lookup.assert_called_once_with(scope, self.uia, dict(role='group',name='Results'))

    def test_ordinary_point_can_resolve_parent_through_repeated_nested_groups(self):
        identities = {'inner': dict(role='group', automation_id='repeated', name='Repeated'),
                      'table': dict(role='table', automation_id='Grid')}
        def resolve(scope, uia, selector):
            if selector['role'] == 'group':
                raise cm.win.BridgeError('context_region_ambiguous')
            return 'table'
        anchor = self.teach_in_groups(identities, resolve)
        self.assertEqual(anchor['target'], dict(role='table', automation_id='Grid'))
        with self.assertRaisesRegex(cm.win.BridgeError, 'context_region_ambiguous'):
            self.teach_in_groups(identities, resolve, target='Repeated')
        protected = Mock(side_effect=cm.win.BridgeError('target_protected'))
        with self.assertRaisesRegex(cm.win.BridgeError, 'target_protected'):
            self.teach_in_groups(identities, protected)
        protected.assert_called_once()

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

    def run_path(self, *, before_leaf=None, receipts=None, guard=None, ticks=None):
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
        apply(patch.object(cm.win,'input_tick',return_value=900,side_effect=ticks))
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

    def test_user_input_while_leaf_is_resolved_stops_without_clicking_it(self):
        result, send = self.run_path(ticks=[40, 40, 41, 41, 42, 43])
        self.assertEqual(send.call_count, 2)
        self.assertEqual((result['diagnostic'], result['input_dispatched'], result['leaf_dispatched']),
                         ('foreground_or_input_changed', True, False))

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

    def test_preview_distinguishes_table_and_actual_point_without_input(self):
        failure = cm.win.BridgeError('context_point_outside_region')
        with (patch.object(cm, '_region', return_value='table'),
              patch.object(cm, '_rect', return_value=(0, 0, 200, 200)),
              patch.object(cm, '_hit_region', side_effect=failure) as hit,
              patch.object(cm.win, '_send_point') as send):
            preview = cm.observe(Mock(), self.uia, self.spec)
        self.assertTrue(preview['region_observed'])
        self.assertFalse(preview['point_observed'])
        self.assertEqual(preview['point_diagnostic'], failure.code)
        self.assertNotIn('proposed_point', preview)
        self.assertEqual(hit.call_count, 1)
        send.assert_not_called()

    def test_proposed_replacement_requires_explicit_table_wide_intent_and_a_data_hit(self):
        spec = dict(self.spec, point_policy='any_data_cell')
        def hit(binding, uia, region, point, *, require_body, recorded_opaque=False):
            self.assertTrue(require_body)
            if point != (100, 150):
                raise cm.win.BridgeError('context_point_not_data')
        with (patch.object(cm, '_region', return_value='table'),
              patch.object(cm, '_rect', return_value=(0, 0, 200, 200)),
              patch.object(cm, '_hit_region', side_effect=hit),
              patch.object(cm.win, '_send_point') as send):
            preview = cm.observe(Mock(), self.uia, spec)
        self.assertEqual(preview['proposed_point'], [.5, .75])
        self.assertTrue(preview['point_observed'])
        self.assertEqual(spec['point'], [.5,.5])
        send.assert_not_called()

    def test_occlusion_and_protected_points_abort_automatic_relocation(self):
        spec = dict(self.spec, point_policy='any_data_cell')
        for code in ('context_point_occluded', 'context_point_protected', 'context_point_disabled'):
            with (self.subTest(code=code), patch.object(cm, '_region', return_value='table'),
                  patch.object(cm, '_rect', return_value=(0, 0, 200, 200)),
                  patch.object(cm, '_hit_region', side_effect=cm.win.BridgeError(code)) as hit):
                preview = cm.observe(Mock(), self.uia, spec)
                self.assertEqual(preview['point_diagnostic'], code)
                self.assertFalse(preview['point_observed'])
                self.assertEqual(hit.call_count, 1)

    def test_correction_is_reported_only_after_complete_exact_menu_path(self):
        correction = {'from': [.5,.5], 'to': [.45,.5]}
        with patch.object(cm, '_resolve_menu_point', return_value=((0,0,200,200), (90,100), correction)):
            result, _ = self.run_path()
        self.assertEqual(result['point_correction'], correction)
        with patch.object(cm, '_resolve_menu_point', return_value=((0,0,200,200), (90,100), correction)):
            failure, _ = self.run_path(before_leaf=Mock(side_effect=cm.win.BridgeError('window_changed')))
        self.assertNotIn('point_correction', failure)

    def test_data_cell_does_not_authorize_an_embedded_button_or_header(self):
        owner = Mock(hwnd=10, pid=42)
        row, region = Mock(), Mock()
        role = {row: 50029, region: 50028}
        self.uia.within.side_effect = None
        self.uia.within.return_value = True
        self.uia.pid.return_value = 42
        self.uia.secure.return_value = False
        for leaf_role, ok in ((50020, True), (50000, False), (50035, False), (50014, False)):
            leaf = Mock()
            role[leaf] = leaf_role
            for node in (leaf, row, region):
                node.property.side_effect = lambda prop, n=node: {30003: role[n], 30010: True}.get(prop)
            self.uia.from_point.return_value = leaf
            self.uia.parent.side_effect = {leaf: row, row: region}.__getitem__
            with (self.subTest(role=leaf_role), patch.object(cm.win, 'window_from_point', return_value=10),
                  patch.object(cm.win, 'root_window', return_value=10), patch.object(cm.win, 'hwnd_pid',return_value=42),
                  patch.object(cm, '_check_region_children'), patch.object(cm, '_rect', return_value=(0,0,100,100))):
                if ok:
                    cm._hit_region(owner, self.uia, region,(30,30),require_body=True)
                else:
                    with self.assertRaisesRegex(cm.win.BridgeError, 'context_point_not_data'):
                        cm._hit_region(owner, self.uia, region,(30,30),require_body=True)

    def test_visual_repair_changes_point_only_after_an_approved_frame(self):
        import grounding
        a = bytearray([255] * (100*100*4))
        a[4*(45*100 + 60)] = 0
        png = grounding._png(100, 100, a)
        scope = Mock(hwnd=37, pid=42)
        with (patch.object(cm, '_region', return_value='data'),
              patch.object(cm, '_rect', return_value=(100,200,100,100)),
              patch.object(cm, '_hit_region', side_effect=cm.win.BridgeError('context_data_cell_unconfirmed')),
              patch.object(cm, '_visual_point') as visual,
              patch.object(cm.win, 'window_from_point', return_value=37),
              patch.object(cm.win, 'root_window', return_value=37),
              patch.object(cm.win, 'hwnd_pid', return_value=42),
              patch.object(grounding,'_capture') as capture):
            revised = cm.adjust_from_frame(scope, self.uia, self.spec, (160,245),window=[100,200,100,100],png=png)
            self.assertEqual(revised['point'], [.6,.45])
            self.assertNotIn('visual_patch', self.spec)
            self.assertEqual(len(revised['visual_patch']), 64)
            self.assertEqual(cm.observe(scope, self.uia, revised)['point_diagnostic'], 'context_visual_check_deferred')
            capture.assert_not_called()
            capture.return_value = ([100,200,100,100],png)
            cm._verify_menu_point(scope,self.uia,'data',(160,245),revised,allow_capture=True)
            a[4*(45*100 + 61)] = 0
            capture.return_value = ([100,200,100,100],grounding._png(100,100,a))
            with self.assertRaisesRegex(cm.win.BridgeError, 'context_visual_patch_changed'):
                cm._verify_menu_point(scope,self.uia,'data',(160,245),revised,allow_capture=True)
            self.assertEqual(visual.call_count, 2)

    def test_directly_hit_opaque_surface_preserves_recorded_point_but_is_not_a_repair_candidate(self):
        scope = Mock(hwnd=1, pid=2)
        region = Mock()
        region.property.return_value = True
        self.uia.pid.return_value = 2
        self.uia.secure.return_value = False
        self.uia.within.side_effect = lambda a,b: a is b
        self.uia.from_point.return_value = region
        with (patch.object(cm.win, 'window_from_point',return_value=1),
              patch.object(cm.win,'root_window',return_value=1),
              patch.object(cm.win,'hwnd_pid',return_value=2), patch.object(cm,'_rect',return_value=(0,0,200,100)),
              patch.object(cm,'_check_region_children') as children):
            cm._hit_region(scope, self.uia, region, (100,50),require_body=True,recorded_opaque=True)
            children.assert_called_once_with(scope, self.uia, region, (100,50),data_only=True,hit_element=region)
            with self.assertRaisesRegex(cm.win.BridgeError, 'context_data_cell_unconfirmed'):
                cm._hit_region(scope, self.uia, region, (100,50),require_body=True)

    def test_tab_container_is_transparent_only_above_the_observed_data_hit(self):
        scope = Mock(hwnd=1,pid=7)
        region,tabs,cell,header,other = [Mock() for _ in range(5)]
        for node,role in ((region,50033),(tabs,50018),(cell,50033),(header,50019),(other,50018)):
            node.property.side_effect = lambda key,role=role: {30003:role,30010:True}.get(key)
        parents = {cell:tabs,tabs:region,header:tabs,other:region}
        def within(node,ancestor):
            for _ in range(10):
                if node is ancestor:return True
                node=parents.get(node)
                if node is None:return False
            return False
        self.uia.same.side_effect = lambda a,b:a is b
        self.uia.within.side_effect = within
        self.uia.from_point.return_value = cell
        self.uia.from_handle.return_value = region
        self.uia.pid.return_value = 7
        self.uia.secure.return_value = False
        self.uia.parent.side_effect = parents.get
        branches = {region:[tabs],tabs:[cell,header]}
        self.uia.children.side_effect = lambda node: branches.get(node,[])
        self.uia.bounds.side_effect = lambda node: dict(x=0,y=0,width=200,height=20 if node is header else 100)
        with (patch.object(cm.win,'window_from_point',return_value=10),
              patch.object(cm.win,'root_window',return_value=1),
              patch.object(cm.win,'hwnd_pid',return_value=7),
              patch.object(cm.lookup,'ancestry',return_value=[cell,tabs,region])):
            cm._hit_region(scope,self.uia,region,(100,60),require_body=True,recorded_opaque=True)
            cm._visual_point(scope,self.uia,region,(100,60))
            for hit in (tabs,header):
                self.uia.from_point.return_value=hit
                with self.assertRaisesRegex(cm.win.BridgeError,'context_point_not_data'):
                    cm._hit_region(scope,self.uia,region,(100,60),require_body=True,recorded_opaque=True)
            self.uia.from_point.return_value=cell
            branches[region].append(other)
            with self.assertRaisesRegex(cm.win.BridgeError,'context_point_not_data'):
                cm._hit_region(scope,self.uia,region,(100,60),require_body=True,recorded_opaque=True)
            with self.assertRaisesRegex(cm.win.BridgeError,'context_point_not_data'):
                cm._visual_point(scope,self.uia,region,(100,60))

    def test_blank_menu_caption_uses_owned_legacy_name_or_its_single_visible_label(self):
        scope = Mock(pid=8)
        item, child = Mock(), Mock()
        self.uia.pid.return_value = 8
        self.uia.secure.return_value = False
        self.uia.children.return_value = [child]
        box = (100, 50, 120, 20)
        child.property.side_effect = {30003:50020, 30005:'Workbook'}.get
        with patch.object(cm,'_menu_visible',return_value=True), patch.object(cm,'_rect',return_value=box):
            item.property.side_effect = {30005:'',30092:'Workbook'}.get
            self.assertEqual(cm._menu_caption(scope,self.uia,item,box),'Workbook')
            item.property.side_effect = {30005:'',30092:'Another label'}.get
            self.assertIsNone(cm._menu_caption(scope,self.uia,item,box))
            item.property.side_effect = {30005:'',30092:''}.get
            self.assertEqual(cm._menu_caption(scope,self.uia,item,box),'Workbook')
            self.uia.secure.return_value = True
            self.assertIsNone(cm._menu_caption(scope,self.uia,item,box))
            self.uia.secure.return_value = False
            self.uia.pid.return_value = 99
            self.assertIsNone(cm._menu_caption(scope,self.uia,item,box))

    def test_unreadable_menu_handoff_reports_only_new_nearby_owned_popup(self):
        scope = Mock(hwnd=1,pid=7)
        root = Mock()
        root.property.return_value = cm.MENU
        self.uia.from_handle.return_value = root
        self.uia.pid.return_value = 7
        self.uia.secure.return_value = False
        with (patch.object(cm,'_scoped_windows',return_value=[1,3,4]), patch.object(cm,'_rect',return_value=(90,80,90,60)),
              patch.object(cm.win.user32,'IsWindowVisible',return_value=True)):
            trace = cm._unresolved_menu(scope,self.uia,['Workbook'],{1,3},(90,80,1,1),8)
            self.assertEqual((trace['host_action'], trace['observed_popup']['hwnd']), ('inspect_open_menu', 4))
            self.assertFalse(trace['repeat_right_click'])
            multiple = cm._unresolved_menu(scope,self.uia,['Workbook'],{1},(90,80,1,1),8)
            self.assertNotIn('observed_popup', multiple)


if __name__ == '__main__':
    unittest.main()
