"""Exercise offline review, raw editor drafts and retry through real Tk callbacks."""
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from test_teach_capture import capture, combo


def tearDownModule():
    # Finalize the closed Tk interpreters on their owning thread before the
    # next test module starts a background window scan.
    gc.collect()


def row(key, text, *, field='Mode', **form):
    step = capture.capture_step(combo(field))
    step['id'] = key
    return {'step': step, 'form': {'text': text, 'alias': '', 'mode': None, 'argument': '', **form}}


def step_menu(get):
    button = get('step_tools')
    return button.nametowidget(button.cget('menu'))


def select_step(root, get, key):
    get('steps').selection_set(key)
    root.update()


def summary(get):
    button = get('preview')
    return button.getvar(button.cget('textvariable'))


def choose_mode(get, mode):
    combo = get('mode')
    combo.set(mode)
    combo.event_generate('<<ComboboxSelected>>')


class DraftSaveUITests(unittest.TestCase):
    def test_single_step_dialog_rejects_empty_multiple_or_partial_takes(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        self.addCleanup(root.destroy)
        step = capture.capture_step(combo())
        def apply():
            dialog = next(w for w in root.winfo_children() if w.winfo_class() == 'Toplevel')
            panel = dialog.winfo_children()[0]
            next(w for w in panel.winfo_children() if w.winfo_class() == 'TButton'
                 and w.cget('text') == '이 동작 적용').invoke()

        for steps, partial in (([], False), ([step, step], False), ([step], True), ([step], False)):
            with self.subTest(count=len(steps), partial=partial), patch.object(capture, 'CaptureSession') as factory:
                take = factory.return_value
                take.notes.partial = partial
                take.finish.return_value = {'steps': steps}
                root.after(30, apply)
                if len(steps) == 1 and not partial:
                    self.assertEqual(capture.capture_one_step(root, object(), 'Example'), step)
                else:
                    with self.assertRaises(ValueError):
                        capture.capture_one_step(root, object(), 'Example')
                take.start.assert_called_once_with()
                take.finish.assert_called_once_with(report_empty=False)

    def review(self, rows, interact, persist, draft, *, initial_step=None, locate_window=None):
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
        deadline = root.after(5000, timeout)
        try:
            with patch.object(tk, 'Tk', return_value=root), patch.object(capture, 'CaptureSession') as session:
                receipt = capture.capture(None, title='Example window', initial_draft=rows,
                                          save_recording=persist, save_draft=draft, initial_step=initial_step,
                                          locate_window=locate_window)
        finally:
            root.after_cancel(deadline)
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
            get('preview').invoke()
            self.assertEqual(get('mode').get(), '실행할 때 입력')
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
            step_menu(get).invoke('단계 별칭')
            self.assertEqual(get('alias').get(), 'Family')
            get('preview').invoke()
            get('argument').insert(0, 'FAMILY')
            get('저장').invoke()

        self.assertEqual(self.review(saved, fix_parameter, task, draft), {'status': 'saved'})
        self.assertEqual(task.call_args.args[0][0]['instruction']['parameter'], 'FAMILY')
        self.assertEqual(draft.call_count, 1)

    def test_value_summary_switches_modes_and_survives_save_and_reopen(self):
        import recorded_plan
        from review_drafts import ReviewStorage
        from test_visual_target import image, visual, VisualTargets

        marker = visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162))
        gesture = '결과 행을 더블클릭해 상세 창을 열어 줘'
        rows = [row('status', 'MES Setup을 N으로 설정', field='MES Setup'),
                row('family', '입력받은 변수를 Family 조건에 입력해줘', field='Family'),
                dict(step=dict(id='item', action='visual-click', label=gesture,
                               visual=dict(marker, gesture='double-click')),
                     form=dict(alias='', text=gesture, mode=None, argument=''))]

        def edit_values(root, get):
            with self.assertRaises(StopIteration):
                get('상세 설정')
            self.assertFalse(get('mode').master.winfo_manager())
            self.assertIn('고정값', summary(get))
            self.assertIn('N', summary(get))
            get('preview').invoke()
            self.assertTrue(get('mode').master.winfo_manager())
            self.assertEqual((get('mode').get(), get('argument').get()), ('고정값', 'N'))
            choose_mode(get, '실행할 때 입력')
            get('argument').delete(0, 'end')
            get('argument').insert(0, 'Status')

            select_step(root, get, 'family')
            self.assertIn('실행할 때 입력', summary(get))
            self.assertIn('Family', summary(get))
            get('preview').invoke()
            self.assertEqual((get('mode').get(), get('argument').get()), ('실행할 때 입력', 'Family'))
            choose_mode(get, '고정값')
            get('argument').delete(0, 'end')
            get('argument').insert(0, 'Hybrid')

            select_step(root, get, 'item')
            self.assertEqual(summary(get), '더블클릭')
            self.assertTrue(get('preview').instate(['disabled']))
            self.assertFalse(get('mode').master.winfo_manager())
            select_step(root, get, 'status')
            self.assertIn('Status', summary(get))
            step_menu(get).invoke('단계 별칭')
            get('alias').insert(0, '품질 상태')
            get('저장').invoke()

        def update_from_sentence(root, get):
            self.assertIn('실행할 때 입력', summary(get))
            self.assertIn('Status', summary(get))
            children = get('steps').get_children()
            select_step(root, get, children[2])
            self.assertEqual(summary(get), '더블클릭')
            self.assertTrue(get('preview').instate(['disabled']))
            select_step(root, get, children[1])
            self.assertIn('Hybrid', summary(get))
            get('preview').invoke()
            choose_mode(get, '문장 해석')
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, 'Family를 DRAM으로 선택')
            self.assertIn('DRAM', summary(get))
            get('저장').invoke()

        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            store = ReviewStorage('원본', 'Example.exe', 'Example window', directory)
            self.review(rows, edit_values, store.save_recording, store.save_draft)
            original = capture.learned_tasks.load_task(store.name, directory)
            steps = original['steps']
            self.assertEqual(steps[0]['alias'], '품질 상태')
            self.assertEqual(steps[0]['instruction']['mode'], 'required')
            self.assertEqual(steps[0]['instruction']['parameter'], 'Status')
            self.assertEqual(steps[1]['instruction']['mode'], 'fixed')
            self.assertEqual(steps[1]['instruction']['value'], 'Hybrid')
            recipe = recorded_plan.compile_plan(original)['envelope']['recipe']['steps']
            self.assertEqual(recipe[0]['parameter'], 'Status')
            self.assertNotIn('value', recipe[0])
            self.assertEqual(recipe[1]['value'], 'Hybrid')
            self.assertNotIn('parameter', recipe[1])
            self.assertEqual(recipe[2]['visual']['gesture'], 'double-click')

            revision = ReviewStorage('수정본', 'Example.exe', 'Example window', directory, source=original)
            self.review(revision.initial_rows(), update_from_sentence, revision.save_recording, revision.save_draft)
            revised = capture.learned_tasks.load_task(revision.name, directory)
            self.assertEqual(revised['steps'][1]['instruction'],
                             dict(text='Family를 DRAM으로 선택', mode='fixed', value='DRAM'))
            self.assertEqual(revised['steps'][0], steps[0])
            self.assertEqual(revised['steps'][2], steps[2])
            self.assertEqual(capture.learned_tasks.load_task(store.name, directory), original)

    def test_deleting_an_opener_keeps_child_and_a_repairable_window_link(self):
        rows = [row('opener', 'Mode를 N으로 선택'), row('child', 'Mode를 N으로 선택')]
        rows[1]['step']['_capture_window'] = {'after_id': 'opener', 'related': True,
                                              'title': 'Second window', 'exe': 'Second.exe'}
        draft = Mock(return_value={'status': 'draft_saved'})
        task = Mock(return_value={'status': 'saved'})

        def delete_opener(root, get):
            step_menu(get).invoke('선택 단계 삭제')
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
            step_menu(get).invoke('창을 연 단계 지정')
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

    def test_viewing_inferred_value_keeps_sentence_in_charge(self):
        rows = [row('mode', 'Mode를 N으로 선택')]
        task, draft = Mock(return_value={'status': 'saved'}), Mock()
        def edit(root, get):
            get('preview').invoke()
            self.assertEqual(get('argument').get(), 'N')
            get('preview').invoke()
            sentence = get('instruction')
            sentence.delete(0, 'end')
            sentence.insert(0, 'Mode를 P로 선택')
            self.assertIn('P', summary(get))
            get('저장').invoke()

        self.review(rows, edit, task, draft)
        self.assertEqual(task.call_args.args[0][0]['instruction'],
                         dict(mode='fixed', value='P', text='Mode를 P로 선택'))
        draft.assert_not_called()

    def test_inferred_manual_target_and_empty_fixed_text_accept_new_instructions(self):
        from test_textfield_capture import textfield
        rows = [dict(step=capture.manual('capture_target_unavailable', 'unknown'),
                     form=dict(alias='', mode=None, argument='', text='MES Setup을 N으로 설정')),
                dict(step=capture.capture_step(textfield()),
                     form=dict(alias='', mode='fixed', argument='', text='검색어 입력란을 비워줘'))]
        task, draft = Mock(return_value={'status': 'saved'}), Mock()

        def edit(root, get):
            self.assertIn('고정값 N', summary(get))
            get('preview').invoke()
            self.assertEqual((get('mode').get(), get('argument').get()), ('고정값', 'N'))
            get('argument').delete(0, 'end')
            get('argument').insert(0, 'P')
            select_step(root, get, 'search-field')
            self.assertIn('비우기', summary(get))
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, '검색어 입력란을 ABC로 설정해줘')
            self.assertIn('ABC', summary(get))
            get('저장').invoke()

        self.review(rows, edit, task, draft)
        found, cleared = map(capture.learned_tasks.recorded_step, task.call_args.args[0])
        self.assertEqual((found['match'], found['target'], found['instruction']['value']),
                         ('query', 'MES Setup', 'P'))
        self.assertEqual(cleared['instruction']['value'], 'ABC')
        draft.assert_not_called()

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

    def test_reorder_preserves_window_dependency_and_retakes_one_step_with_its_fields(self):
        rows = [row('first', 'Mode를 N으로 선택'), row('open', '입력받은 변수를 Mode 조건에 입력해줘',
                    alias='옵션', mode='required', argument='Mode'), row('child', 'Mode를 N으로 선택')]
        rows[2]['step']['_capture_window'] = dict(after_id='open', related=True, title='Other', exe='Other.exe')
        replacement = capture.capture_step(combo())
        replacement['target']['locator']['automation_id'] = 'correctedMode'
        task, draft, locate = Mock(return_value={'status': 'saved'}), Mock(), Mock(return_value=object())

        def edit(root, get):
            tree = get('steps')
            button = get('step_tools')
            menu = button.nametowidget(button.cget('menu'))
            tree.selection_set('open')
            root.update()
            menu.invoke('아래로 이동')
            self.assertEqual(tree.get_children(), ('first', 'open', 'child'))
            menu.invoke('위로 이동')
            self.assertEqual(tree.get_children(), ('open', 'first', 'child'))
            menu.invoke('이 단계 다시 녹화')  # an unrecognized take cannot displace the valid one
            self.assertEqual(get('steps').set('open', 'status'), '준비됨')
            menu.invoke('이 단계 다시 녹화')
            menu.invoke('단계 별칭')
            self.assertEqual(get('alias').get(), '옵션')
            get('preview').invoke()
            self.assertEqual(get('argument').get(), 'Mode')
            tree.selection_set('child')
            root.update()
            menu.invoke('이 단계 다시 녹화')  # cancellation in the other window
            get('저장').invoke()

        with patch.object(capture, 'capture_one_step', side_effect=[
                capture.manual('capture_target_unavailable'), replacement, None]) as take:
            self.review(rows, edit, task, draft, locate_window=locate)
        self.assertEqual(take.call_count, 3)
        self.assertEqual([c.args[0] for c in locate.call_args_list], [None, None, rows[2]['step']['_capture_window']])
        steps = task.call_args.args[0]
        self.assertEqual([s['id'] for s in steps], ['open', 'first', 'child'])
        self.assertEqual(steps[0]['target']['locator']['automation_id'], 'correctedMode')
        self.assertEqual((steps[0]['alias'], steps[0]['instruction']['parameter']), ('옵션', 'Mode'))
        self.assertEqual(steps[2]['window_context']['after_step'], 1)
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
            step_menu(get).invoke('창을 연 단계 지정')
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
            step_menu(get).invoke('선택 단계 삭제')
            root.update()
            get('저장').invoke()

        self.review(rows, remove, task, draft)
        self.assertEqual([s['id'] for s in task.call_args.args[0]], ['keep'])
        draft.assert_not_called()


if __name__ == '__main__':
    unittest.main()
