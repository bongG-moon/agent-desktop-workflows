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

    def test_old_draft_local_pick_resumes_once_and_saves_the_next_run(self):
        import grounding
        import local_result_review
        import python_workflow as flow
        import recorded_plan
        import review_drafts
        import teach_capture as capture
        from test_recorded_plan import button
        from test_visual_target import VisualTargets, image

        surface = dict(role='group', automation_id='results')
        child = dict(exe='A.exe', title='Details', related=True, _openers=['query', 'row'])
        query = dict(button(), id='query', label='조회')
        row = dict(capture.manual('double_click_needs_review', 'row'), result_surface=surface)
        drag = dict(capture.manual('drag_needs_review', 'memo'), _capture_window=child)
        close = dict(id='exit', label='닫기', action='click', _capture_window=child,
                     target=dict(kind='element', locator=dict(role='button', name='닫기')),
                     recognition=dict(target='identified', action='supported', result='manual'))
        raw = [(dict(self.raw, id='field', label='Region'), self.raw['instruction']['text']),
               (query, query['instruction']['text']), (row, '녹화한 항목 더블클릭 (조회 결과 행 더블클릭)'),
               (drag, '3단계에서 열린 창에서 memo부분 내용 추출'), (close, '닫기 버튼을 눌러줘')]
        name = '기존 녹화'
        review_drafts.ReviewStorage(name, 'A.exe', '업무 창', self.directory).save_draft(
            [dict(step=step, form=dict(alias='', text=sentence, mode=None, argument=''))
             for step, sentence in raw], 'instruction_target_unavailable', 4)
        original = (self.directory / (name + '.json')).read_bytes()
        rect = list(VisualTargets.window)
        box = dict(x=rect[0]+10, y=rect[1]+6, width=rect[2]+10, height=rect[3]-6)
        png = grounding._png(*rect[2:], image((35, 42)))
        frames = self.directory / 'frames'
        frames.mkdir()
        self.worker.side_effect = [self.worker.return_value, dict(status='verified', input_dispatched=True, steps=[
            dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True)),
            dict(number=2, receipt=dict(status='observed', input_dispatched=False, value='current memo')),
            dict(number=3, receipt=dict(status='verified', input_dispatched=True, diagnostic='recorded_window_closed'))])]
        args = ['--task', name, '--set', 'Region=North', '--local-result-review', '--allow-physical', '--summary']
        with patch.object(app.bridge, 'Binding'), patch.object(app.bridge, 'UIA') as uia, \
             patch.object(flow, '_result_target', return_value=(object(), surface)), \
             patch.object(grounding, '_capture', return_value=(rect, png)) as capture_frame, \
             patch.object(grounding, '_directory', return_value=frames), \
             patch.object(local_result_review, 'choose', return_value=dict(point=[35, 42], row_policy='recorded',
                                                                           confirmed=True)) as picker:
            uia.return_value.__enter__.return_value.bounds.return_value = box
            report = self.trial(*args)
            again = self.trial(*args)
        self.assertTrue(report.get('correction_saved'), report)
        self.assertFalse(report['prior_steps_replayed'])
        self.assertEqual(report['effective_task'], again['effective_task'])
        self.assertTrue(again['previous_trial'])
        self.assertFalse(again['this_call_dispatched_input'])
        self.assertEqual([s['source_step'] for s in report['steps']], [1, 2, 3, 4, 5])
        self.assertEqual(report['observations'][0]['value'], 'current memo')
        picker.assert_called_once()
        capture_frame.assert_called_once()
        self.assertEqual(self.worker.call_count, 2)
        suffix = self.worker.call_args.kwargs['payload']['recipe']['steps']
        self.assertEqual([s['action'] for s in suffix], ['visual-click', 'read-value', 'invoke'])
        self.assertEqual([s['window_context']['after_step'] for s in suffix[1:]], [1, 1])
        corrected = tasks.load_task(report['effective_task'], self.directory)
        recipe = recorded_plan.get_plan(corrected, self.directory)['envelope']['recipe']
        self.assertEqual(recipe['steps'][2]['result_point'], dict(x=25, y=36, width=180, height=114))
        self.assertEqual((recipe['steps'][2]['selector'], recipe['steps'][2]['result_row']), (surface, 'recorded'))
        self.assertEqual([s['window_context']['after_step'] for s in recipe['steps'][3:]], [3, 3])
        self.assertEqual((self.directory / (name + '.json')).read_bytes(), original)
        self.assertFalse(list(frames.glob('*.png')))


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
