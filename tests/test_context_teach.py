"""The recorder review preserves query, anchor, menu and outcome as one action."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select   # checks and loads the pinned Windows bridge
import context_teach
import teach_capture


class MenuRecordingTests(unittest.TestCase):
    def setUp(self):
        self.button = {'role': 'button', 'automation_id': 'query'}
        self.step = {'id': 'reviewed', 'action': 'result-menu', 'label': '조건 조회 후 내보내기',
                     'context': {'trigger': self.button, 'ready': {'kind': 'text',
                                  'target': {'role': 'statictext', 'automation_id': 'generation'},
                                  'expected': '완료'},
                                  'menu': {'target': {'role': 'group', 'automation_id': 'panel'},
                                           'point': [.4, .4], 'path': ['내보내기', '파일']},
                                  'output': None}}

    def test_alias_does_not_transfer_when_menu_was_retaught(self):
        edits = teach_capture.StepEdits()
        edits.store(self.step, '내보내기', '')
        self.assertEqual(edits.apply([self.step])[0]['alias'], '내보내기')
        changed = deepcopy(self.step)
        changed['context']['menu']['path'] = ['다른 메뉴', '파일']
        self.assertNotIn('alias', edits.apply([changed])[0])
        view = teach_capture.instruction_view(self.step, None)
        self.assertIn('조회 완료 확인 → 표 우클릭 → 내보내기 → 파일', view)
        self.assertIn('전달까지만 확인', view)
        self.assertEqual(teach_capture.default_text(self.step), self.step['label'])

    def test_merge_proposal_is_for_an_identical_host_button_only(self):
        old = {'action': 'click', 'target': {'kind': 'element', 'locator': dict(self.button, name='조회')}}
        self.assertTrue(context_teach.previous_trigger(old, self.step['context']))
        copy = deepcopy(old)
        copy['target']['locator']['automation_id'] = 'other'
        self.assertFalse(context_teach.previous_trigger(copy, self.step['context']))
        old['target']['window'] = {'kind': 'popup'}
        self.assertFalse(context_teach.previous_trigger(old, self.step['context']))

    def test_menu_path_preserves_literal_order_and_internal_spaces(self):
        self.assertEqual(context_teach.menu_path('  EXPORT\n\n EXPORT TO EXCEL  '),
                         ['EXPORT', 'EXPORT TO EXCEL'])
        with self.assertRaises(ValueError):
            context_teach.menu_path(' \n')

    def test_an_ordinary_right_click_is_identifiable_in_review_without_losing_the_stop(self):
        step = teach_capture.capture_step({'id': 'seen', 'action': 'right-click',
                                          'target': {'kind': 'region', 'locator': {'role': 'group'}}})
        self.assertEqual('manual', step['action'])
        self.assertIn('우클릭', step['label'])
        self.assertIn('조회 후 우클릭 메뉴 가르치기', teach_capture.instruction_view(step, None))
