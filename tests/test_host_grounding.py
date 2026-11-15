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
    def test_image_round_trip_preserves_pixel_locations(self):
        import random
        rgb = bytearray(random.Random(7).randbytes(48*53*4))
        rgb[3::4]=bytes(48*53)
        box,pixels=grounding._pixels(grounding._png(48,53,rgb))
        self.assertEqual((box,pixels),([0,0,48,53],bytes(rgb)))

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
