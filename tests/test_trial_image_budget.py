"""Keep live proof current while letting the host avoid rereading identical images."""
from contextlib import ExitStack
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch

import test_current_result_trial as fixture
import grounding
import python_workflow as flow
import test_visual_target as visual_fixture

app, first_trial = fixture.app, fixture.first_trial


class TrialImageBudget(unittest.TestCase):
    setUp = fixture.CurrentResultTrial.setUp
    trial = fixture.CurrentResultTrial.trial

    def inspection(self, *, value_mode=False):
        report = self.trial('--task', self.task['name'], '--set', 'Region=North', '--allow-physical')
        token = report['trial_receipt']
        if value_mode:
            path = self.directory / 'first-trials' / (token + '.json')
            report['result_inspection'].pop('result_row')
            report['result_inspection'].update(column='Part', resume_supported=False)
            path.write_text(json.dumps(report), encoding='utf8')
        rect = list(visual_fixture.VisualTargets.window)
        png = grounding._png(*rect[2:], visual_fixture.image((35, 42)))
        box = dict(zip(('x', 'y', 'width', 'height'), rect))
        uia = Mock()
        uia.__enter__ = Mock(return_value=uia)
        uia.__exit__ = Mock(return_value=False)
        uia.bounds.return_value = box
        frames = self.directory / 'frames'
        frames.mkdir()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(app.bridge, 'Binding', return_value=Mock()))
        stack.enter_context(patch.object(app.bridge, 'UIA', return_value=uia))
        stack.enter_context(patch.object(flow, '_result_target', autospec=True,
                                        return_value=(Mock(), {'role': 'group', 'automation_id': 'results'})))
        capture = stack.enter_context(patch.object(grounding, '_capture', return_value=(rect, png)))
        stack.enter_context(patch.object(grounding, '_directory', return_value=frames))
        return token, rect, png, capture

    def test_same_pixels_are_explicitly_reusable_but_current_capture_is_not_skipped(self):
        token, rect, png, capture = self.inspection()
        first = first_trial.inspect_result(app, token)
        second = first_trial.inspect_result(app, token)
        self.assertEqual(first['image_sha256'], sha256(png).hexdigest())
        self.assertFalse(first['image_unchanged'])
        self.assertEqual(first['image_review'], 'review_current_image')
        self.assertTrue(second['image_unchanged'])
        self.assertEqual(second['image_review'], 'reuse_if_already_seen')
        self.assertEqual(first['frame'], second['frame'])
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(first['resume_args'], second['resume_args'])
        self.worker.assert_called_once()

    def test_async_pixel_change_requires_new_review_even_without_input(self):
        token, rect, png, capture = self.inspection()
        first = first_trial.inspect_result(app, token)
        changed = grounding._png(*rect[2:], visual_fixture.image((50, 50)))
        capture.return_value = (rect, changed)
        second = first_trial.inspect_result(app, token)
        self.assertNotEqual(first['image_sha256'], second['image_sha256'])
        self.assertFalse(second['image_unchanged'])
        self.assertEqual(second['image_review'], 'review_current_image')
        self.assertNotEqual(first['frame'], second['frame'])
        self.worker.assert_called_once()

    def test_value_review_also_tracks_image_identity_without_changing_expiration(self):
        token, rect, png, capture = self.inspection(value_mode=True)
        path = self.directory / 'first-trials' / (token + '.json')
        before = json.loads(path.read_text(encoding='utf8'))
        first_trial.inspect_result(app, token)
        second = first_trial.inspect_result(app, token)
        after = json.loads(path.read_text(encoding='utf8'))
        self.assertTrue(second['image_unchanged'])
        self.assertEqual(before['result_inspection']['observed_at'], after['result_inspection']['observed_at'])
        self.assertEqual(before['steps'], after['steps'])
        self.assertNotIn('frame_geometry', after['result_inspection'])


class GeneratedRowGuideBudget(unittest.TestCase):
    def recipe(self):
        return dict(schema=1, name='Open result', exe='App.exe', window='Records', steps=[
            dict(action='double-click-result', result_row='single',
                 selector=dict(role='group', automation_id='records'))])

    def test_row_policy_does_not_instruct_an_unrequested_value_comparison(self):
        text = flow.run_instructions(self.recipe(), 'open-result')
        self.assertIn('값 비교를 추가하지 않는다', text)
        self.assertNotIn('이번 요청값을 먼저 읽어 비교', text)
        self.assertNotIn('값 비교 단계는', text)
        self.assertIn('앞선 조회는 반복하지 않는다', text)

    def test_value_comparison_guide_keeps_its_requested_check(self):
        recipe = self.recipe()
        recipe['steps'] = [dict(action='verify-value', result_column='Part', parameter='Part',
                               selector=dict(role='group', automation_id='records')),
                           dict(action='double-click-result')]
        text = flow.run_instructions(recipe, 'open-part')
        self.assertIn('값 비교 단계는 기록된 열의 현재 값과 이번 요청값을 비교한다', text)
        self.assertNotIn('현재 행 열기는', text)


if __name__ == '__main__':
    unittest.main()
