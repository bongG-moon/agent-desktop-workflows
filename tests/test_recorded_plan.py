"""Natural-language recording plans compile once without changing source meaning."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import learned_tasks as tasks
import recorded_plan as plans
import python_workflow as workflow


def combo(label='MES Setup', identifier='status', sentence='MES Setup을 N으로 설정'):
    raw = dict(action='teach-target', field_label=label,
               recognition=dict(target='identified', action='requires_instruction', result='manual',
                                reason='combo_choice_unproven'),
               target=dict(kind='element', locator=dict(automation_id=identifier, role='combobox',
                           ancestors=[dict(role='group', automation_id='conditions')])) )
    raw['instruction'] = tasks.compile_instruction(raw, sentence)
    return raw


def button():
    raw = dict(action='click', field_label='조회',
               recognition=dict(target='identified', action='supported', result='manual'),
               target=dict(kind='element', locator=dict(automation_id='query', name='조회', role='button',
                           ancestors=[dict(role='group', automation_id='content')])) )
    raw['instruction'] = tasks.compile_instruction(raw, '조회 버튼을 눌러 줘')
    return raw


def recording():
    return tasks.from_recording('조건 조회', 'Example.exe', 'Device window', [
        combo(), combo('Family', 'family', '입력받은 변수를 Family 조건에 입력해줘'), button()])


class CompilationTests(unittest.TestCase):
    def test_old_visual_value_check_compiles_to_current_input_without_recorded_pixels(self):
        from test_visual_target import image, visual, VisualTargets
        marker = visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162))
        source = recording()
        source['steps'] = [
            dict(action='fill', target='partInput', role='textfield', match='automation_id', label='Part ID',
                 recorded_effect='unobserved', instruction=dict(mode='required', parameter='품목', text='Part ID 입력')),
            dict(action='visual-click', visual=marker, label='기록한 셀',
                 description='조회 결과 표에서 Part ID 값이 입력한 값과 같은지 확인해줘'),
            dict(action='visual-click', visual=dict(marker, gesture='double-click'), label='기록한 행',
                 description='확인한 결과 행을 더블클릭해줘')]
        before = deepcopy(source)
        plan = plans.compile_plan(source)
        self.assertEqual(plan['status'], 'compiled', plan)
        steps = plan['envelope']['recipe']['steps']
        self.assertEqual([s['action'] for s in steps], ['set-value', 'verify-value', 'double-click-result'])
        self.assertEqual(steps[1]['parameter'], '품목')
        self.assertEqual(steps[1]['result_column'], 'Part ID')
        self.assertNotIn('visual', json.dumps(steps))
        self.assertEqual(source, before)
        for value in ('LAB-42', 'PART-A'):
            self.assertEqual(workflow.values_for(plan['envelope']['recipe'], {'품목': value}), ({'품목': value}, []))

    def test_latest_assignment_uses_control_identity_and_keeps_distinct_fields_ambiguous(self):
        check = dict(action='verify-value', parameter='Part ID', label='Part ID')
        old = dict(action='fill', target='part', match='automation_id', role='textfield', label='Part ID',
                   instruction=dict(mode='fixed', value='old'))
        current = dict(old, instruction=dict(mode='required', parameter='품목'))
        self.assertEqual(plans._expected_binding(check, [old, current]), {'parameter':'품목'})
        different = dict(current, target='anotherPart', instruction=dict(mode='fixed', value='different'))
        self.assertIsNone(plans._expected_binding(check, [old, current, different]))
        scoped = dict(current, ancestors=[dict(role='group', automation_id='otherPanel')],
                      instruction=dict(mode='fixed', value='different'))
        self.assertIsNone(plans._expected_binding(check, [current, scoped]))

    def test_child_comparison_inherits_only_its_openers_input_at_open_time(self):
        prior = dict(action='fill', target='part', match='automation_id', role='textfield', label='Part ID',
                     instruction=dict(mode='required', parameter='품목'))
        opener = dict(action='open')
        child = dict(exe='Example.exe', title='Results', after_step=2, related=True)
        check = dict(action='verify-value', parameter='Part ID', label='Part ID', window_context=child)
        changed = dict(prior, instruction=dict(mode='fixed', value='later'))
        self.assertEqual(plans._expected_binding(check, [prior, opener, changed]), {'parameter':'품목'})
        unrelated = dict(check, window_context=dict(exe='Other.exe', title='Other', mode='existing'))
        self.assertIsNone(plans._expected_binding(unrelated, [prior, opener]))
        self.assertEqual(plans._expected_binding(check, [prior, opener,
                         dict(changed, window_context=child)]), {'value':'later'})
        self.assertEqual(plans._expected_binding(dict(check, value='explicit'), [prior]), {'value':'explicit'})

    def test_unparsed_comparison_hands_off_with_its_location_instead_of_clicking_old_pixels(self):
        from test_visual_target import image, visual, VisualTargets
        marker = visual.from_frame(VisualTargets.window, image((35, 42)), (235, 162))
        for sentence in ('Compare Code with the entered part, then open the matching row',
                         '입력한 값과 맞는지 확인하고 맞으면 결과 행을 더블클릭해줘',
                         'Batch 일치 확인 후 결과 행 열기', '조회 결과 값 일치 확인 후 계속',
                         'Batch를 입력한 값과 비교 후 확인한 행 열기'):
            old = dict(action='visual-click', visual=marker, label=sentence)
            spec = tasks.compile_instruction(old, sentence)
            self.assertEqual(spec, dict(mode='draft', text=sentence))
            source = dict(recording(), steps=[old])
            resolved = tasks.resolve_saved_intents(source)['steps'][0]
            self.assertEqual(resolved['visual_hint'], marker)
            self.assertEqual(resolved['instruction_draft'], sentence)
            plan = plans.compile_plan(source)
            self.assertEqual(plan['diagnostic'], 'instruction_requires_structure')
            self.assertEqual(plan['grounding_steps'][0]['instruction'], sentence)
            self.assertNotIn('envelope', plan)
        icon = dict(action='visual-click', visual=marker, label='Compare button')
        self.assertEqual(tasks.resolve_saved_intents(dict(recording(), steps=[icon]))['steps'], [icon])
        for sentence in ('비교하기 버튼을 눌러줘', 'click Compare button'):
            self.assertEqual(tasks.compile_instruction(icon, sentence)['mode'], 'visual-click')

    def test_saved_recorded_instruction_text_can_express_a_result_observation(self):
        prior = recording()['steps'][0]
        old = dict(action='open', target='cell', role='button', match='automation_id', expand=False,
                   instruction=dict(mode='recorded', text='MES Setup 일치 확인'))
        source = dict(recording(), steps=[prior, old])
        plan = plans.compile_plan(source)
        self.assertEqual(plan['status'], 'compiled', plan)
        self.assertEqual(plan['envelope']['recipe']['steps'][1]['action'], 'verify-value')
        self.assertEqual(plan['envelope']['recipe']['steps'][1]['value'], 'N')

    def test_new_recording_compiles_result_check_double_click_read_and_business_close(self):
        import teach_capture as capture
        from capture_trail import finalize_contexts
        field = dict(id='field', action='set-value', field_label='Part ID', value='old',
                     target=dict(kind='element', locator=dict(role='textfield', automation_id='partInput')),
                     recognition=dict(target='identified', action='supported', result='checkable'))
        view = dict(button(), id='view')
        cell = dict(id='cell', action='click',
                    target=dict(kind='element', locator=dict(role='row', name='old',
                                ancestors=[dict(role='table', automation_id='results')])))
        close = dict(id='close', action='click', target=dict(kind='element', locator={
                    'role':'button', 'automation_id':'SystemClose', 'name':'Close',
                    'ancestors':[dict(role='titlebar', name='title')]}),
                    recognition=dict(target='identified', action='supported', result='unobserved'))
        scope = dict(exe='Example.exe', title='Details', related=True, after_id='double')
        gestures = [capture.capture_step(field), capture.capture_step(view), capture.capture_step(cell),
                    capture.manual('double_click_needs_review', 'double'),
                    dict(capture.manual('capture_target_unavailable', 'comment'), _capture_window=scope),
                    dict(capture.capture_step(close), _capture_window=scope)]
        sentences = ['입력받은 {품목} 변수를 Part ID 입력란에 입력해줘', '조회 버튼을 눌러줘', 'Part ID 일치 확인',
                     '결과 행을 더블클릭해 상세 창을 열어줘', 'Memo 값을 읽어줘', 'Exit 버튼을 눌러줘']
        edits = capture.StepEdits()
        for gesture, sentence in zip(gestures, sentences):
            edits.store(gesture, '', sentence)
        source = tasks.from_recording('test', 'Example.exe', 'Main', finalize_contexts(edits.apply(gestures)))
        plan = plans.compile_plan(source)
        self.assertEqual(plan['status'], 'compiled', plan)
        steps = plan['envelope']['recipe']['steps']
        self.assertEqual([s['action'] for s in steps], [
            'set-value', 'invoke', 'verify-value', 'double-click-result', 'read-value', 'invoke'])
        self.assertEqual(steps[2]['selector'], {'automation_id':'results', 'role':'table'})
        self.assertEqual((steps[2]['result_column'], steps[2]['parameter']), ('Part ID', '품목'))
        self.assertNotIn('old', json.dumps(steps[2:]))
        self.assertEqual(steps[4]['selector'], {'role':'textfield', 'query':'Memo'})
        self.assertEqual(steps[5]['selector'], {'role':'button', 'query':'Exit'})
        self.assertTrue(steps[5]['verify_closed'])
        self.assertEqual(steps[4]['window_context']['after_step'], 4)

    def test_observations_require_safe_subject_and_compare_only_the_specified_result(self):
        raw = dict(action='manual', reason='capture_target_unavailable')
        verify = tasks.instructed_step(raw, tasks.compile_instruction(raw, 'Batch 일치 확인'))
        self.assertEqual(verify['selector'], {'role':'table'})
        natural = tasks.compile_instruction(raw, '조회 결과 표에서 Batch 값이 입력한 값과 같은지 확인해줘')
        self.assertEqual((natural['result_column'], natural['parameter']), ('Batch', 'Batch'))
        named = tasks.compile_instruction(raw, '결과 표에서 Equip ID가 입력한 장비 ID와 같은지 확인해줘')
        self.assertEqual((named['result_column'], named['parameter']), ('Equip ID', '장비 ID'))
        for sentence in ('결과 표에서 Batch 값이 입력한 값과 맞는지 확인해줘',
                         '결과 표에서 Batch 값을 입력한 값과 비교해줘',
                         '결과 표에서 Batch가 입력값이랑 같은지 확인해줘'):
            natural = tasks.compile_instruction(raw, sentence)
            self.assertEqual((natural['result_column'], natural['parameter']), ('Batch', 'Batch'))
        stated_input = tasks.compile_instruction(raw, 'Batch 입력란이 {조회값}과 일치하는지 확인해줘')
        self.assertEqual(stated_input['selector'], {'role':'textfield', 'query':'Batch'})
        self.assertNotIn('result_column', stated_input)
        self.assertIsNone(plans._expected_binding(verify, []))
        fixed = dict(action='fill', label='Batch', instruction=dict(mode='fixed', value='B4'))
        self.assertEqual(plans._expected_binding(verify, [fixed]), {'value':'B4'})
        collision = dict(fixed, instruction=dict(mode='required', parameter='조건'))
        self.assertIsNone(plans._expected_binding(verify, [fixed, collision]))
        for sentence in ('Memo 값을 읽고 창을 닫아줘', 'Memo 값을 읽지 마', 'Memo 값을 읽어줘 그리고 조회'):
            self.assertEqual(tasks.compile_instruction(raw, sentence)['mode'], 'draft')
        protected = dict(raw, reason='protected_input_not_recorded')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_unavailable'):
            tasks.compile_instruction(protected, 'Memo 값을 읽어줘')
        detail = dict(raw, window_context=dict(exe='Example.exe', title='Detail', after_step=1, related=True))
        for sentence in ('Exit 버튼을 눌러 상세 창을 닫아줘', '확인 버튼으로 이 창을 닫아 줘'):
            observed = tasks.instructed_step(detail, tasks.compile_instruction(detail, sentence))
            self.assertEqual(observed['selector']['query'], sentence.split()[0])
            self.assertEqual(observed['action'], 'close-popup')
        saved = dict(detail, reason='popup_needs_live_binding', instruction_draft='Memo 값을 읽어줘')
        task = tasks.from_recording('read', 'Example.exe', 'Main', [button()])
        task['steps'].append(saved)
        self.assertEqual(tasks.resolve_saved_intents(task)['steps'][1]['action'], 'read-value')
        self.assertFalse(tasks.same_result_double(dict(action='manual', reason='double_click_needs_review',
                         instruction_draft='다른 결과 행을 더블클릭해줘'), verify))
        self.assertFalse(tasks.same_result_double(dict(action='manual', reason='double_click_needs_review',
                         instruction_draft='설정 아이콘을 더블클릭해줘'), verify))

    def test_result_double_click_uses_current_rows_instead_of_recording_pixels(self):
        from test_visual_target import image, visual, VisualTargets
        sentence = '나온 테이블 결과를 더블 클릭해서 팝업 창을 띄워줘'
        raw = dict(action='manual', target={'kind':'manual'}, recognition={'reason':'double_click_needs_review'})
        draft = tasks.instructed_step(raw, tasks.compile_instruction(raw, sentence))
        self.assertEqual(draft['action'], 'double-click-result')
        self.assertEqual(draft['result_row'], 'recorded')
        self.assertFalse(tasks.has_groundable_draft(draft))
        marker = visual.from_frame(VisualTargets.window, image((35,42)), (235,162))
        hinted = dict(raw, visual_hint=marker)
        step = tasks.instructed_step(hinted, tasks.compile_instruction(hinted, sentence))
        self.assertNotIn('visual', step)
        self.assertIsNone(tasks.current_result_intent(hinted, '다음 결과 행을 더블클릭해줘'))
        self.assertIsNone(tasks.current_result_intent(hinted, '결과를 더블 클릭하지 마'))
        self.assertIsNone(tasks.current_result_intent(dict(raw, protected=True), sentence))
        self.assertFalse(tasks.is_double_click_step(dict(raw, protected=True)))
        src = recording()
        src['steps'] = [dict(action='open', role='button', match='automation_id', target='view',
                             command_name='VIEW', expand=False), draft]
        envelope = plans.compile_plan(src)['envelope']
        self.assertEqual([row['action'] for row in envelope['recipe']['steps']], ['invoke','double-click-result'])
        self.assertEqual(envelope['recipe']['steps'][1]['result_row'], 'recorded')
        self.assertNotIn('visual', str(envelope['recipe']))
        self.assertEqual(envelope['notes'][0]['diagnostic'], 'open_current_result')
        first = tasks.compile_instruction(raw, '조회 결과의 첫 행을 더블클릭해줘')
        self.assertEqual(first['result_row'], 'first')
        twice = tasks.compile_instruction(raw, '결과 행을 두 번 눌러 상세 창을 열어줘')
        self.assertEqual(twice['result_row'], 'recorded')
        for caption in ('Delete', 'Apply'):
            src['steps'][0]['command_name'] = caption
            # The preceding command is recorded; current-row readiness is
            # determined by its typed observer, not by a caption allowlist.
            generic = plans.compile_plan(src)['envelope']
            self.assertEqual([s['action'] for s in generic['recipe']['steps']], ['invoke', 'double-click-result'])
            self.assertEqual(generic['notes'][0]['business_result'], 'not_verified')

    def test_run_time_values_on_unrelated_combo_and_text_field(self):
        zone = combo('배송 권역', 'deliveryZone', '배송 권역에 "서울"을 선택해줘. 매번 실행할 때 물어봐')
        level = combo('배송 등급', 'serviceLevel', '배송 등급에 "당일"을 선택해줘')
        part = dict(action='set-value', field_label='주문 번호', value='ORD-17',
                    target=dict(kind='element', locator=dict(role='textfield', automation_id='orderRef')),
                    recognition=dict(action='supported', target='identified', result='checkable'))
        part['instruction'] = tasks.compile_instruction(part, '실행할 때 값을 물어봐줘')
        task = tasks.from_recording('배송 조건', 'Dispatch.exe', '배송 관리', [zone, level, part])
        recipe = plans.compile_plan(task)['envelope']['recipe']
        self.assertEqual([step.get('parameter') for step in recipe['steps']], ['배송 권역', None, '주문 번호'])
        self.assertEqual([step.get('value') for step in recipe['steps']], [None, '당일', None])
        self.assertEqual(workflow.values_for(recipe, {}), ({}, ['배송 권역', '주문 번호']))
        overrides = {'배송 권역': '부산', '주문 번호': 'ORD-28'}
        self.assertEqual(workflow.values_for(recipe, overrides), (overrides, []))

    def test_ask_at_run_without_an_identified_field_preserves_draft(self):
        question = '실행할 때 값을 물어봐줘'
        for step in (dict(action='manual', reason='capture_target_unavailable'), button()):
            spec = tasks.compile_instruction(step, question)
            self.assertEqual(spec, dict(mode='draft', text=question))
        self.assertIsNone(tasks.instruction_subject(question))

    def test_quoted_combo_assignments_preserve_spelling_and_explicit_older_bindings(self):
        step = recording()['steps'][0]
        for literal in ("'N'", '"N"', '‘N’', '“N”'):
            sentence = f'MES SETUP조건을 {literal}으로 설정'
            self.assertEqual(tasks.compile_instruction(step, sentence), dict(mode='fixed', value='N', text=sentence))
            with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
                tasks.compile_instruction(step, sentence, mode='fixed', argument='P')
        for literal in ("''", "'N", "N’"):
            self.assertEqual(tasks.compile_instruction(step, f'MES Setup을 {literal}으로 설정')['mode'], 'draft')
        step['instruction'] = dict(mode='fixed', text="MES Setup을 'Open'으로 설정", value='O')
        self.assertEqual(tasks.compile_instruction(step, step['instruction']['text'], mode='fixed', argument='O'),
                         step['instruction'])

    def test_identified_query_and_grid_menu_compile_without_editing_recording(self):
        task = recording()
        menu = dict(action='result-menu', label='내보내기', context=dict(trigger=None, ready=None, output=None,
                    menu=dict(target=dict(role='group', automation_id='Results'), point=[.3, .4], path=['Export'])))
        task['steps'].append(menu)
        source = deepcopy(task)
        compiled = plans.compile_plan(task)['envelope']
        self.assertEqual(task, source)
        self.assertEqual(compiled['recipe']['steps'][-2], dict(action='wait', seconds=5))
        self.assertEqual(compiled['notes'], [dict(step=3, diagnostic='default_query_settle',
                                                  seconds=5, business_result='not_verified')])
        menu['context']['ready'] = dict(kind='text', target=dict(role='statictext', name='State'), expected='Done')
        compiled = plans.compile_plan(task)['envelope']
        self.assertNotIn('wait', [s['action'] for s in compiled['recipe']['steps']])
        self.assertEqual(compiled['recipe']['steps'][-1]['context']['ready'], menu['context']['ready'])
        # A different button must not inherit an implicit continuation.
        task['steps'][2].update(command_name='Delete', instruction=dict(mode='recorded', text=''))
        self.assertEqual(plans.compile_plan(task)['diagnostic'], 'execution_plan_wait_required')

    def test_query_wait_and_exact_group_menu_survive_compilation(self):
        task = recording()
        task['steps'][-1]['wait_after'] = 5
        menu = dict(action='result-menu', label='시트 내보내기', context=dict(
            trigger=None, ready=None, output=None, menu=dict(
                target=dict(role='group', automation_id='Results'), point=[.38, .54],
                path=['Export to excel', 'Active sheet'])))
        task['steps'].append(menu)
        compiled = plans.compile_plan(task)['envelope']
        actions = compiled['recipe']['steps']
        self.assertEqual([s['action'] for s in actions], ['set-value','set-value','invoke','wait','result-menu'])
        self.assertEqual((actions[0]['value'], actions[1]['parameter'], actions[-2]['seconds']), ('N', 'Family', 5))
        self.assertEqual(actions[-1], menu)
        self.assertEqual(compiled['source_steps'], [1, 2, 3, 3, 4])

    def test_fixed_and_required_text_and_stable_scope_are_preserved(self):
        source = recording()
        source['steps'][1].update(alias='제품군', description='업무에서 받은 제품군')
        untouched = deepcopy(source)
        result = plans.compile_plan(source)
        self.assertEqual(result['status'], 'compiled')
        self.assertFalse(result['input_dispatched'])
        envelope = result['envelope']
        self.assertEqual(envelope['source'], source)
        self.assertEqual(source, untouched)
        first, second, third = envelope['recipe']['steps']
        self.assertEqual(first['value'], 'N')
        self.assertNotIn('parameter', first)
        self.assertEqual(second['parameter'], 'Family')
        self.assertNotIn('value', second)
        self.assertEqual(second['label'], '제품군')
        self.assertEqual(second['field_label'], 'Family')
        self.assertEqual(second['selector']['ancestors'], source['steps'][1]['ancestors'])
        self.assertEqual(third['command_name'], '조회')
        self.assertEqual(third['action'], 'invoke')
        self.assertEqual(envelope['source_steps'], [1, 2, 3])
        self.assertEqual(envelope['source']['steps'][1]['instruction']['text'],
                         '입력받은 변수를 Family 조건에 입력해줘')
        source['steps'][0]['instruction']['value'] = 'P'
        self.assertEqual(envelope['source']['steps'][0]['instruction']['value'], 'N')

    def test_missing_required_value_stops_before_ui_or_input(self):
        recipe = plans.compile_plan(recording())['envelope']['recipe']
        with patch.object(workflow, 'bind', side_effect=AssertionError('No UI access')):
            result = workflow.run(recipe)
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['missing_parameters'], ['Family'])
        self.assertFalse(result['input_dispatched'])
        self.assertEqual(result['steps'], [])

    def test_legacy_demonstrated_value_is_never_silently_fixed(self):
        task = recording()
        step = task['steps'][1]
        step.pop('instruction')
        step.update(parameter='Family', value='old-demo', recorded_effect='matched')
        result = plans.compile_plan(task)
        compiled = result['envelope']['recipe']['steps'][1]
        self.assertEqual(compiled['parameter'], 'Family')
        self.assertNotIn('value', compiled)
        self.assertEqual(result['envelope']['notes'], [dict(step=2,
            diagnostic='legacy_parameter_requires_input', parameter='Family')])

    def test_unsupported_steps_never_yield_partial_plan(self):
        manual = dict(action='manual', reason='target_missing')
        expanded = dict(action='open', role='menuitem', target='export', match='automation_id', expand=True)
        choice = dict(action='open', role='listitem', target='selection', match='automation_id', expand=False)
        for unsupported in (manual, expanded, choice):
            with self.subTest(unsupported=unsupported):
                task = recording()
                task['steps'].append(unsupported)
                result = plans.compile_plan(task)
                self.assertEqual(result['status'], 'not_compatible')
                self.assertEqual(result['step'], 4)
                self.assertNotIn('envelope', result)

    def test_existing_valid_limits_use_original_player(self):
        task = recording()
        task['steps'] = [deepcopy(task['steps'][2]) for _ in range(101)]
        self.assertEqual(plans.compile_plan(task)['diagnostic'], 'execution_plan_step_limit')
        task = recording()
        task['steps'][1]['instruction'] = dict(mode='required', text='', parameter='F' * 150)
        result = plans.compile_plan(task)
        self.assertEqual(result['status'], 'not_compatible')
        self.assertEqual(result['diagnostic'], 'execution_plan_contract_mismatch')

    def test_bad_source_binding_fails_instead_of_guessing(self):
        task = recording()
        task['steps'][0]['instruction']['value'] = 'P'
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
            plans.compile_plan(task)

    def test_nonterminal_invoke_without_explicit_wait_keeps_original_player(self):
        task = recording()
        task['steps'].append(deepcopy(task['steps'][2]))
        result = plans.compile_plan(task)
        self.assertEqual((result['status'], result['diagnostic'], result['step']),
                         ('not_compatible', 'execution_plan_wait_required', 3))
        self.assertNotIn('envelope', result)

    def test_explicit_wait_becomes_separate_step_without_inventing_result_verification(self):
        task = recording()
        task['steps'][2]['wait_after'] = 5
        final = deepcopy(task['steps'][2])
        final.pop('wait_after')
        final.update(target='export', command_name='Excel', label='Excel')
        final['instruction'] = dict(mode='recorded', text='')
        task['steps'].append(final)
        result = plans.compile_plan(task)
        self.assertEqual(result['status'], 'compiled')
        steps = result['envelope']['recipe']['steps']
        self.assertEqual([step['action'] for step in steps], ['set-value', 'set-value', 'invoke', 'wait', 'invoke'])
        self.assertEqual(steps[3], dict(action='wait', seconds=5))
        self.assertEqual(result['envelope']['source_steps'], [1, 2, 3, 3, 4])
        self.assertEqual(result['envelope']['source']['steps'][2]['wait_after'], 5)
        self.assertFalse(any('verified' in step for step in steps))

    def test_wait_receipt_maps_to_its_original_query_step(self):
        task = recording()
        task['steps'].pop(1)
        task['steps'][1]['wait_after'] = 5
        final = deepcopy(task['steps'][1])
        final.pop('wait_after')
        final.update(target='export', command_name='Excel', label='Excel')
        final['instruction'] = dict(mode='recorded', text='')
        task['steps'].append(final)
        envelope = plans.compile_plan(task)['envelope']
        self.assertEqual(envelope['source_steps'], [1, 2, 2, 3])
        self.assertEqual(len(envelope['source_steps']), len(envelope['recipe']['steps']))

    def test_compiled_step_limit_includes_inserted_waits(self):
        task = recording()
        command = deepcopy(task['steps'][2])
        command['wait_after'] = 1
        task['steps'] = [deepcopy(command) for _ in range(51)]
        result = plans.compile_plan(task)
        self.assertEqual((result['status'], result['diagnostic']),
                         ('not_compatible', 'execution_plan_step_limit'))
        self.assertNotIn('envelope', result)

    def test_canonical_fingerprint_ignores_dict_order_not_instruction_or_step_order(self):
        original = recording()
        reordered = dict(reversed(list(original.items())))
        self.assertEqual(plans.fingerprint(original), plans.fingerprint(reordered))
        changed = deepcopy(original)
        changed['steps'][0]['instruction'] = dict(mode='fixed', text='MES Setup을 P로 설정', value='P')
        self.assertNotEqual(plans.fingerprint(original), plans.fingerprint(changed))
        changed = deepcopy(original)
        changed['steps'].reverse()
        self.assertNotEqual(plans.fingerprint(original), plans.fingerprint(changed))


class StorageTests(unittest.TestCase):
    def test_missing_or_stale_plan_compiles_once_per_get(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            for status in ('saved', 'loaded', 'saved'):
                with patch.object(plans, 'compile_saved', wraps=plans.compile_saved) as compile_saved:
                    result = plans.get_plan(task, folder)
                    self.assertEqual(result['status'], status)
                    compile_saved.assert_called_once_with(task, folder)
                if status == 'loaded':
                    Path(result['path']).write_text('{}', encoding='utf8')

    def test_atomic_sidecar_roundtrip_does_not_replace_or_list_as_original_recording(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            original = tasks.save_task(task, folder)
            before = original.read_bytes()
            result = plans.save_plan(task, folder)
            self.assertEqual(result['status'], 'saved')
            path = Path(result['path'])
            self.assertEqual(path.parent.name, 'execution-plans')
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(tasks.list_tasks(folder), [task['name']])
            loaded = plans.load_plan(task, folder)
            self.assertEqual(loaded['status'], 'loaded')
            self.assertEqual(result['envelope'], loaded['envelope'])
            self.assertFalse(list(path.parent.glob('.plan-*')))

    def test_source_change_never_returns_old_recipe_and_get_plan_rebuilds(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            first = plans.save_plan(task, folder)
            task['steps'][0]['instruction'] = dict(mode='fixed', text='MES Setup을 P로 설정', value='P')
            stale = plans.load_plan(task, folder)
            self.assertEqual(stale['status'], 'stale')
            self.assertNotIn('envelope', stale)
            fresh = plans.get_plan(task, folder)
            self.assertEqual(fresh['status'], 'saved')
            self.assertNotEqual(first['envelope']['source_fingerprint'], fresh['envelope']['source_fingerprint'])
            self.assertEqual(fresh['envelope']['recipe']['steps'][0]['value'], 'P')

    def test_modified_recipe_and_compiler_version_are_rejected(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            saved = plans.save_plan(task, folder)
            path = Path(saved['path'])
            for modification, status in (('recipe', 'invalid'), ('compiler', 'stale'), ('bool-schema', 'stale')):
                payload = deepcopy(saved['envelope'])
                if modification == 'recipe':
                    payload['recipe']['steps'][0]['value'] = 'P'
                elif modification == 'compiler':
                    payload['compiler'] += 1
                else:
                    payload['schema'] = True
                path.write_text(json.dumps(payload), encoding='utf8')
                observed = plans.load_plan(task, folder)
                self.assertEqual(observed['status'], status)
                self.assertNotIn('envelope', observed)

    def test_invalid_json_is_rebuilt_only_from_current_source(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(plans.load_plan(task, folder)['status'], 'missing')
            saved = plans.save_plan(task, folder)
            Path(saved['path']).write_text('{"schema":1,"schema":2}', encoding='utf8')
            self.assertEqual(plans.load_plan(task, folder)['status'], 'invalid')
            repaired = plans.get_plan(task, folder)
            self.assertEqual(repaired['status'], 'saved')
            self.assertEqual(repaired['envelope']['recipe']['steps'][0]['value'], 'N')

    def test_saved_plan_without_source_mapping_is_not_reused(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            saved = plans.save_plan(task, folder)
            payload = deepcopy(saved['envelope'])
            payload.pop('source_steps')
            Path(saved['path']).write_text(json.dumps(payload), encoding='utf8')
            self.assertEqual(plans.load_plan(task, folder)['status'], 'invalid')
            repaired = plans.get_plan(task, folder)
            self.assertEqual(repaired['status'], 'saved')
            self.assertEqual(repaired['envelope']['source_steps'], [1, 2, 3])

    def test_failed_publication_preserves_source_and_previous_plan(self):
        task = recording()
        with tempfile.TemporaryDirectory() as folder:
            source = tasks.save_task(task, folder)
            source_bytes = source.read_bytes()
            saved = plans.save_plan(task, folder)
            destination = Path(saved['path'])
            previous_bytes = destination.read_bytes()
            with patch.object(plans.os, 'replace', side_effect=PermissionError('denied')):
                result = plans.save_plan(task, folder)
            self.assertEqual(result['status'], 'plan_unavailable')
            self.assertNotIn('envelope', result)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(destination.read_bytes(), previous_bytes)
            self.assertFalse(list(destination.parent.glob('.plan-*')))


if __name__ == '__main__':
    unittest.main()
