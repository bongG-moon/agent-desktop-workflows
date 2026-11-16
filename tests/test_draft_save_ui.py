"""Exercise offline review, raw editor drafts and retry through real Tk callbacks."""
import unittest
from unittest.mock import Mock, patch

from test_teach_capture import capture, combo


def row(key, text, **form):
    step = capture.capture_step(combo())
    step['id'] = key
    return {'step': step, 'form': {'text': text, 'alias': '', 'mode': None, 'argument': '', **form}}


class DraftSaveUITests(unittest.TestCase):
    def review(self, rows, interact, persist, draft, *, initial_step=None):
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as exc:
            self.skipTest(str(exc))
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        errors = []

        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)

        def get(name):
            return next(w for w in widgets(root) if w.winfo_name() == name
                        or (isinstance(w, ttk.Button) and w.cget('text') == name))

        def exercise():
            try:
                interact(root, get)
            except Exception as exc:
                errors.append(exc)
                root.destroy()

        def timeout():
            errors.append(AssertionError('review did not finish'))
            root.destroy()

        root.after(60, exercise)
        root.after(5000, timeout)
        with patch.object(tk, 'Tk', return_value=root), patch.object(capture, 'CaptureSession') as session:
            receipt = capture.capture(None, title='Example window', initial_draft=rows,
                                      save_recording=persist, save_draft=draft, initial_step=initial_step)
        session.assert_not_called()
        if errors:
            raise errors[0]
        return receipt

    def test_invalid_parameter_and_active_text_survive_save_and_offline_reopen(self):
        rows = [row('a', '멈추면 나중에 계속해', alias='Family', mode='required')]
        text = '  실행할 때 입력받은 값으로 바꿔 줘  '
        receipt = {'status': 'draft_saved'}
        draft = Mock(return_value=receipt)
        task = Mock(return_value={'status': 'saved'})

        def save_incomplete(root, get):
            self.assertEqual(get('mode').get(), '필수 변수')
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, text)
            self.assertFalse(get('초안 저장').instate(['disabled']))
            get('초안 저장').invoke()

        self.assertEqual(self.review(rows, save_incomplete, task, draft), receipt)
        task.assert_not_called()
        saved, code, number = draft.call_args.args
        self.assertEqual((number, saved[0]['form']), (1, dict(alias='Family', mode='required', argument='', text=text)))
        self.assertTrue(code)

        def fix_parameter(root, get):
            self.assertEqual(get('instruction').get(), text)
            self.assertEqual(get('alias').get(), 'Family')
            get('argument').insert(0, 'FAMILY')
            get('저장').invoke()

        self.assertEqual(self.review(saved, fix_parameter, task, draft), {'status': 'saved'})
        self.assertEqual(task.call_args.args[0][0]['instruction']['parameter'], 'FAMILY')
        self.assertEqual(draft.call_count, 1)

    def test_deleting_an_opener_keeps_child_and_a_repairable_window_link(self):
        rows = [row('opener', 'Mode를 N으로 선택'), row('child', 'Mode를 N으로 선택')]
        rows[1]['step']['_capture_window'] = {'after_id': 'opener', 'related': True,
                                              'title': 'Second window', 'exe': 'Second.exe'}
        draft = Mock(return_value={'status': 'draft_saved'})
        task = Mock(return_value={'status': 'saved'})

        def delete_opener(root, get):
            get('선택 단계 삭제').invoke()
            root.update()
            self.assertEqual(get('steps').get_children(), ('child',))
            get('초안 저장').invoke()

        self.review(rows, delete_opener, task, draft)
        raw, reason, number = draft.call_args.args
        self.assertEqual((reason, number), ('recorded_window_opener_required', 1))
        self.assertEqual(raw[0]['form'], rows[1]['form'])
        self.assertEqual(raw[0]['step']['_capture_window']['_openers'], ())
        task.assert_not_called()

        def repair(root, get):
            get('창을 연 단계 지정').invoke()
            root.update()
            dialog = next(w for w in root.winfo_children() if w.winfo_class() == 'Toplevel')
            panel = dialog.winfo_children()[0]
            picker = next(w for w in panel.winfo_children() if w.winfo_class() == 'TCombobox')
            picker.current(0)
            get('연결').invoke()
            root.update()
            get('저장').invoke()

        self.review(raw, repair, task, draft)
        self.assertEqual(task.call_args.args[0][0]['window_context'],
                         {'title': 'Second window', 'exe': 'Second.exe', 'mode': 'existing'})

    def test_error_after_conversion_keeps_editor_active_for_retry(self):
        rows = [row('a', 'Mode를 N으로 선택')]
        task = Mock(side_effect=[OSError('disk busy'), {'status': 'saved'}])
        draft = Mock()

        def retry(root, get):
            get('저장').invoke()
            self.assertTrue(root.winfo_exists())
            self.assertEqual(get('instruction').get(), rows[0]['form']['text'])
            self.assertFalse(get('저장').instate(['disabled']))
            get('저장').invoke()

        self.assertEqual(self.review(rows, retry, task, draft), {'status': 'saved'})
        self.assertEqual(task.call_count, 2)
        self.assertEqual(task.call_args_list[0], task.call_args_list[1])
        draft.assert_not_called()

    def test_resume_focuses_failed_child_and_can_replace_existing_manual_opener(self):
        rows = [row('launch', 'Mode를 N으로 선택'), row('manual', ''), row('child', 'Mode를 N으로 선택')]
        rows[1]['step'] = capture.manual('right_click_requires_menu_teaching', 'manual')
        rows[2]['step']['_capture_window'] = dict(exe='Other.exe', title='Other', after_id='manual', related=True)
        task, draft = Mock(return_value={'status': 'saved'}), Mock()

        def fix(root, get):
            self.assertEqual(get('steps').selection(), ('child',))
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, 'Mode를 P로 선택')
            get('창을 연 단계 지정').invoke()
            dialog = next(w for w in root.winfo_children() if w.winfo_class() == 'Toplevel')
            panel = dialog.winfo_children()[0]
            picker = next(w for w in panel.winfo_children() if w.winfo_class() == 'TCombobox')
            values = picker.cget('values')
            self.assertEqual(len(values), 2)
            self.assertTrue(values[0].startswith('1단계'))
            picker.current(0)
            get('연결').invoke()
            self.assertEqual(get('instruction').get(), 'Mode를 P로 선택')
            get('초안 저장').invoke()

        self.review(rows, fix, task, draft, initial_step=3)
        saved = task.call_args.args[0][-1]
        self.assertEqual(saved['window_context']['after_step'], 1)
        self.assertEqual(saved['instruction']['value'], 'P')
        draft.assert_not_called()

    def test_conversion_error_keeps_original_empty_field_and_step_index_in_draft(self):
        rows = [row('a', 'Mode를 N으로 선택'), row('b', '')]
        error = capture.learned_tasks.TaskError('invalid_step_instruction')
        error.step_number = 2
        task = Mock(side_effect=error)
        draft = Mock(return_value={'status': 'draft_saved'})
        self.review(rows, lambda root, get: get('저장').invoke(), task, draft)
        self.assertEqual(draft.call_args.args, (rows, 'invalid_step_instruction', 2))

    def test_bad_visual_hint_can_be_deleted_without_losing_other_description(self):
        rows = [row('bad', '화면 버튼'), row('keep', 'Mode를 N으로 선택')]
        rows[0]['step'] = dict(id='bad', label='화면 버튼', action='visual-click', visual={})
        task, draft = Mock(return_value={'status': 'saved'}), Mock()

        def remove(root, get):
            self.assertEqual(get('steps').set('bad', 'status'), '확인 필요')
            self.assertFalse(get('초안 저장').instate(['disabled']))
            get('선택 단계 삭제').invoke()
            root.update()
            get('저장').invoke()

        self.review(rows, remove, task, draft)
        self.assertEqual([s['id'] for s in task.call_args.args[0]], ['keep'])
        draft.assert_not_called()


if __name__ == '__main__':
    unittest.main()
