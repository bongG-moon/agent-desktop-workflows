"""Black-box recovery of a saved form using a fresh, input-free window snapshot."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import learned_tasks as tasks
import recovery
import review_drafts
import teach_capture
from test_recorded_plan import button, combo


class LiveRecordingRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.inputs = self.directory / 'binding.json'
        self.owner = SimpleNamespace(hwnd=1, pid=2, created=3, exe='Demo.exe')
        self.observable = Mock()
        self.observe = Mock(return_value=dict(targets=[
            dict(role='combobox', automation_id='c_status', semantic_label='MES Setup', enabled=True, offscreen=False),
            dict(role='combobox', automation_id='c_family', semantic_label='Family', enabled=True, offscreen=False),
            dict(role='button', automation_id='query', name='조회', enabled=True, offscreen=False)]))
        self.menu = Mock(return_value=dict(trigger=None, ready=None, output=None, menu=dict(
            target=dict(role='group', automation_id='results'), point=[.3, .5], path=['Export to Excel', 'Active sheet'])))
        for p in (patch.dict(sys.modules, live_recovery=SimpleNamespace(observe_scope=self.observe,
                                                                         menu_from_frame=self.menu)),
                  patch.object(app, 'task_directory', return_value=self.directory),
                  patch.object(app, 'windows', return_value=[dict(hwnd=1, title='업무')]),
                  patch.object(app, 'pick_window', return_value={}), patch.object(app.bridge, 'Binding', return_value=self.owner),
                  patch.object(app.visibility, 'require_observable', self.observable),
                  patch.object(app, 'guard')):
            p.start()
            self.addCleanup(p.stop)
        self.rows = []
        for index, (raw, text) in enumerate([
            (combo(label='임시 라벨', identifier='c_status', sentence='이 항목을 N으로 설정해줘'), "MES Setup에 'N'을 선택해줘"),
            (combo(label='Family', identifier='c_family', sentence='입력받은 변수를 Family에 입력해줘'), 'Family에 입력받은 변수를 넣어줘'),
            (button(), '조회 버튼을 누르고 5초 기다려줘'),
            (teach_capture.manual('right_click_requires_menu_teaching'), '결과 표를 우클릭해줘'),
            (teach_capture.manual('popup_needs_live_binding'), 'Export to Excel을 눌러줘'),
            (teach_capture.manual('unsupported_recorded_action'), 'Active sheet 선택'),
        ], 1):
            raw.pop('instruction', None)
            raw.update(id=str(index), label=raw.get('field_label') or raw['label'])
            mode, arg = ('required', '제품군') if index == 2 else (None, '')
            self.rows.append(dict(step=raw, form=dict(alias='', text=text, mode=mode, argument=arg)))
        self.store = review_drafts.ReviewStorage('내 녹화', 'Demo.exe', '업무', self.directory)

    def args(self, **args):
        return argparse.Namespace(**dict(dict(task='내 녹화', only=None, image=False, bindings=None, save_as=None), **args))

    def probe(self):
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        return recovery.run(app, self.args())

    def test_prepare_and_instruct_on_original_draft_point_straight_to_double_click_grounding(self):
        from recording_followup import followup
        text = '설정 항목을 더블 클릭해서 팝업 창을 띄워줘'
        self.rows = [dict(step=teach_capture.manual('double_click_needs_review'),
                          form=dict(alias='',text=text,mode=None,argument=''))]
        self.store.save_draft(self.rows, 'invalid_step_instruction', 1)
        report = followup(recovery.run(app,self.args(prepare=True)))
        self.assertEqual(report['next_action']['args'], ['ground','--task','내 녹화','--only','1'])
        self.assertFalse(report['input_dispatched'])
        self.assertEqual(report['host_action'],'ground_recorded_double_click')
        self.observe.assert_called_once()
        with patch.object(app,'attach_recovery') as rescan:
            response = app.save_instruction(self.args(only='1',text=text,save_as='실행용'))
            rescan.assert_not_called()
            self.assertEqual(response['ground_args'], report['next_action']['args'])

    def bind(self, fingerprint, patches, **args):
        self.inputs.write_text(json.dumps(dict(source_fingerprint=fingerprint, steps=patches)), encoding='utf8')
        return recovery.run(app, self.args(bindings=self.inputs, save_as='실행용', **args))

    def patches(self):
        return [dict(number=1, target='c_status', role='combobox', mode='fixed', argument='N'),
                dict(number=2, target='Family', role='combobox', mode='required', argument='제품군'),
                dict(number=3, target='조회', role='button', mode='recorded', wait_after=5),
                dict(number=4, through=6, target='results', role='group', frame='001', point=[10, 30],
                     mode='context-menu', path=['Export to Excel', 'Active sheet'])]

    def test_read_observe_bind_and_fold_preserves_source_and_role(self):
        info = self.probe()
        original = (self.directory/'내 녹화.json').read_bytes()
        self.assertEqual(len(info['live_scopes']), 1)
        self.assertEqual(info['steps'][0]['text'], "MES Setup에 'N'을 선택해줘")
        suggested = info['bindings_template']['steps']
        self.assertEqual([(s['number'], s['target'], s.get('argument')) for s in suggested],
                         [(1,'c_status','N'), (2,'c_family','제품군'), (3,'query',None)])
        self.assertEqual(suggested[2]['wait_after'], 5)
        self.assertEqual(info['steps'][3]['image_args'][-3:], ['--only','4','--image'])
        self.observe.assert_called_once()
        result = self.bind(info['source_fingerprint'], self.patches())
        self.assertEqual(result['status'], 'recorded', result)
        fixed = tasks.load_task('실행용', self.directory)
        self.assertEqual(len(fixed['steps']), 4)
        self.assertEqual(fixed['steps'][0]['instruction'], dict(mode='fixed', value='N', text=self.rows[0]['form']['text']))
        self.assertEqual(fixed['steps'][1]['instruction']['parameter'], '제품군')
        self.assertEqual(fixed['steps'][2]['wait_after'], 5)
        self.assertEqual(fixed['steps'][3]['context'], self.menu.return_value)
        self.assertEqual(result['source_steps'], {1:1, 2:2, 3:3, 4:4, 5:4, 6:4})
        self.assertEqual(self.menu.call_args.args[-2], [info['source_fingerprint'], 4])
        self.assertEqual(self.observe.call_count, 2)
        self.assertEqual((self.directory/'내 녹화.json').read_bytes(), original)
        self.assertFalse(result['input_dispatched'])

    def test_unseen_and_ambiguous_targets_are_not_bound(self):
        info = self.probe()
        windows = self.observe.return_value['targets']
        windows[0]['enabled'] = False
        self.assertEqual(self.bind(info['source_fingerprint'], self.patches())['diagnostic'], 'target_disabled')
        windows.append(deepcopy(windows[0]))
        self.assertEqual(self.bind(info['source_fingerprint'], self.patches())['diagnostic'], 'target_ambiguous')
        windows[:] = windows[1:3]
        self.assertEqual(self.bind(info['source_fingerprint'], self.patches())['diagnostic'], 'target_missing')
        self.assertFalse((self.directory/'실행용.json').exists())

    def test_generated_fields_and_named_frame_region_use_one_targeted_scope(self):
        info = self.probe()
        fields = info['bindings_template']['steps']
        self.assertEqual([item['match'] for item in fields], ['automation_id'] * 3)
        self.observe.reset_mock()
        result = self.bind(info['source_fingerprint'], fields + [self.patches()[-1]])
        self.assertEqual(result['status'], 'recorded', result)
        self.observe.assert_called_once()
        kwargs = self.observe.call_args.kwargs
        self.assertTrue(kwargs['bindings_only'])
        self.assertEqual(kwargs['lookups']['selectors'], [
            dict(automation_id='c_status', role='combobox'), dict(automation_id='c_family', role='combobox'),
            dict(automation_id='query', role='button')])
        self.assertEqual(kwargs['lookups']['regions'], [('results', 'group')])

    def test_explicit_identifier_cannot_bind_another_controls_label(self):
        inventory = dict(targets=[dict(role='combobox', automation_id='other',
                                       semantic_label='missing', enabled=True, offscreen=False)])
        with self.assertRaisesRegex(tasks.TaskError, 'target_missing'):
            recovery._observed_field(dict(role='combobox', match='automation_id', target='missing'), inventory)

    def test_prepare_uses_recorded_selectors_for_open_child_and_shares_them_with_enter(self):
        child = SimpleNamespace(hwnd=8, pid=2, created=3, exe='Demo.exe')
        scope = dict(exe=child.exe, title='Detail', mode='existing')
        main_field = dict(role='textfield', automation_id='equipment')
        detail_field = dict(role='textfield', automation_id='notes')
        steps = [
            dict(action='field-input', label='Equipment', selector=main_field, value='XA'),
            dict(action='field-input', label='Notes', selector=detail_field, value='AB', window_context=scope),
            dict(action='submit', label='Enter', selector=detail_field, window_context=scope)]
        tasks.save_task(dict(name='내 녹화', schema=1, exe=self.owner.exe, window='업무', steps=steps), self.directory)

        def observe(_app, _main, surface, _title, **options):
            selectors = options['lookups']['selectors']
            self.assertEqual(selectors, [main_field if surface == self.owner else detail_field])
            return dict(targets=[dict(selector, recorded_selector=selector, enabled=True, offscreen=False)
                                 for selector in selectors])

        self.observe.side_effect = observe
        with patch.object(recovery, '_bind_surface', side_effect=[self.owner, child, child]):
            report = recovery.run(app, self.args(prepare=True))
        self.assertEqual(report['status'], 'recorded', report)
        self.assertEqual(self.observe.call_count, 2)
        self.assertFalse(report['input_dispatched'])

    def test_minimized_window_stops_once_and_only_restores_on_explicit_request(self):
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        self.observable.side_effect = app.bridge.BridgeError('target_window_minimized')
        with patch.object(app.visibility, 'restore_for_action', return_value=True) as restore:
            result = recovery.run(app, self.args(prepare=True))
            self.assertEqual(result['diagnostic'], 'target_window_minimized')
            self.assertEqual(result['host_action'], 'show_recorded_window_then_retry')
            self.assertEqual(result['retry_args'], ['recover', '--task', '내 녹화', '--prepare'])
            self.assertLess(list(result).index('diagnostic'), list(result).index('steps'))
            self.assertFalse(result['input_dispatched'])
            self.observe.assert_not_called()
            self.observable.assert_called_once_with(self.owner)
            fingerprint = recovery._fingerprint(tasks.read_document(self.args().task, self.directory))
            result = self.bind(fingerprint, self.patches())
            self.assertEqual(result['retry_args'], ['recover', '--task', '내 녹화',
                                                    '--bindings', str(self.inputs), '--save-as', '실행용'])
            self.observe.assert_not_called()
            restore.assert_not_called()
            result = recovery.run(app, self.args(restore_window=True))
            self.assertEqual(result['status'], 'observed')
            self.assertTrue(result['window_restored'])
            restore.assert_called_once_with(self.owner, '업무')

    def test_demonstrated_result_area_can_bind_only_exact_point_on_a_single_table(self):
        sentence = '데이터 조회 결과가 나오면 이 영역에서 우클릭해줘'
        self.rows[3]['form']['text'] = sentence
        self.rows[3]['step']['menu_window_point'] = [.4, .6]
        self.assertFalse(recovery._any_body_cell(sentence))
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        table = dict(role='table', automation_id='table', enabled=True, offscreen=False)
        self.observe.return_value['targets'].append(table)
        self.assertEqual(self.recover_cli('--prepare')['status'], 'recorded')
        grounded = self.menu.call_args.args[-1]
        self.assertEqual((grounded['target'], grounded['menu_window_point']), ('table', [.4, .6]))
        self.assertNotIn('point_policy', grounded)
        self.observe.return_value['targets'].append(dict(table, automation_id='otherTable'))
        self.menu.reset_mock()
        self.assertEqual(self.recover_cli('--prepare')['status'], 'needs_grounding')
        self.menu.assert_not_called()

    def test_bad_source_revision_does_not_open_windows_or_publish(self):
        self.probe()
        self.observe.reset_mock()
        with self.assertRaisesRegex(tasks.TaskError, 'recording_draft_changed'):
            self.bind('a'*64, self.patches())
        self.observe.assert_not_called()
        self.assertFalse((self.directory/'실행용.json').exists())

    def test_window_missing_returns_recorded_text_and_live_diagnostic(self):
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        with patch.object(app, 'pick_window', side_effect=tasks.TaskError('window_not_found')):
            result = recovery.run(app, self.args(only=1, image=True))
        self.assertEqual(result['steps'][0]['text'], self.rows[0]['form']['text'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['diagnostic'], 'window_not_found')

    def test_protected_tail_and_cross_window_are_not_swallowed_by_collapsing_path(self):
        for reason, scope in [('protected_input_not_recorded', None), ('popup_needs_live_binding',
                              dict(mode='existing', exe='Child.exe', title='Other'))]:
            with self.subTest(reason=reason):
                raw = deepcopy(self.rows)
                raw[-1]['step'] = teach_capture.manual(reason, '6')
                if scope:
                    raw[-1]['step']['_capture_window'] = scope
                existing = review_drafts.ReviewStorage('내 녹화', 'Demo.exe', '업무', self.directory)
                existing.save_draft(raw, 'instruction_target_mismatch', 1)
                doc = tasks.read_document('내 녹화', self.directory)
                result = self.bind(recovery._fingerprint(doc), self.patches())
                self.assertEqual(result['diagnostic'], 'menu_step_range_mismatch')
                self.assertFalse((self.directory/'실행용.json').exists())

    def test_required_parameter_cannot_be_accidentally_frozen(self):
        info = self.probe()
        ops = self.patches()
        ops[1].update(mode='fixed', argument='Hybrid')
        self.assertEqual(self.bind(info['source_fingerprint'], ops)['diagnostic'], 'instruction_binding_conflict')

    def test_explicit_region_is_proved_once_and_kept_for_replay(self):
        info = self.probe()
        self.observe.reset_mock()
        ops = self.patches()
        ops[0].update(match='automation_id', within='content')
        field = self.observe.return_value['targets'][0]
        parent = dict(role='group', automation_id='psView')
        selector = dict(role='combobox', match='automation_id', target='c_status', ancestors=[parent])
        field.update(binding_scopes={'content': selector})
        self.observe.return_value['targets'].append(dict(field, binding_scopes={}))
        bad = [dict(ops[0], within='not present'), *ops[1:]]
        self.assertEqual(self.bind(info['source_fingerprint'], bad)['diagnostic'], 'target_missing_in_scope')
        self.observe.reset_mock()
        result = self.bind(info['source_fingerprint'], ops)
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(self.observe.call_count, 1)
        command = tasks.load_task('실행용', self.directory)['steps'][0]
        self.assertEqual(command['ancestors'], [parent])
        self.assertEqual(command['within'], 'psView')
        self.assertEqual(command['target'], 'c_status')

    def test_reversed_or_unrelated_menu_path_cannot_consume_user_steps(self):
        info = self.probe()
        for path in (['Active sheet', 'Export to Excel'], ['Export to Excel', 'All sheets']):
            with self.subTest(path=path):
                ops = self.patches()
                ops[-1]['path'] = path
                result = self.bind(info['source_fingerprint'], ops)
                self.assertEqual(result['diagnostic'], 'menu_step_range_mismatch')
                self.assertFalse((self.directory/'실행용.json').exists())
        self.menu.assert_not_called()

    def five_step_draft(self):
        self.rows[4:] = [dict(step=teach_capture.manual('capture_target_unavailable', '5'), form=dict(
            alias='', text='Export to excel > Active sheet버튼 눌러줘', mode=None, argument=''))]
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)

    def recover_cli(self, *options):
        args = app.parser().parse_args(['recover', '--task', '내 녹화', *options])
        return app.run(args)

    def test_public_recover_folds_exact_menu_route_from_0417_raw_editor(self):
        self.five_step_draft()
        source = (self.directory / '내 녹화.json').read_bytes()
        info = self.recover_cli()
        row = info['steps'][4]
        self.assertEqual((row['action'], row['match'], row['role'], row['target']),
                         ('open', 'query', 'button', 'Export to excel > Active sheet'))
        path = ['Export to excel', 'Active sheet']
        hint = dict(number=4, through=5, mode='context-menu', path=path)
        self.assertEqual(info['steps'][3]['menu_binding_hint'], hint)
        self.assertEqual(info['steps'][3]['image_args'], ['recover', '--task', '내 녹화', '--only', '4', '--image'])
        image = self.recover_cli('--only', '4', '--image')
        self.assertEqual(image['steps'][0]['menu_binding_hint'], hint)
        self.observe.assert_called_with(app, self.owner, self.owner, '업무', frame_key=[info['source_fingerprint'], 4])
        binding = dict(hint, target='results', role='group', frame='001', point=[10,30])
        self.menu.return_value['menu']['path'] = path
        operations = [*info['bindings_template']['steps'], binding]
        self.inputs.write_text(json.dumps(dict(source_fingerprint=info['source_fingerprint'], steps=operations)), encoding='utf8')
        result = self.recover_cli('--bindings', str(self.inputs), '--save-as', '실행용')
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(result['source_steps'], {1:1, 2:2, 3:3, 4:4, 5:4})
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(tasks.load_task('실행용', self.directory)['steps'][3]['context']['menu']['path'], path)
        self.assertEqual(self.menu.call_args.args[-1], dict(binding, point_policy='any_data_cell'))
        self.assertEqual((self.directory / '내 녹화.json').read_bytes(), source)

    def test_complete_route_on_missing_capture_is_recoverable_after_normal_save(self):
        self.five_step_draft()
        rows = self.rows[3:]
        edits = teach_capture.StepEdits()
        for row in rows:
            if row['step']['id'] != '4':
                edits.store(row['step'], '', row['form']['text'])
        source = tasks.from_recording('저장된 녹화', 'Demo.exe', '업무', edits.apply([row['step'] for row in rows]))
        tasks.save_task(source, self.directory)
        result = app.run(app.parser().parse_args(['recover', '--task', source['name']]))
        hint = result['steps'][0]['menu_binding_hint']
        self.assertEqual(hint['through'], 2)
        self.assertEqual(result['steps'][1]['match'], 'query')
        saved = self.bind(result['source_fingerprint'], [dict(self.patches()[-1], **hint)], task=source['name'])
        self.assertEqual(saved['source_steps'], {1:1, 2:1})

    def test_grounded_recorded_anchor_includes_menu_binding_in_template(self):
        self.rows[3]['step']['menu_anchor'] = dict(target=dict(role='group', automation_id='results'), point=[.3,.5])
        self.observe.return_value['targets'].append(dict(role='group', automation_id='results', enabled=True, offscreen=False))
        self.five_step_draft()
        info = self.recover_cli()
        template = info['bindings_template']
        hint = info['steps'][3]['menu_binding_hint']
        self.assertEqual(template['steps'][-1], hint)
        self.assertNotIn('image_args', info['steps'][3])
        self.assertEqual(self.bind(template['source_fingerprint'], template['steps'])['status'], 'recorded')
        self.assertEqual(self.menu.call_args.args[-1]['menu_anchor'], self.rows[3]['step']['menu_anchor'])

    def test_one_prepare_on_previous_six_step_draft_uses_one_inventory_and_preserves_source(self):
        self.rows[3]['step']['menu_anchor'] = dict(target=dict(role='group', automation_id='results'), point=[.3,.5])
        self.observe.return_value['targets'].append(dict(role='group', automation_id='results', enabled=True, offscreen=False))
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        original = (self.directory/'내 녹화.json').read_bytes()
        result = self.recover_cli('--prepare')
        self.assertEqual(result['status'], 'recorded', result)
        self.observe.assert_called_once()
        self.assertEqual(result['source_steps'], {1:1, 2:2, 3:3, 4:4, 5:4, 6:4})
        self.assertEqual(result['execution_plan']['required_parameters'], ['제품군'])
        self.assertEqual(self.menu.call_args.args[-1]['point_policy'], 'any_data_cell')
        self.assertEqual(self.recover_cli('--prepare')['task']['name'], result['task']['name'])
        self.assertEqual((self.directory/'내 녹화.json').read_bytes(), original)

    def field_and_export_task(self, first_sentence):
        # Persist the shape emitted by the old recorder, including its
        # instruction_draft on an otherwise identified combo and two popup queries.
        rows = [tasks.instructed_step(combo(identifier='c_status'), dict(mode='draft', text=first_sentence)),
                tasks.instructed_step(combo('Family', 'c_family', 'Family에 Hybrid 입력'),
                                      dict(mode='draft', text="Family 조건에 'Hybrid'선택")),
                tasks.recorded_step(button()),
                dict(action='manual', reason='right_click_requires_menu_teaching',
                     instruction_draft='조회 결과 테이블에서 우클릭',
                     menu_anchor=dict(target=dict(role='group', automation_id='results'), point=[.37, .22]))]
        for caption in ('Export to excel', 'Active Sheet'):
            rows.append(dict(action='open', role='button', match='query', expand=False, target=caption,
                             instruction=dict(mode='recorded', text=caption + ' 클릭')))
        tasks.save_task(dict(schema=1, name='내 녹화', exe='Demo.exe', window='업무', steps=rows), self.directory)
        self.observe.return_value['targets'].append(dict(role='group', automation_id='results',
                                                        enabled=True, offscreen=False))
        self.menu.return_value['menu'].update(point=[.37, .22], path=['Export to excel', 'Active Sheet'])
        return rows

    def test_existing_example_with_runtime_question_prepares_in_one_observation(self):
        sentence = "MES Setup 조건에  'N'을 입력해줘 (이건 실행 전 어떤 값을 넣을 지 물어봐줘)"
        self.field_and_export_task(sentence)
        original = (self.directory/'내 녹화.json').read_bytes()
        result = self.recover_cli('--prepare')
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(result['execution_plan']['required_parameters'], ['MES Setup'])
        self.assertEqual(result['source_steps'], {1:1, 2:2, 3:3, 4:4, 5:4, 6:4})
        steps = tasks.load_task(result['task']['name'], self.directory)['steps']
        self.assertEqual(steps[0]['instruction'], dict(text=sentence, mode='required', parameter='MES Setup'))
        self.assertEqual(steps[1]['instruction']['value'], 'Hybrid')
        self.assertEqual(steps[-1]['context']['menu']['path'], ['Export to excel', 'Active Sheet'])
        self.assertEqual((self.directory/'내 녹화.json').read_bytes(), original)
        self.observe.assert_called_once()
        self.menu.assert_called_once()
        self.assertFalse(result['input_dispatched'])

    def test_incomplete_plan_returns_reusable_valid_bindings_and_diagnoses_extra_menu_keys(self):
        rows = self.field_and_export_task('실행 환경을 고려해 정할 값')
        info = self.recover_cli('--prepare')
        self.assertEqual((info['diagnostic'], info['step_number']), ('instruction_requires_structure', 1))
        template = info['bindings_template']
        self.assertEqual([item['number'] for item in template['steps']], [2, 3, 4])
        menu = template['steps'][-1]
        self.assertEqual(menu, dict(number=4, through=6, mode='context-menu', path=['Export to excel', 'Active Sheet']))
        fields = [dict(self.patches()[0], mode='required', argument='State'), *template['steps'][:-1]]
        bad = self.bind(template['source_fingerprint'], [*fields, dict(menu, extra='ignored?')])
        self.assertEqual((bad['step_number'], bad['unsupported_fields'], bad['menu_binding_hint']), (4, ['extra'], menu))
        wrong_mode = self.bind(template['source_fingerprint'], [*fields, dict(menu, mode='recorded')])
        self.assertEqual(wrong_mode['menu_binding_hint'], menu)
        self.assertIn('context-menu', wrong_mode['recovery_hint'])
        changed = self.bind(template['source_fingerprint'], [*fields, dict(menu, menu_anchor={'point':[0.,0.]})])
        self.assertEqual(changed['diagnostic'], 'context_region_changed')
        accepted = self.bind(template['source_fingerprint'], [*fields, dict(menu, menu_anchor=rows[3]['menu_anchor'])])
        self.assertEqual(accepted['execution_plan']['required_parameters'], ['State'])
        self.assertFalse(accepted['input_dispatched'])

    def test_one_prepare_on_legacy_task_with_separate_unidentified_popup_queries(self):
        self.rows[3]['step']['menu_anchor'] = dict(target=dict(role='group', automation_id='results'), point=[.3,.5])
        self.observe.return_value['targets'].append(dict(role='group', automation_id='results', enabled=True, offscreen=False))
        rows = self.rows[3:]
        edits = teach_capture.StepEdits()
        for row in rows:
            if row['step']['id'] != '4':
                label = 'Export to excel' if row['step']['id'] == '5' else 'Active sheet'
                row['step'] = teach_capture.manual('capture_target_unavailable', row['step']['id'])
                edits.store(row['step'], '', label + ' 버튼을 눌러줘')
        source = tasks.from_recording('old-task', 'Demo.exe', '업무', edits.apply([row['step'] for row in rows]))
        self.assertEqual([s.get('match') for s in source['steps']], [None, 'query', 'query'])
        tasks.save_task(source, self.directory)
        args = app.parser().parse_args(['recover', '--task', 'old-task', '--prepare'])
        result = app.run(args)
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(result['source_steps'], {1:1, 2:1, 3:1})
        self.assertEqual(self.menu.call_args.args[-1]['path'], ['Export to excel', 'Active sheet'])

    def test_prepare_rejects_guessed_captions_that_belong_to_live_buttons(self):
        self.rows[3]['step']['menu_anchor'] = dict(target=dict(role='group', automation_id='results'), point=[.3,.5])
        self.observe.return_value['targets'].extend([
            dict(role='group', automation_id='results', enabled=True, offscreen=False),
            dict(role='button', automation_id='toolbar', name='Export to Excel', enabled=True, offscreen=False)])
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        result = self.recover_cli('--prepare')
        self.assertEqual(result['status'], 'needs_grounding', result)
        self.assertTrue(result['steps'][3]['capture_requires_consent'])
        self.menu.assert_not_called()

    def test_captured_mail_menu_beats_same_named_toolbar_and_keeps_specific_column(self):
        title, task = 'Mail Archive', '보관함에서 복사'
        anchor = dict(target=dict(role='table', automation_id='messageList'), point=[.67, .24])
        captions = ['Message', 'Copy', 'Message ID']
        rows = [dict(action='manual', reason='right_click_requires_menu_teaching',
                     instruction_draft='메시지 번호 열에서 우클릭', menu_anchor=anchor)]
        for caption in captions:
            rows.append(dict(action='open', role='menuitem', match='name', target=caption,
                             expand=False,
                             instruction=dict(mode='recorded', text=caption + ' 클릭')))
        tasks.save_task(dict(schema=1, name=task, exe='MailArchive.exe', window=title, steps=rows), self.directory)
        self.owner.exe = 'MailArchive.exe'
        app.windows.return_value = [dict(hwnd=1, title=title)]
        self.observe.return_value['targets'] = [dict(anchor['target'], enabled=True, offscreen=False),
            dict(role='button', automation_id='copyToolbar', name='Copy', enabled=True, offscreen=False)]
        self.menu.return_value['menu'] = dict(anchor, path=captions)
        prepared = app.run(app.parser().parse_args(['recover', '--task', task, '--prepare']))
        self.assertEqual(prepared['status'], 'recorded', prepared)
        self.assertEqual(prepared['source_steps'], {1: 1, 2: 1, 3: 1, 4: 1})
        self.observe.assert_called_once()
        self.menu.assert_called_once()
        binding = self.menu.call_args.args[-1]
        self.assertEqual((binding['menu_anchor'], binding['path']), (anchor, captions))
        self.assertNotIn('point_policy', binding)

    def test_edited_caption_does_not_silently_replay_old_captured_menu(self):
        old = dict(action='open', role='menuitem', match='name', target='Copy')
        for text in ('Archive 클릭', 'Archive 선택', 'Copy 클릭하지 마'):
            with self.subTest(text=text):
                self.assertEqual(recovery._menu_tail(dict(step=old, text=text, mode=None)), [])
        quiet = dict(old, target='Do Not Disturb')
        self.assertEqual(recovery._menu_tail(dict(step=quiet, text='Click Do Not Disturb', mode=None)),
                         ['Do Not Disturb'])
        self.assertEqual(recovery._menu_tail(dict(step=quiet, text='Do not click Do Not Disturb', mode=None)), [])

    def test_prepare_reports_a_missing_recorded_field_before_publishing_the_copy(self):
        self.observe.return_value['targets'].pop(1)
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        result = self.recover_cli('--prepare', '--save-as', '준비본')
        self.assertEqual((result['status'], result['step_number'], result['diagnostic']),
                         ('needs_grounding', 2, 'target_missing'))
        self.assertEqual(result['steps'][1]['target'], 'c_family')
        self.assertEqual(result['steps'][1]['scope_index'], 0)
        self.assertFalse((self.directory/'준비본.json').exists())

    def test_prose_after_last_menu_item_remains_a_separate_unresolved_step(self):
        self.rows[3]['step']['menu_anchor'] = dict(target=dict(role='group', automation_id='results'), point=[.3,.5])
        self.observe.return_value['targets'].append(dict(role='group', automation_id='results', enabled=True, offscreen=False))
        self.rows.append(dict(step=teach_capture.manual('unsupported_recorded_action', '7'),
                              form=dict(alias='', text='상태에 맞춰 후속 처리', mode=None, argument='')))
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        result = self.recover_cli('--prepare', '--save-as', '준비본')
        self.assertEqual((result['status'], result['step_number']), ('needs_grounding', 7), result)
        hint = result['steps'][3]['menu_binding_hint']
        self.assertEqual((hint['through'], hint['path']), (6, ['Export to Excel', 'Active sheet']))
        self.assertFalse((self.directory/'준비본.json').exists())

    def test_dependent_step_identifies_missing_opener_without_reusing_a_step_number(self):
        self.observe.return_value['targets'].pop(2)
        step = teach_capture.manual('capture_target_unavailable', '4')
        step['_capture_window'] = dict(after_id='3', exe='Child.exe', title='Details')
        self.rows[3:] = [dict(step=step, form=dict(alias='', text='Open 버튼을 눌러줘', mode=None, argument=''))]
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        result = self.recover_cli('--prepare')
        self.assertEqual((result['step_number'], result['diagnostic']), (3, 'target_missing'), result)
        self.assertEqual((result['steps'][3]['diagnostic'], result['steps'][3]['opener_step']),
                         ('recorded_window_opener_unresolved', 3))

    def test_specific_rows_and_cells_are_never_generic_point_repairs(self):
        self.assertTrue(recovery._any_body_cell('조회된 테이블에서 우클릭을 눌러줘'))
        self.assertTrue(recovery._any_body_cell('결과 표를 우클릭해줘'))
        for text in ('4번째 행의 셀을 우클릭해줘', '결과 표에서 특정 행을 우클릭', 'right click selected row'):
            self.assertFalse(recovery._any_body_cell(text))

    def test_ordinary_right_click_accepts_complete_path_even_if_popup_row_was_missed(self):
        raw = self.rows[3]['step']
        raw['menu_anchor'] = dict(target=dict(role='group', automation_id='results'), point=[.3,.5])
        text = 'Export to excel > Active sheet버튼 눌러줘'
        edits = teach_capture.StepEdits()
        edits.store(raw, '', text)
        source = tasks.from_recording('내 녹화', 'Demo.exe', '업무', edits.apply([raw]))
        tasks.save_task(source, self.directory)
        self.assertEqual(source['steps'][0]['instruction_draft'], text)
        self.assertEqual(source['steps'][0]['action'], 'manual')
        self.observe.return_value['targets'].append(dict(role='group', automation_id='results', enabled=True, offscreen=False))
        template = self.recover_cli()['bindings_template']
        self.assertEqual(template['steps'], [dict(number=1, through=1, path=['Export to excel', 'Active sheet'],
                                                  mode='context-menu')])
        self.assertEqual(self.bind(template['source_fingerprint'], template['steps'])['source_steps'], {1:1})

    def test_rejected_route_identifies_conflicting_tail_and_preserves_real_buttons(self):
        text = 'Export to excel > Active sheet버튼 눌러줘'
        real_button = button()
        real_button.pop('instruction', None)
        real_button.update(id='5', label='조회')
        for raw, sentence, path in [
            (teach_capture.manual('capture_target_unavailable', '5'), text, ['Active sheet', 'Export to excel']),
            (teach_capture.manual('capture_target_unavailable', '5'), text, ['Other export', 'Active sheet']),
            (teach_capture.manual('capture_target_unavailable', '5'), 'Active sheet버튼 눌러줘', ['Export to excel', 'Active sheet']),
            (real_button, text, ['Export to excel', 'Active sheet']),
        ]:
            with self.subTest(sentence=sentence, path=path, capture=raw['action']):
                self.rows[4:] = [dict(step=raw, form=dict(alias='', text=sentence, mode=None, argument=''))]
                store = review_drafts.ReviewStorage('내 녹화', 'Demo.exe', '업무', self.directory)
                store.save_draft(self.rows, 'instruction_target_mismatch', 1)
                info = self.recover_cli()
                result = self.bind(info['source_fingerprint'], [*self.patches()[:3],
                                   dict(self.patches()[-1], through=5, path=path)])
                self.assertEqual(result['diagnostic'], 'menu_step_range_mismatch', result)
                self.assertEqual(result['steps'][3]['conflicting_step'], 5)
                self.assertIn('through', result['steps'][3]['recovery_hint'])
                self.assertFalse((self.directory / '실행용.json').exists())
        self.menu.assert_not_called()

    def test_different_observed_control_requires_supporting_user_wording(self):
        info = self.probe()
        ops = self.patches()
        ops[0]['target'] = 'Family'
        self.assertEqual(self.bind(info['source_fingerprint'], ops)['diagnostic'],
                         'live_target_does_not_match_instruction')
        self.assertFalse((self.directory/'실행용.json').exists())

    def test_changed_revision_during_live_read_never_publishes(self):
        info = self.probe()
        def while_reading(*args, **kwargs):
            other = review_drafts.ReviewStorage('내 녹화', 'Demo.exe', '업무', self.directory)
            rows = other.initial_rows()
            rows[0]['form']['text'] = '작업자가 바꾼 설명'
            other.save_draft(rows, 'instruction_target_mismatch', 1)
            return self.observe.return_value
        self.observe.side_effect = while_reading
        with self.assertRaisesRegex(tasks.TaskError, 'recording_draft_changed'):
            self.bind(info['source_fingerprint'], self.patches())
        self.assertFalse((self.directory/'실행용.json').exists())

    def test_derived_scope_after_folded_tail_keeps_its_opener(self):
        raw = button()
        raw.pop('instruction')
        raw.update(id='7', label='조회', _capture_window={'exe':'Viewer.exe','title':'미리보기','after_id':'6'})
        self.rows.append(dict(step=raw, form=dict(alias='', text='조회 버튼을 눌러줘', mode=None, argument='')))
        # The child is not open during registration. Its locator was already
        # recorded; validating the previous menu operation is sufficient here.
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        doc = tasks.read_document('내 녹화', self.directory)
        result = self.bind(recovery._fingerprint(doc), self.patches())
        self.assertEqual(result['status'], 'recorded', result)
        final = tasks.load_task('실행용', self.directory)
        self.assertEqual(final['steps'][-1]['window_context'], {'exe':'Viewer.exe','title':'미리보기','after_step':4})
        self.assertFalse(result['input_dispatched'])

    def test_existing_regular_task_and_menu_only_supplement_survive_repair(self):
        # Old installations may already have converted all 3 notes to canonical
        # manual steps. Recovery uses the same in-scope route without rerecording.
        raw = []
        for row in self.rows[3:]:
            step = deepcopy(row['step'])
            step['instruction_draft'] = row['form']['text']
            raw.append(step)
        original = tasks.from_recording('추가 녹화', 'Demo.exe', '업무', raw)
        tasks.save_task(original, self.directory)
        info = recovery.run(app, self.args(task=original['name']))
        spec = dict(self.patches()[-1], number=1, through=3)
        saved = self.bind(info['source_fingerprint'], [spec], task=original['name'])
        self.assertEqual(saved['status'], 'recorded', saved)
        self.assertEqual(tasks.load_task(original['name'], self.directory), original)
        self.assertEqual(tasks.load_task('실행용', self.directory)['steps'][0]['context']['menu']['path'], spec['path'])

    def test_ordinary_menu_edit_supersedes_its_old_path_and_keeps_anchor(self):
        context = deepcopy(self.menu.return_value)
        raw = dict(id='anchor', action='result-menu', ordinary_menu=True, label='표 → 옛 메뉴', context=context)
        edited = '같은 표에서 새 메뉴 선택'
        self.rows[:] = [dict(step=raw, form=dict(alias='', text=edited, mode=None, argument=''))]
        self.store.save_draft(self.rows, 'instruction_target_mismatch', 1)
        current = recovery.run(app, self.args())
        detail = current['steps'][0]
        self.assertEqual(detail['text'], edited)
        self.assertEqual(detail['description'], raw['label'])
        self.assertEqual(detail['reason'], 'right_click_requires_menu_teaching')
        self.assertEqual(detail['menu_anchor']['point'], [.3,.5])


class RecoveryScopeInventoryTests(unittest.TestCase):
    def test_only_scan_popups_for_menu_or_unresolved_recorded_selectors(self):
        import live_recovery
        from contextlib import ExitStack
        surface = SimpleNamespace(hwnd=1, exe='Demo.exe')
        for captions, failure, expected_scans in (([], False, 0), (['Export'], False, 1), ([], True, 2)):
            with self.subTest(captions=captions, failure=failure), ExitStack() as stack:
                for source, name in ((app.bridge, 'UIA'), (app, 'guard'), (app.bridge, 'window_text'),
                                     (app, 'bind_surface'), (app.visibility, 'require_observable')):
                    stack.enter_context(patch.object(source, name))
                popups = stack.enter_context(patch.object(app, 'owned_popups', return_value=[dict(hwnd=2)]))
                menu = stack.enter_context(patch.object(live_recovery.context_menu, '_native_menu_owner', return_value=False))
                inventory = stack.enter_context(patch.object(live_recovery, '_targets', return_value=[]))
                direct = stack.enter_context(patch.object(live_recovery, '_recorded_targets', return_value=[]))
                if failure:
                    direct.side_effect = app.bridge.BridgeError('target_missing')
                result = live_recovery.observe_scope(app, surface, surface, '업무', lookups=dict(
                    captions=captions, selectors=[dict(automation_id='code', role='textfield')]))
                direct.assert_called_once()
                self.assertEqual(inventory.call_count, expected_scans)
                self.assertEqual(popups.call_count, int(bool(expected_scans)))
                self.assertEqual(menu.call_count, popups.call_count)
                self.assertFalse(result['input_dispatched'])
