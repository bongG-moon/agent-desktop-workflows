"""A legacy click can be taught from one observed window while preserving its source."""
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from test_learned_tasks import task, demo
import desktop_select as app
import grounding
import learned_tasks as tasks


class HostGroundingTests(TestCase):
    def test_draft_double_click_binds_in_place_in_new_copy_and_leaves_following_actions_manual(self):
        import random
        import review_drafts
        import teach_capture
        sentence = '나온 테이블 결과를 더블 클릭해서 팝업 창을 띄워줘'
        with TemporaryDirectory() as td:
            folder = Path(td)
            source = review_drafts.ReviewStorage('demo','Example.exe','Example',folder)
            rows = [dict(step=teach_capture.manual(reason, key), form=dict(alias='',text=text,mode=None,argument=''))
                    for reason,key,text in [('double_click_needs_review','cell',sentence),
                                            ('drag_needs_review','drag','팝업에서 COMMENT를 읽어줘')]]
            source.save_draft(rows,'invalid_step_instruction',1)
            source_bytes = (folder/'demo.json').read_bytes()
            binding = SimpleNamespace(hwnd=5,pid=42,created='100')
            args = SimpleNamespace(command='ground',task='demo',only='1',save_as='fixed',point=(52,47))
            data = grounding._png(120,95,random.Random(5).randbytes(120*95*4))
            with (mock.patch.object(app,'task_directory',return_value=folder),
                  mock.patch.object(grounding,'_directory',return_value=folder),
                  mock.patch.object(app,'windows',return_value=[]), mock.patch.object(app,'pick_window'),
                  mock.patch.object(app.bridge,'Binding',return_value=binding),
                  mock.patch.object(grounding,'WindowRoutes'),mock.patch.object(app,'guard'),
                  mock.patch.object(grounding,'_capture',return_value=([0,0,120,95],data)),
                  mock.patch.object(grounding.visual_target,'observe') as observe):
                frame = grounding.run(app,args)
                self.assertEqual(frame['gesture'],'double-click')
                self.assertNotIn('row_binding_command',frame)
                args.command,args.frame = 'bind-visual',frame['frame']
                observe.side_effect = grounding.visual_target.bridge.BridgeError('visual_target_ambiguous')
                repeat = grounding.run(app,args)
                self.assertEqual((repeat['host_action'],repeat['frame']), ('choose_distinct_point_in_same_target',frame['frame']))
                self.assertFalse((folder/'fixed.json').exists())
                observe.side_effect = None
                receipt = grounding.run(app,args)
                self.assertFalse(receipt['input_dispatched'])
            self.assertEqual(observe.call_args.args[1]['gesture'],'double-click')
            self.assertEqual((folder/'demo.json').read_bytes(),source_bytes)
            saved = tasks.load_task('fixed',folder)
            self.assertEqual((saved['steps'][0]['label'],saved['steps'][0]['visual']['gesture']), (sentence,'double-click'))
            self.assertEqual(saved['steps'][1]['action'],'manual')
            self.assertEqual(receipt['test_step_args'][4],'1')

    def test_image_round_trip_preserves_pixel_locations(self):
        import random
        rgb = bytearray(random.Random(7).randbytes(48*53*4))
        rgb[3::4]=bytes(48*53)
        box,pixels=grounding._pixels(grounding._png(48,53,rgb))
        self.assertEqual((box,pixels),([0,0,48,53],bytes(rgb)))

    def test_test_step_args_execute_only_bound_double_in_otherwise_invalid_draft(self):
        import random
        import review_drafts
        import teach_capture
        import recovery
        form = dict(alias='',text='이 행을 두 번 눌러 상세 창을 열어 줘',mode=None,argument='')
        raw = teach_capture.manual('double_click_needs_review','cell')
        bad = dict(step=teach_capture.manual('drag_needs_review','bad'),
                   form=dict(alias='=unfinished',text='아직 편집 중',mode='required',argument=''))
        with TemporaryDirectory() as td:
            folder = Path(td)
            review_drafts.ReviewStorage('demo','Example.exe','Example',folder).save_draft(
                [bad, dict(step=raw,form=form)],'invalid_step_instruction',1)
            owner = SimpleNamespace(hwnd=5,pid=42,created='100')
            data = grounding._png(120,95,random.Random(5).randbytes(120*95*4))
            with (mock.patch.object(app,'task_directory',return_value=folder),
                  mock.patch.object(grounding,'_directory',return_value=folder),
                  mock.patch.object(app,'windows',return_value=[]), mock.patch.object(app,'pick_window',return_value={'hwnd':5}),
                  mock.patch.object(app.bridge,'Binding',return_value=owner),mock.patch.object(app,'guard'),
                  mock.patch.object(grounding,'_capture',return_value=([0,0,120,95],data)),
                  mock.patch.object(grounding.visual_target,'observe'), mock.patch.object(app.coordination,'window'),
                  mock.patch.object(app,'worker_receipt',return_value=dict(status='delivered_unverified',
                       diagnostic='visual_double_click_dispatched',input_dispatched=True)) as send):
                def run(argv):
                    return app.run(app.parser().parse_args(argv))
                frame = run(['ground','--task','demo','--only','2'])['frame']
                bound = run(['bind-visual','--task','demo','--only','2','--frame',frame,'--point','52','47','--save-as','fixed'])
                self.assertTrue(bound['draft'])
                saved = tasks.load_task('fixed',folder,allow_review=True)
                self.assertEqual(saved['rows'][0],bad)
                self.assertEqual(run(bound['test_step_args']+['--background-only'])['input_dispatched'],False)
                with mock.patch.object(app,'attach_recovery',side_effect=lambda report,*a,**kw: report):
                    self.assertEqual(run(['replay','--task','fixed','--exe','Example.exe','--window','Example'])['status'],'needs_review')
                send.assert_not_called()
                report = run(bound['test_step_args'])
                self.assertEqual(report['tested_step'],2)
                self.assertEqual(report['steps'][0]['number'],2)
                self.assertEqual(send.call_args.kwargs['payload']['step']['visual']['gesture'],'double-click')
                send.assert_called_once()
                send.return_value = dict(status='blocked',diagnostic='visual_target_not_found',input_dispatched=False)
                missing = run(bound['test_step_args'])
                self.assertEqual(missing['ground_args'],['ground','--task','fixed','--only','2'])
                send.return_value = dict(status='uncertain',diagnostic='visual_target_not_found',input_dispatched=None)
                self.assertNotIn('ground_args',run(bound['test_step_args']))
                # An unresolved opener can never silently redirect a child double-click to main.
                saved['rows'][1]['step']['_capture_window'] = dict(exe='Example.exe',title='Other',after_id='bad',related=True)
                with self.assertRaisesRegex(tasks.TaskError,'window_origin_step_not_run'):
                    recovery.selected_draft_double(saved,'2')

    def test_bound_copy_requires_original_step_window_and_image(self):
        import random
        with TemporaryDirectory() as td:
            store=Path(td)
            plan=task(demo())
            sentence='카드 게임 열어줘'
            plan['steps']=[dict(tasks._manual('unsupported_recorded_action'), instruction_draft=sentence)]
            tasks.save_task(plan,store)
            original=(store/(plan['name']+'.json')).read_bytes()
            args=SimpleNamespace(command='ground',task=plan['name'],only='1',save_as='게임 열기',point=(52,47))
            binding=SimpleNamespace(hwnd=5,pid=42,created='100')
            data=grounding._png(120,95,random.Random(5).randbytes(120*95*4))
            with (mock.patch.object(app,'task_directory',return_value=store),
                  mock.patch.object(grounding,'_directory',return_value=store),
                  mock.patch.object(app,'windows',return_value=[]),mock.patch.object(app,'pick_window'),
                  mock.patch.object(app.bridge,'Binding',return_value=binding),
                  mock.patch.object(grounding,'WindowRoutes'),mock.patch.object(app,'guard'),
                  mock.patch.object(grounding,'_capture',return_value=([0,0,120,95],data)),
                  mock.patch.object(grounding.visual_target,'observe') as observe):
                report=grounding.run(app,args)
                self.assertFalse(report['input_dispatched'])
                args.command='bind-visual'
                args.frame=report['frame']
                binding.created='101'
                with self.assertRaisesRegex(tasks.TaskError,'grounding_frame_changed'):
                    grounding.run(app,args)
                self.assertFalse((store/(args.save_as+'.json')).exists())
                observe.assert_not_called()
                binding.created='100'
                report=grounding.run(app,args)
                observe.assert_called_once()
                self.assertFalse(report['input_dispatched'])
                saved=tasks.load_task(args.save_as,store)['steps'][0]
                self.assertEqual((saved['label'],saved['action']),(sentence,'visual-click'))
                self.assertEqual((store/(plan['name']+'.json')).read_bytes(),original)
                self.assertFalse((store/(args.frame+'.png')).exists())
