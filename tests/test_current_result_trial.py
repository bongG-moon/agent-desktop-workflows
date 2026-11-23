from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import first_trial
import input_policy
import learned_tasks as tasks
import result_trial
import test_first_trial as fixtures


class CurrentResultTrial(unittest.TestCase):
    trial = fixtures.FirstTrial.trial

    def setUp(self):
        fixtures.FirstTrial.setUp(self)
        child = dict(exe='A.exe', title='Details', related=True, after_step=3)
        self.task['steps'] += [
            dict(action='double-click-result', result_row='single', selector={'role':'group','automation_id':'results'}, label='결과 행'),
            dict(action='read-value', selector={'role':'textfield','automation_id':'txtComment'}, label='Comment', window_context=child),
            dict(action='close-popup', selector={'role':'button','automation_id':'exit'}, label='Exit', window_context=child)]
        self.worker.return_value = dict(status='blocked', stopped_at=3, input_dispatched=True,
            steps=[dict(number=1,receipt=dict(status='verified',input_dispatched=True)),
                   dict(number=2,receipt=dict(status='delivered_unverified',input_dispatched=True)),
                   dict(number=3,receipt=dict(status='blocked',input_dispatched=False,
                       diagnostic='result_row_unavailable',result_window=dict(hwnd=7,pid=2,title='업무 창',exe_name='A.exe'),
                       result_surface_selector={'role':'group','automation_id':'results'}))])
        tasks.save_task(self.task,self.directory)

    def test_opaque_current_row_offers_one_resume_without_expected_value(self):
        report = self.trial('--task',self.task['name'],'--set','Region=North','--allow-physical')
        self.assertEqual(report['host_action'],'inspect_current_result_without_replaying')
        self.assertEqual(report['result_inspection']['result_row'],'single')
        self.assertNotIn('expected_hash',report['result_inspection'])
        self.assertTrue(report['result_inspection']['resume_supported'])
        repeat = self.trial('--task',self.task['name'],'--set','Region=North','--allow-physical')
        self.assertEqual(repeat['trial_receipt'],report['trial_receipt'])
        self.worker.assert_called_once()

    def test_empty_or_multiple_rows_do_not_offer_images(self):
        for diagnostic in ('result_row_not_found','result_row_ambiguous'):
            self.worker.return_value['steps'][-1]['receipt']['diagnostic']=diagnostic
            report=self.trial('--task',self.task['name'],'--set','Region=North','--allow-physical','--trial-id',diagnostic)
            self.assertEqual(report['host_action'],'report_unmatched_result')
            self.assertNotIn('result_inspection',report)

    def test_locked_desktop_does_not_reserve_or_dispatch_a_trial(self):
        blocked=dict(status='blocked',diagnostic='no_interactive_desktop',input_dispatched=False)
        with patch.object(input_policy,'interactive_desktop_blocked',return_value=blocked):
            report=self.trial('--task',self.task['name'],'--set','Region=North','--allow-physical')
        self.assertEqual(report,blocked)
        self.worker.assert_not_called()
        self.assertFalse((self.directory/'first-trials').exists())

    def test_worker_lock_race_can_retry_only_zero_input(self):
        callback=Mock(side_effect=[dict(status='blocked',diagnostic='no_interactive_desktop',input_dispatched=False),
                                   dict(status='verified',input_dispatched=True)])
        first_trial._once(self.directory,'race',callback)
        self.assertFalse((self.directory/'first-trials/race.json').exists())
        first_trial._once(self.directory,'race',callback)
        again=first_trial._once(self.directory,'race',callback)
        self.assertTrue(again['previous_trial'])
        self.assertEqual(callback.call_count,2)

    def test_one_frame_resume_uses_current_table_and_never_replays_query(self):
        import grounding
        import python_workflow as flow
        from test_visual_target import VisualTargets, image
        report=self.trial('--task',self.task['name'],'--set','Region=North','--allow-physical')
        token=report['trial_receipt']
        rect=list(VisualTargets.window)
        png=grounding._png(*rect[2:],image((35,42)))
        box=dict(zip(('x','y','width','height'),rect))
        uia=Mock()
        uia.__enter__=Mock(return_value=uia)
        uia.__exit__=Mock(return_value=False)
        uia.bounds.return_value=box
        frames=self.directory/'frames'
        frames.mkdir()
        with patch.object(app.bridge,'Binding',return_value=Mock()), patch.object(app.bridge,'UIA',return_value=uia), \
             patch.object(flow,'_result_target',autospec=True,return_value=(Mock(),{'role':'group','automation_id':'results'})), \
             patch.object(grounding,'_capture',return_value=(rect,png)), patch.object(grounding,'_directory',return_value=frames):
            captured=first_trial.inspect_result(app,token)
            self.assertEqual(captured['result_row'],'single')
            self.worker.assert_called_once()
            self.worker.return_value=dict(status='completed_unverified',input_dispatched=True,steps=[
                dict(number=1,receipt=dict(status='delivered_unverified',input_dispatched=True)),
                dict(number=2,receipt=dict(status='observed',input_dispatched=False,value='current memo')),
                dict(number=3,receipt=dict(status='verified',input_dispatched=True))])
            args=app.parser().parse_args(['resume-trial','--receipt',token,'--frame',captured['frame'],
                                        '--row-policy','single','--point','35','42','--allow-physical'])
            with patch.object(app,'worker_receipt',self.worker):
                resumed=result_trial.resume(app,args)
                again=result_trial.resume(app,args)
        suffix=self.worker.call_args.kwargs['payload']['recipe']['steps']
        self.assertEqual([s['action'] for s in suffix],['visual-click','read-value','invoke'])
        self.assertEqual(self.worker.call_count,2)
        self.assertEqual([r['number'] for r in resumed['steps']],[1,2,3,4,5])
        self.assertFalse(resumed['prior_steps_replayed'])
        self.assertEqual(resumed['host_action'],'report_completed_trial')
        self.assertEqual(again['observations'][0]['value'],'current memo')


class ResultRowSuffix(unittest.TestCase):
    def test_row_suffix_remaps_detail_opener_and_preserves_source(self):
        from test_visual_target import VisualTargets, image
        import grounding
        child=dict(exe='A.exe',title='Details',related=True,after_step=2)
        recipe=dict(schema=1,name='조회',exe='A.exe',window='Main',steps=[
            dict(action='invoke',selector={'role':'button','automation_id':'query'}),
            dict(action='double-click-result',result_row='single',selector={'role':'group','automation_id':'grid'},label='결과'),
            dict(action='read-value',selector={'role':'textfield','automation_id':'memo'},label='Comment',window_context=child),
            dict(action='invoke',selector={'role':'button','automation_id':'exit'},verify_closed=True,window_context=child)])
        before=deepcopy(recipe)
        png=grounding._png(*VisualTargets.window[2:],image((35,42)))
        suffix=result_trial.visual_suffix(recipe,2,png,[35,42],reviewed=True)
        self.assertEqual(len(suffix['steps']),3)
        self.assertEqual(suffix['steps'][0]['action'],'visual-click')
        self.assertEqual(suffix['steps'][1]['window_context']['after_step'],1)
        self.assertEqual(recipe,before)


if __name__ == '__main__':
    unittest.main()
