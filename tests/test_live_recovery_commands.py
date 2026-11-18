from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import grounding
import live_recovery
from test_recorded_plan import recording


class RecoveryCommandsTests(unittest.TestCase):
    def test_continue_saved_supplement_preserves_prefix_and_moves_window_source(self):
        base = recording()
        tail = dict(base, name='supplement', steps=[deepcopy(base['steps'][-1]), dict(
            action='open', target='ok', role='button', match='automation_id', expand=False,
            window_context=dict(after_step=1, exe='Form.exe', title='Details'))])
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, 'task_directory', return_value=Path(tmp)), \
                patch.object(app, 'windows') as windows:
            app.tasks.save_task(base, tmp)
            app.tasks.save_task(tail, tmp)
            args = app.parser().parse_args(['continue-recording','--task',base['name'],'--supplement','supplement',
                                            '--from-step','3'])
            result = app.run(args)
            saved = app.tasks.load_task(result['task']['name'], tmp)
            self.assertEqual(saved['steps'][:2], base['steps'][:2])
            self.assertEqual(saved['steps'][3]['window_context']['after_step'], 3)
            self.assertEqual(app.tasks.load_task('supplement', tmp), tail)
            self.assertEqual(app.tasks.load_task(base['name'], tmp), base)
            windows.assert_not_called()

    def test_current_image_must_agree_around_pointer_in_same_sized_window(self):
        def png(changed, width=64):
            data = bytearray(width * 64 * 4)
            for x, y in changed:
                data[4*(y*width+x)] = 255
            return grounding._png(width,64,data)
        original = png([])
        self.assertTrue(live_recovery._same_patch(original, png([(1,1)]), [35,35]))
        self.assertFalse(live_recovery._same_patch(original, png([(34,35)]), [35,35]))
        self.assertFalse(live_recovery._same_patch(original, png([],65), [35,35]))
        self.assertFalse(live_recovery._same_patch(original, original, [-1,0]))

    def test_target_first_value_and_variable_preserve_literal_and_kind(self):
        row = recording()['steps'][0]
        readings = [("MES Setup에 'N'을 선택해줘", 'fixed', 'N'),
                    ('MES Setup 조건에 “High Speed”를 설정해줘', 'fixed', 'High Speed'),
                    ('MES Setup에 입력받은 변수를 넣어줘','required','옵션'),
                    ('MES Setup에 {State}를 넣어줘','required','State')]
        for text, mode, argument in readings:
            with self.subTest(text=text):
                spec = app.tasks.compile_instruction(row,text,mode=mode,argument=argument)
                self.assertEqual(spec['text'], text)
                self.assertEqual(spec.get('value', spec.get('parameter')), argument)
        with self.assertRaisesRegex(ValueError, 'instruction_binding_conflict'):
            app.tasks.compile_instruction(row, readings[-1][0], mode='required', argument='Wrong')

    def test_runtime_question_belongs_to_one_assignment_and_preserves_named_variables(self):
        row = recording()['steps'][0]
        prompt = ' (실행할 때 값을 물어봐줘)'
        sample = "MES Setup 조건에 'W'를 입력해줘" + prompt
        spec = app.tasks.compile_instruction(row, sample, mode='required', argument='Status')
        self.assertEqual(spec, dict(mode='required', parameter='Status', text=sample))
        with self.assertRaisesRegex(ValueError, 'instruction_binding_conflict'):
            app.tasks.compile_instruction(row, sample, mode='fixed', argument='W')
        explicit = 'MES Setup에 {State}를 넣어줘' + prompt
        with self.assertRaisesRegex(ValueError, 'instruction_binding_conflict'):
            app.tasks.compile_instruction(row, explicit, mode='required', argument='Other')
        with self.assertRaisesRegex(ValueError, 'instruction_target_mismatch'):
            app.tasks.compile_instruction(row, 'Family에 X 입력' + prompt)
        for note in ('실행 전에 값이 없으면 물어봐줘', '실행 전에 묻지 말고 N을 써'):
            text = "MES Setup에 'N'을 선택해줘 (" + note + ')'
            self.assertEqual(app.tasks.compile_instruction(row, text)['mode'], 'draft')
        # The old editor accepted an explicit fixed binding even with this
        # former free-form note. A saved choice must keep its original meaning.
        saved = dict(row, instruction=dict(mode='fixed', text=sample, value='W'))
        self.assertEqual(app.tasks._check_instruction(saved, saved['instruction']), saved['instruction'])

    def test_runtime_question_can_follow_a_period_or_comma(self):
        row = recording()['steps'][0]
        for suffix in ('. 실행할 때마다 물어봐줘', ', 실행 전에 어떤 값을 넣을지 물어봐줘',
                       '（실행할 때 물어봐줘）'):
            text = 'MES Setup 조건에 N을 입력해줘' + suffix
            self.assertEqual(app.tasks.compile_instruction(row, text), dict(mode='required', parameter='MES Setup', text=text))
        for text in ('MES Setup 조건에 N을 입력해줘 (실행 전에 물어봐줘）',
                     'MES Setup 조건에 N을 입력해줘, 실행 전에 물어봐줘)',
                     'MES Setup 조건에 N을 입력해줘. 실행 전에 Family 값을 물어봐줘',
                     'MES Setup 조건에 N을 입력해줘. 실행 전에 물어보지 말아줘',
                     'MES Setup 조건에 N을 입력해줘. 실행할 때 값이 없으면 물어봐줘',
                     'MES Setup을 N으로 설정하고 Family를 Hybrid로 설정해줘. 실행 전에 물어봐줘'):
            with self.subTest(text=text):
                self.assertEqual(app.tasks.compile_instruction(row, text)['mode'], 'draft')
