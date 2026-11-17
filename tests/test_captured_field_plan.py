"""New recordings retain input intent and future dialog stages without private app rules."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import learned_tasks as tasks
import recorded_plan
import recovery
import review_drafts


def field(action='set-value'):
    value = dict(action=action, id='input', label='조회 필터',
                 target=dict(kind='element', locator=dict(anchor=dict(role='group', name='Result view'),
                             point=[.3,.1], role='textfield', class_name='NativeEditor')),
                 recognition=dict(target='identified', action='supported', result='checkable'))
    if action == 'set-value':
        value['value'] = "Status = 'N' AND Family = 'Hybrid'"
    return value


class CapturedPlanTests(unittest.TestCase):
    def test_late_completed_menu_resolves_the_real_draft_window_candidates(self):
        import capture_trail
        import teach_capture
        first = teach_capture.manual('stable_locator_unavailable','unknown')
        text, enter = field(), field('submit')
        enter['id'] = 'enter'
        menu = dict(id='menu',action='result-menu',label='우클릭 → Export',
                    context=dict(trigger=None,ready=None,output=None,
                        menu=dict(target=dict(role='group',name='Result view'),point=[.4,.4],path=['Export'])))
        scope = dict(exe='Example.exe',title='Transfer',
                     _openers=['unknown','enter','menu'],related=True)
        choice = dict(id='format',action='select',label='Spreadsheet',
                      target=dict(kind='element',locator=dict(role='listitem',name='Spreadsheet')),
                      recognition=dict(action='supported',target='identified'),_capture_window=scope)
        raw = [first,text,enter,menu,choice]
        original = deepcopy(raw)
        self.assertEqual(capture_trail.finalize_contexts(raw)[-1]['window_context']['after_step'],4)
        doc = dict(kind=review_drafts.KIND,schema=1,name='원본',exe='Example.exe',window='Data window',
                   diagnostic='recorded_window_opener_required',step_number=5,
                   rows=[dict(step=s,form=dict(alias='',text=teach_capture.default_text(s),mode=None,argument=''))
                         for s in raw])
        parsed = recovery._rows(doc)[-1]
        self.assertNotIn('scope_problem',parsed)
        self.assertEqual(parsed['context']['after_step'],4)
        self.assertEqual(raw,original)

    def test_late_menu_resolution_keeps_ambiguous_or_unfinished_window_origins(self):
        import capture_trail
        menu = dict(id='menu',action='result-menu',label='우클릭 → Export',
                    context=dict(trigger=None,ready=None,output=None,
                        menu=dict(target=dict(role='group',name='Result view'),point=[.4,.4],path=['Export'])))
        earlier = field('submit')
        earlier['id'] = 'enter'
        scope = dict(exe='Example.exe',title='Transfer',_openers=['enter','menu'],related=True)
        different = dict(earlier,_capture_window=dict(exe='Other.exe',title='Another',mode='existing'))
        unfinished = dict(menu,action='manual')
        for candidate_steps, candidate_scope in [
            ([earlier,unfinished],scope),
            ([different,menu],scope),
            ([earlier,menu],dict(scope,related=False)),
            ([earlier,menu,dict(id='later',action='click')],dict(scope,_openers=['enter','menu','later'])),
        ]:
            with self.subTest(candidates=candidate_steps):
                self.assertEqual(capture_trail.resolve_menu_scope(candidate_scope,candidate_steps,4),candidate_scope)

    def test_mixed_draft_inventory_resolves_unnamed_field_once_for_value_and_enter(self):
        import live_recovery
        step = tasks.recorded_step(field())
        inventory = dict(targets=[])
        owner = Mock()
        observed = dict(recorded_selector=step['selector'],role='textfield',enabled=True,offscreen=False)
        with (patch.object(app.bridge,'UIA'),
              patch.object(live_recovery,'_recorded_targets',return_value=[observed]) as targeted):
            recovery._observe_command(step,inventory,app,owner)
            recovery._observe_command(tasks.recorded_step(field('submit')),inventory,app,owner)
        targeted.assert_called_once()
        self.assertEqual(inventory['targets'],[observed])

    def test_mixed_draft_reports_actual_occlusion_instead_of_missing_unnamed_field(self):
        import live_recovery
        step = tasks.recorded_step(field())
        inventory = dict(targets=[])
        with (patch.object(app.bridge,'UIA'),
              patch.object(live_recovery,'_recorded_targets',
                           side_effect=app.bridge.BridgeError('anchored_target_occluded')) as targeted):
            for _ in range(2):
                with self.assertRaisesRegex((tasks.TaskError,app.bridge.BridgeError),'anchored_target_occluded'):
                    recovery._observe_command(step,inventory,app,Mock())
        targeted.assert_called_once()
        self.assertEqual(inventory['targets'],[])

    def test_default_editor_text_compiles_complete_new_recording_with_future_wizard(self):
        import capture_trail
        import teach_capture
        first, submit = field(), field('submit')
        submit['id'] = 'enter'
        menu = dict(id='menu', action='result-menu', label='우클릭 → Export',
                    context=dict(trigger=None, ready=None, output=None,
                                 menu=dict(target=dict(role='group', name='Result view'),
                                           point=[.4,.4], path=['Export'])))
        context = dict(exe='Example.exe', title='Transfer', after_id='menu', related=True)
        choice = dict(id='format',action='select',label='Spreadsheet',
                      target=dict(kind='element',locator=dict(role='listitem',name='Spreadsheet')),
                      recognition=dict(action='supported',target='identified'),_capture_window=context)
        directory = field()
        directory.update(id='directory',label='Directory',value=r'C:\Reports',
                         target=dict(kind='element', locator=dict(anchor=dict(role='window',name='Transfer'),
                                     point=[.6,.3],role='textfield',class_name='NativeEditor')),
                         _capture_window=context)
        def button(identifier, name):
            return dict(id=identifier, action='click',label=name,
                        target=dict(kind='element',locator=dict(role='button',name=name)),
                        recognition=dict(action='supported',target='identified'),_capture_window=context)
        raw = [first,submit,menu,choice,*[button('next'+str(i),'Next >') for i in range(3)],
               directory,button('proceed','Proceed')]
        steps = capture_trail.finalize_contexts(teach_capture.StepEdits().apply(raw))
        task = tasks.from_recording('새 업무', 'Example.exe', 'Data window', steps)
        plan = recorded_plan.compile_plan(task)
        self.assertEqual(plan['status'],'compiled',plan)
        actual = plan['envelope']['recipe']['steps']
        self.assertEqual([s['action'] for s in actual],
                         ['set-value','submit','result-menu','choose-item',
                          'invoke','invoke','invoke','set-value','invoke'])
        self.assertEqual(actual[0]['value'],first['value'])
        self.assertEqual(actual[7]['value'],directory['value'])
        self.assertTrue(all(s['window_context']['after_step'] == 3 for s in actual[3:]))
        self.assertEqual(actual[3]['selector']['name'],'Spreadsheet')
        lookups = recovery._recorded_lookups(recovery._rows(task))
        self.assertEqual(lookups['selectors'], [first['target']['locator'],menu['context']['menu']['target']])

    def test_unnamed_value_enter_and_native_format_survive_compile(self):
        raw = field()
        raw['instruction'] = tasks.compile_instruction(raw,
                         '조회 필터 입력란을 "Status = \'N\' AND Family = \'Hybrid\'"로 설정해줘')
        submit = field('submit')
        submit['instruction'] = tasks.compile_instruction(submit, '조회 필터에서 Enter를 눌러줘')
        item = dict(action='select', label='Spreadsheet', target=dict(kind='element',
                    locator=dict(role='listitem', name='Spreadsheet')),
                    recognition=dict(target='identified', action='supported', result='checkable'))
        item['instruction'] = tasks.compile_instruction(item, 'Spreadsheet 선택해줘')
        task = tasks.from_recording('신규 녹화', 'Example.exe', 'Data window', [raw, submit, item])
        self.assertEqual([s['action'] for s in task['steps']], ['field-input','submit','choose-item'])
        result = recorded_plan.compile_plan(task)
        self.assertEqual(result['status'], 'compiled', result)
        steps = result['envelope']['recipe']['steps']
        self.assertEqual([s['action'] for s in steps], ['set-value','submit','choose-item'])
        self.assertEqual(steps[0]['value'], raw['value'])
        self.assertEqual(steps[0]['selector'], raw['target']['locator'])

    def test_required_value_remains_required_and_repeated_names_share_input(self):
        raw = field()
        raw['instruction'] = tasks.compile_instruction(raw, '입력받은 변수를 조회 필터 입력란에 입력해줘',
                                                       mode='required', argument='조건')
        task = tasks.from_recording('변수', 'Example.exe', 'Data window', [raw, deepcopy(raw)])
        result = recorded_plan.compile_plan(task)
        self.assertEqual(result['status'], 'compiled', result)
        self.assertEqual([s['parameter'] for s in result['envelope']['recipe']['steps']], ['조건','조건'])
        self.assertTrue(all('value' not in s for s in result['envelope']['recipe']['steps']))
        task['steps'][0]['parameter'] = '다른 변수'
        with self.assertRaises(tasks.TaskError):
            tasks._validate(task)

    def test_capture_cannot_promote_protected_input_or_unwitnessed_value(self):
        for change in (dict(secure=True), dict(recognition=dict(target='identified', action='supported', result='unavailable'))):
            raw = dict(field(), **change)
            self.assertEqual(tasks.recorded_step(raw)['action'], 'manual')
        task = tasks.from_recording('새 입력', 'Example.exe', 'Data window', [field()])
        task['steps'][0]['selector']['point'] = [1.2,.1]
        with self.assertRaises(ValueError):
            tasks._validate(task)

    def test_natural_edits_cannot_send_enter_or_select_a_different_item(self):
        raw = field('submit')
        for text in ('Enter를 누르지 마', 'Enter 대신 Tab을 눌러줘', '다른 필드에서 Enter를 눌러줘'):
            self.assertEqual(tasks.compile_instruction(raw, text)['mode'], 'draft', text)
        choice = dict(action='select',label='Spreadsheet', target=dict(kind='element',
                      locator=dict(role='listitem',name='Spreadsheet')),
                      recognition=dict(action='supported',target='identified'))
        self.assertEqual(tasks.compile_instruction(choice,'Spreadsheet 형식을 선택해줘')['mode'],'recorded')
        self.assertEqual(tasks.compile_instruction(choice,'CSV 형식을 선택해줘')['mode'],'draft')

    def test_editing_saved_field_into_a_freeform_draft_preserves_the_description(self):
        raw = field()
        saved = tasks.instructed_step(raw, tasks.compile_instruction(raw, '이 입력란을 "example"로 설정해줘'))
        text = '이번 화면에 맞춰 조회 조건을 결정해줘'
        revised = tasks.instructed_step(saved, dict(mode='draft',text=text))
        self.assertNotIn('instruction',revised)
        self.assertEqual(revised['instruction_draft'],text)
        task = dict(schema=1,name='수정',exe='Example.exe',window='Data window',steps=[revised])
        tasks._validate(task)
        self.assertEqual(recorded_plan.compile_plan(task)['diagnostic'],'instruction_requires_structure')

    def test_failed_form_with_recorded_enter_saves_and_reopens_as_a_draft(self):
        import teach_capture
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            submit = field('submit')
            submit['id'] = 'enter'
            rows = [dict(step=raw, form=dict(alias='',text=text,mode=None,argument='')) for raw,text in [
                (field(), '이 입력란을 "example"로 설정해줘'),
                (submit, '이 입력란에서 Enter를 눌러 적용해줘'),
                (teach_capture.manual('capture_target_unavailable'), '상태에 맞춰 후속 처리')]]
            rows[2]['step']['_capture_window'] = dict(exe='Example.exe', title='Transfer',
                                                     _openers=['input', 'enter'], related=True)
            store = review_drafts.ReviewStorage('보완 중', 'Example.exe', 'Data window', folder)
            saved = store.save_draft(rows, 'instruction_target_unavailable', 3)
            self.assertEqual(saved['status'], 'recorded')
            restored = review_drafts.ReviewStorage('보완 중', 'Example.exe', 'Data window', folder).initial_rows()
            self.assertEqual(restored, rows)
            reopened = teach_capture.StepEdits().restore_rows(restored)
            self.assertEqual(reopened[1]['action'], 'submit')
            self.assertEqual(reopened[1]['target'], submit['target'])
            self.assertEqual(restored[2]['form']['text'], '상태에 맞춰 후속 처리')
            self.assertEqual(restored[2]['step']['_capture_window']['_openers'], ['input','enter'])

    def test_prepare_keeps_missing_related_dialog_steps_deferred(self):
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            first = field()
            opener = dict(action='click', id='open-export', label='Export',
                          target=dict(kind='element', locator=dict(role='button', name='Export')),
                          recognition=dict(action='supported', target='identified'))
            after = dict(exe='Example.exe', title='Transfer', after_id='open-export', related=True)
            choice = dict(action='select', id='format', label='Spreadsheet',
                          target=dict(kind='element', locator=dict(role='listitem', name='Spreadsheet')),
                          recognition=dict(action='supported', target='identified'), _capture_window=after)
            next_step = dict(action='click', id='next', label='Next',
                             target=dict(kind='element', locator=dict(role='button', name='Next')),
                             recognition=dict(action='supported', target='identified'), _capture_window=after)
            rows = [dict(step=s, form=dict(alias='', text=text, mode=None, argument='')) for s,text in [
                (first, '조회 필터 입력란을 "Status = \'N\' AND Family = \'Hybrid\'"로 설정해줘'),
                (opener, 'Export 버튼을 눌러줘'), (choice, 'Spreadsheet 선택해줘'), (next_step, 'Next 버튼을 눌러줘')]]
            store = review_drafts.ReviewStorage('새 녹화', 'Example.exe', 'Data window', folder)
            store.save_draft(rows, 'recording_draft_requires_review', 3)
            original = (folder/'새 녹화.json').read_bytes()
            owner = SimpleNamespace(hwnd=1,pid=2,created=3,exe='Example.exe')
            inventory = dict(targets=[
                dict(recorded_selector=first['target']['locator'], role='textfield',enabled=True,offscreen=False),
                dict(role='button', name='Export',enabled=True,offscreen=False)],owned_popups=[])
            bind = lambda _app,_doc,_owner,_rows,_seq,n: owner if n < 3 else (_ for _ in ()).throw(
                tasks.TaskError('recorded_window_not_open'))
            args=argparse.Namespace(task='새 녹화',prepare=True,only=None,image=False,bindings=None,save_as='실행')
            with (patch.object(app,'task_directory',return_value=folder),patch.object(app,'windows',return_value=[]),
                  patch.object(app,'pick_window',return_value={}),patch.object(app.bridge,'Binding',return_value=owner),
                  patch.object(app,'guard'),patch.object(app.visibility, 'require_observable'),
                  patch.object(recovery,'_bind_surface',side_effect=bind),
                  patch('live_recovery.observe_scope',return_value=inventory) as read):
                result = recovery.run(app,args)
            self.assertEqual(result['status'],'recorded',result)
            self.assertEqual([r['number'] for r in result['pending_scopes']],[3,4])
            self.assertEqual(read.call_count,1)
            self.assertEqual((folder/'새 녹화.json').read_bytes(),original)
            saved=tasks.load_task('실행',folder)
            plan=recorded_plan.compile_plan(saved)
            self.assertEqual(plan['status'],'compiled',plan)
            self.assertEqual(plan['envelope']['recipe']['steps'][2]['action'],'choose-item')
            self.assertEqual(plan['envelope']['recipe']['steps'][2]['window_context']['after_step'],2)


if __name__ == '__main__':
    unittest.main()
