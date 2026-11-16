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
