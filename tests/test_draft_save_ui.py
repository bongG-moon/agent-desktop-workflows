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
    def test_review_focus_and_explicit_next_keep_enter_available_for_composition(self):
        rows = [row('first', 'Mode를 N으로 설정해줘'), row('second', 'Mode를 N으로 설정해줘'),
                row('third', 'Mode를 N으로 설정해줘')]
        task, draft = Mock(return_value=dict(status='saved')), Mock()

        def edit(root, get):
            entry, tree = get('instruction'), get('steps')
            self.assertEqual(root.focus_lastfor(), entry)
            self.assertEqual(entry.bind('<Return>'), '')
            self.assertTrue(entry.bind('<Control-Return>'))
            hint = get('instruction_hint')
            self.assertIn('Ctrl+Enter', hint.getvar(hint.cget('textvariable')))
            self.assertEqual(root.focus_get(), entry)
            entry.delete(0, 'end')
            entry.insert(0, 'Mode를 W로 설정해줘')
            entry.event_generate('<Return>')
            root.update()
            self.assertEqual(tree.selection(), ('first',))
            self.assertEqual(entry.get(), 'Mode를 W로 설정해줘')
            entry.event_generate('<Control-Return>')
            root.update()
            self.assertEqual(tree.selection(), ('second',))
            self.assertEqual(root.focus_get(), entry)

            tree.focus_set()
            select_step(root, get, 'first')
            self.assertEqual(root.focus_get(), tree)
            next_button = get('적용하고 다음 단계')
            next_button.focus_set()
            next_button.invoke()
            root.update()
            self.assertEqual(tree.selection(), ('second',))
            self.assertEqual(root.focus_get(), entry)
            tree.focus_set()
            select_step(root, get, 'third')
            self.assertEqual(root.focus_get(), tree)
            get('저장').invoke()

        self.review(rows, edit, task, draft, visible=True)
        self.assertEqual(task.call_args.args[0][0]['instruction']['value'], 'W')
        draft.assert_not_called()

    def test_name_correction_is_shown_only_for_a_mismatched_recorded_target(self):
        rows = [row('field', 'Mode를 N으로 설정해줘')]
        task, draft = Mock(return_value=dict(status='saved')), Mock()

        def correct_name(root, get):
            menu = step_menu(get)
            def labels():
                return [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1)
                        if menu.type(i) != 'separator']
            self.assertNotIn('대상 이름 수정', labels())
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, 'MES Setup을 N으로 설정해줘')
            self.assertIn('대상 이름 수정', labels())
            menu.invoke('대상 이름 수정')
            self.assertTrue(get('alias').master.winfo_manager())
            get('use_sentence_name').invoke()
            self.assertEqual(get('alias').get(), 'MES Setup')
            self.assertNotIn('대상 이름 수정', labels())
            self.assertIn('MES Setup', get('target_summary').getvar(get('target_summary').cget('textvariable')))
            get('저장').invoke()

        self.assertEqual(self.review(rows, correct_name, task, draft), dict(status='saved'))
        saved, = task.call_args.args[0]
        self.assertEqual(saved['alias'], 'MES Setup')
        self.assertEqual(saved['instruction']['value'], 'N')
        self.assertEqual(saved['target']['locator']['automation_id'], 'comboMode')
        draft.assert_not_called()

    def test_location_repair_is_optional_and_replaces_only_the_selected_unresolved_step(self):
        import context_teach
        import row_teach
        sentence = '결과 표에서 우클릭해서 내보내 줘'
        missing = capture.manual('capture_target_unavailable', 'missing')
        rows = [row('ready', 'Mode를 N으로 설정해줘'),
                dict(step=missing, form=dict(text=sentence, alias='결과 내보내기', mode=None, argument=''))]
        repaired = dict(id='taught', action='result-menu', label='메뉴 내보내기',
                        context=dict(trigger=None, ready=None, output=None,
                                     menu=dict(target=dict(role='group', automation_id='results'),
                                               point=[.4, .4], path=['Export', 'Excel'])))
        task, draft, locate = Mock(return_value=dict(status='saved')), Mock(), Mock()

        def labels(menu):
            end = menu.index('end')
            return [] if end is None else [menu.entrycget(i, 'label') for i in range(end + 1)
                                          if menu.type(i) != 'separator']

        def repair(root, get):
            button = get('add_step')
            self.assertEqual(labels(button.nametowidget(button.cget('menu'))),
                             ['선택한 단계 아래에 추가', '맨 아래에 추가'])
            self.assertNotIn('위치 직접 보완', labels(step_menu(get)))
            select_step(root, get, 'missing')
            self.assertIn('위치 직접 보완', labels(step_menu(get)))
            submenu = step_menu(get).nametowidget(step_menu(get).entrycget('위치 직접 보완', 'menu'))
            submenu.invoke('행 버튼 지정')  # Cancelling retains the original text and step.
            root.update()
            self.assertEqual(get('steps').get_children(), ('ready', 'missing'))
            self.assertEqual(get('instruction').get(), sentence)
            submenu.invoke('우클릭 메뉴 지정')
            root.update()
            self.assertEqual(get('steps').get_children(), ('ready', 'missing'))
            self.assertEqual(get('instruction').get(), sentence)
            self.assertNotIn('위치 직접 보완', labels(step_menu(get)))
            self.assertEqual(str(get('instruction').cget('state')), 'readonly')
            hint = get('instruction_hint')
            self.assertIn('시연한 메뉴 순서', hint.getvar(hint.cget('textvariable')))
            get('저장').invoke()

        with (patch.object(context_teach, 'teach_context_dialog', return_value=repaired),
              patch.object(row_teach, 'teach_row_dialog', return_value=None)):
            self.review(rows, repair, task, draft, locate_window=locate)
        saved = task.call_args.args[0]
        self.assertEqual([s['id'] for s in saved], ['ready', 'missing'])
        self.assertEqual(saved[1]['action'], 'result-menu')
        self.assertEqual(saved[1]['description'], sentence)
        self.assertEqual(saved[1]['alias'], '결과 내보내기')
        draft.assert_not_called()

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

    def review(self, rows, interact, persist, draft, *, initial_step=None, locate_window=None, visible=False):
        try:
            import tkinter as tk
            from tkinter import ttk
        except ImportError as exc:
            self.skipTest(str(exc))
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        if visible:
            root.update()
            root.focus_force()
        else:
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

        root.after(60, lambda: root.after_idle(exercise))
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

    def test_insert_below_preserves_pending_text_and_popup_opener_when_numbers_change(self):
        rows = [row('first', 'Mode를 N으로 선택'), row('opener', 'Mode를 N으로 선택'),
                row('child', 'Mode를 N으로 선택'), row('tail', 'Mode를 N으로 선택')]
        rows[2]['step']['_capture_window'] = dict(after_id='opener', related=True,
                                                   title='Other', exe='Other.exe')
        task, draft = Mock(return_value={'status': 'saved'}), Mock()
        added = []

        def insert(root, get):
            tree = get('steps')
            button = get('add_step')
            menu = button.nametowidget(button.cget('menu'))
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, 'Mode를 P로 선택')
            menu.invoke('선택한 단계 아래에 추가')
            added.append(tree.selection()[0])
            self.assertEqual(tree.get_children(), ('first', added[0], 'opener', 'child', 'tail'))
            self.assertEqual(get('instruction').get(), '')
            get('instruction').insert(0, 'Mode를 N으로 선택')

            select_step(root, get, 'child')
            get('instruction').delete(0, 'end')
            get('instruction').insert(0, 'Mode를 P로 선택')
            menu.invoke('선택한 단계 아래에 추가')
            added.append(tree.selection()[0])
            self.assertEqual(tree.get_children(),
                             ('first', added[0], 'opener', 'child', added[1], 'tail'))
            self.assertEqual(tree.set(added[1], 'window'), 'Other')
            self.assertEqual(get('instruction').get(), '')
            get('instruction').insert(0, 'Mode를 N으로 선택')
            select_step(root, get, 'tail')
            self.assertEqual([tree.set(key, 'number') for key in tree.get_children()],
                             ['1', '2', '3', '4', '5', '6'])
            get('저장').invoke()

        self.review(rows, insert, task, draft)
        saved = task.call_args.args[0]
        self.assertEqual([step['id'] for step in saved],
                         ['first', added[0], 'opener', 'child', added[1], 'tail'])
        self.assertEqual(saved[0]['instruction']['value'], 'P')
        self.assertEqual(saved[3]['instruction']['value'], 'P')
        self.assertEqual(saved[3]['window_context']['after_step'], 3)
        self.assertEqual(saved[4]['window_context'], saved[3]['window_context'])
        self.assertNotIn('window_context', saved[1])
        draft.assert_not_called()

    def test_append_uses_last_window_without_selection_and_works_in_an_empty_review(self):
        populated = [row('first', 'Mode를 N으로 선택'), row('opener', 'Mode를 N으로 선택'),
                     row('child', 'Mode를 N으로 선택')]
        populated[2]['step']['_capture_window'] = dict(after_id='opener', related=True,
                                                        title='Other', exe='Other.exe')

        for rows in (populated, []):
            with self.subTest(empty=not rows):
                task, draft = Mock(return_value={'status': 'saved'}), Mock()
                added = []

                def append(root, get):
                    tree = get('steps')
                    previous = tree.get_children()
                    tree.selection_remove(tree.selection())
                    root.update()
                    self.assertTrue(get('선택 단계 삭제').instate(['disabled']))
                    button = get('add_step')
                    menu = button.nametowidget(button.cget('menu'))
                    menu.tk.call(menu.cget('postcommand'))
                    self.assertEqual(menu.entrycget('선택한 단계 아래에 추가', 'state'), 'disabled')
                    self.assertEqual(menu.entrycget('맨 아래에 추가', 'state'), 'normal')
                    menu.invoke('선택한 단계 아래에 추가')
                    self.assertEqual(tree.get_children(), previous)
                    menu.invoke('맨 아래에 추가')
                    added.append(tree.selection()[0])
                    self.assertEqual(tree.get_children(), previous + (added[0],))
                    self.assertEqual(get('instruction').get(), '')
                    get('instruction').insert(0, 'Mode를 N으로 선택')
                    get('저장').invoke()

                self.review(rows, append, task, draft)
                saved = task.call_args.args[0]
                self.assertEqual([step['id'] for step in saved],
                                 [entry['step']['id'] for entry in rows] + added)
                if rows:
                    self.assertNotIn('window_context', saved[0])
                    self.assertEqual(saved[-1]['window_context'], saved[-2]['window_context'])
                    self.assertEqual(saved[-1]['window_context']['after_step'], 2)
                else:
                    self.assertNotIn('window_context', saved[-1])
                draft.assert_not_called()

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
            self.assertEqual(get('선택 단계 삭제').winfo_manager(), 'pack')
            menu = step_menu(get)
            self.assertNotIn('선택 단계 삭제', [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1)
                                               if menu.type(i) != 'separator'])
            get('선택 단계 삭제').invoke()
            root.update()
            get('저장').invoke()

        self.review(rows, remove, task, draft)
        self.assertEqual([s['id'] for s in task.call_args.args[0]], ['keep'])
        draft.assert_not_called()


if __name__ == '__main__':
    unittest.main()
