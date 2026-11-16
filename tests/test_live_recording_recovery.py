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
