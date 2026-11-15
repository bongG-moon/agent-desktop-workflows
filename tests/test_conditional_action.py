from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import desktop_select as app
import conditional_action as action
import grounding
import python_workflow as workflow
import row_action
import scene_session as scenes
import visual_target
from test_visual_target import image


class ConditionalExecutionTests(TestCase):
    target = (100,80)
    close = (35,37)
    box = [0,0,170,120]
    step = dict(action='conditional-open',label='Clear the popup if present, then always open the app.',
                seconds=10,when_present={'name':'notice'},dismiss={'name':'X'},always={'name':'Pocket'})
    report = dict(input_dispatched=False,steps=[])

    def run_scenes(self, branches, choices, error_after=None):
        session = mock.Mock()
        session.ask.side_effect=[dict(decision=b,point=p) for b,p in zip(branches,choices)]
        pixels=bytearray(image(self.target,self.close))
        for y in range(self.close[1]-15,self.close[1]+16):
            for x in range(self.close[0]-15,self.close[0]+16):
                index=(y*self.box[2]+x)*4
                pixels[index+2] ^= 128
        capture=grounding._png(*self.box[2:],pixels)
        sent=[]
        def click(binding,spec,guard,**unused):
            pos=visual_target._locate(*grounding._pixels(capture),spec)[1]
            sent.append(pos)
            return dict(status='delivered_unverified',diagnostic='visual_click_dispatched',input_dispatched=True)
        with ExitStack() as ctx:
            ctx.enter_context(mock.patch.object(row_action,'foreground'))
            wait=ctx.enter_context(mock.patch.object(action,'_wait'))
            ctx.enter_context(mock.patch.object(grounding,'_capture',
                side_effect=([ (self.box,capture),error_after ] if error_after else None),return_value=(self.box,capture)))
            ctx.enter_context(mock.patch.object(visual_target,'execute',side_effect=click))
            result=action.execute(None,self.step,lambda:None,session,self.report,3,'Demo')
        return result,sent,[c.args[0] for c in wait.call_args_list]

    def test_wait_and_open_exactly_once_without_ad(self):
        result,clicks,waits=self.run_scenes(['absent'],[self.target])
        self.assertEqual((result['condition'],result['diagnostic'],clicks,waits),
                         ('absent','conditional_open_dispatched',[list(self.target)],[10]))

    def test_ad_and_simultaneously_visible_game_require_dismiss_first(self):
        result,clicks,waits=self.run_scenes(['present','absent'],[self.close,self.target])
        self.assertEqual((result['condition'],clicks,waits),('present',[list(self.close),list(self.target)],[10,.35]))
        self.assertEqual([s['action'] for s in result['actions']],['dismiss','open'])

    def test_no_second_dismiss_no_guess_from_unreadable_screen(self):
        result,clicks,_=self.run_scenes(['present','present'],[self.close,self.close])
        self.assertEqual((result['status'],result['input_dispatched'],clicks),('blocked',True,[list(self.close)]))
        result,clicks,_=self.run_scenes(['unknown'],[None])
        self.assertEqual((result['status'],result['input_dispatched'],clicks),('blocked',False,[]))

    def test_observation_error_after_close_preserves_delivery(self):
        result,clicks,_=self.run_scenes(['present'],[self.close],app.bridge.BridgeError('window_changed'))
        self.assertEqual((result['diagnostic'],result['input_dispatched'],clicks),('window_changed',True,[list(self.close)]))

    def test_new_overlay_elsewhere_invalidates_absent_answer(self):
        original=grounding._png(*self.box[2:],image(self.target))
        other=grounding._png(*self.box[2:],image(self.target,self.close))
        with mock.patch.object(grounding,'_capture',return_value=(self.box,other)):
            with self.assertRaisesRegex(ValueError,'conditional_scene_changed'):
                action._fresh(None,self.box,original,lambda:None,app)

    def test_animated_panel_can_change_outside_a_stable_contextual_dismiss_target(self):
        import random
        width,height=320,220
        box=[0,0,width,height]
        pixels=bytearray(random.Random(3).randbytes(width*height*4))
        original=grounding._png(width,height,pixels)
        pixels[0:256]=bytes(256)
        animated=grounding._png(width,height,pixels)
        with mock.patch.object(grounding,'_capture',return_value=(box,animated)):
            action._fresh(None,box,original,lambda:None,app,dismiss_point=(210,130))
            with self.assertRaisesRegex(ValueError,'conditional_scene_changed'):
                action._fresh(None,box,original,lambda:None,app)
        index=(130*width+190)*4
        pixels[index:index+8]=bytes(8)
        changed_panel=grounding._png(width,height,pixels)
        with mock.patch.object(grounding,'_capture',return_value=(box,changed_panel)):
            with self.assertRaisesRegex(ValueError,'conditional_scene_changed'):
                action._fresh(None,box,original,lambda:None,app,dismiss_point=(210,130))

    def test_two_identical_games_in_same_scene_send_no_input(self):
        capture=grounding._png(*self.box[2:],image(self.target,self.close))
        session=mock.Mock()
        session.ask.return_value=dict(decision='absent',point=self.target)
        with mock.patch.object(grounding,'_capture',return_value=(self.box,capture)), \
                mock.patch.object(action,'_wait'), mock.patch.object(row_action,'foreground'), \
                mock.patch.object(visual_target,'execute') as send:
            result=action.execute(None,self.step,lambda:None,session,self.report,3,'Demo')
        self.assertEqual((result['input_dispatched'],result['diagnostic']),(False,'visual_target_ambiguous'))
        send.assert_not_called()

    def test_background_preflight_never_starts_worker_even_with_optional_click(self):
        recipe=dict(schema=1,name='Demo',exe='Demo.exe',window='Demo',steps=[self.step])
        with mock.patch.object(scenes,'start') as start:
            result=workflow.run(recipe,background_only=True)
        self.assertIs(result['input_dispatched'],False)
        self.assertEqual(result['blocked_steps'],[1])
        start.assert_not_called()

    def test_scene_answer_expired_duplicate_does_not_submit_again(self):
        with TemporaryDirectory() as td, mock.patch.object(scenes,'_root',return_value=Path(td)):
            ticket='7'*64
            folder,token=scenes._paths(ticket)
            folder.mkdir()
            pending=folder/('pending-'+token+'.json')
            pic=b'example'
            scenes._publish(folder/('frame-'+token+'.png'),pic)
            scenes._json(pending,dict(expires=scenes.time.time()+20,timeout=20,pid=scenes.os.getpid(),
                                      sha256=scenes.hashlib.sha256(pic).hexdigest()))
            with mock.patch.object(scenes,'_await',return_value={'status':'observed'}) as collect:
                result=scenes.answer(ticket,'absent',[100,80])
                self.assertEqual(result['status'],'observed')
                with self.assertRaisesRegex(ValueError,'already_used'):
                    scenes.answer(ticket,'absent',[100,80])
                collect.assert_called_once()
            pending.unlink()
            with self.assertRaisesRegex(ValueError,'expired_or_used'):
                scenes.answer(ticket,'present',[35,37])
