"""Demonstration values, bound instructions, partial actions and local storage."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import learned_tasks as tasks


def demo(label='MES Setup', value='W', role='combobox', action='set-value'):
    return {'action': action, 'field_label': label, 'value': value,
            'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'},
            'target': {'kind': 'element', 'locator': {'automation_id': label.replace(' ', '') + 'ID',
                       'name': '', 'role': role, 'ancestors': [{'role': 'group', 'automation_id': 'conditions'}]}}}


def task(*steps):
    return tasks.from_recording('설비 조건', 'Agent.exe', 'Device Setup', list(steps))


def compiled(raw, sentence, **kwargs):
    return dict(raw, instruction=tasks.compile_instruction(raw, sentence, **kwargs))


def matched(step):
    return {'status': 'verified', 'input_dispatched': True, 'effect_verification': 'matched'}


class PlanTests(unittest.TestCase):
    def run_task(self, plan, overrides=None, invoke=matched, **kwargs):
        return tasks.replay(plan, 'agent.exe', 'Device Setup', overrides or {}, invoke, **kwargs)

    def test_conversion_disallows_coordinates_and_transient_foreign_windows(self):
        a = demo()
        a['target'].update(kind='region', recorded_bounds={'x': 4100}, point={'x_ratio': .55})
        b = demo()
        b['target']['window'] = {'hwnd': 123, 'title': 'private-popup'}
        p = task(a, b)
        self.assertTrue(all(s['action'] == 'manual' for s in p['steps']))
        self.assertNotIn('4100', json.dumps(p))
        self.assertNotIn('private-popup', json.dumps(p))
        self.assertEqual(self.run_task(p, invoke=lambda _: self.fail())['status'], 'needs_review')

    def test_typed_different_control_cannot_match_disappeared_named_target(self):
        s = task(demo())['steps'][0]
        def node(path, parent, role, key, **kw):
            return dict(path=path, parent_path=parent, role=role, automation_id=key,
                        enabled=True, secure=False, **kw)
        nodes = [node('0', '', 'window', None), node('0/0', '0', 'group', 'conditions'),
                 node('0/1', '0', 'group', 'other'), node('0/0/0', '0/0', 'combobox', s['target']),
                 node('0/1/0', '0/1', 'combobox', s['target'])]
        self.assertEqual(tasks.locate_target(nodes, s), (3, 'automation_id'))
        nodes[3]['name'], nodes[3]['automation_id'] = s['target'], 'replacement'
        with self.assertRaisesRegex(tasks.TaskError, 'target_missing'):
            tasks.locate_target(nodes, s)

    def test_command_with_reused_id_and_new_caption_cannot_run(self):
        raw = demo('Command', '', 'button', 'click')
        raw['target']['locator']['name'] = 'Run query'
        step = task(raw)['steps'][0]
        self.assertEqual(step['command_name'], 'Run query')
        tasks.check_display(step, {'name': 'run   query'})
        with self.assertRaisesRegex(tasks.TaskError, 'recorded_label_changed'):
            tasks.check_display(step, {'name': 'Cancel operation'})

    def test_manual_preflight_stops_before_earlier_actions_but_exact_alias_can_run(self):
        a = dict(demo(), alias='MES 상태', description='현재 화면의 조건')
        p = task(a, {'action': 'manual', 'recognition': {'reason': 'popup_scope_unproven'}})
        result = self.run_task(p, invoke=lambda _: self.fail())
        self.assertEqual((result['status'], result['stopped_at'], result['input_dispatched']), ('needs_review', 2, False))
        calls = []
        result = self.run_task(p, {'MES 상태': 'N'}, lambda s: calls.append(s) or matched(s), only='MES 상태')
        self.assertEqual((result['status'], [s['value'] for s in calls]), ('verified', ['N']))
        self.assertEqual(p['steps'][0]['value'], 'W')

    def test_exact_aliases_and_no_interpretation_of_free_description(self):
        p = task(dict(demo(), alias='이름', description='Family 값을 D로 변경해줘'),
                 dict(demo('Family'), alias='이름'))
        for ref, reason in [('이', 'unknown_step_alias'), ('이름', 'ambiguous_step_alias')]:
            with self.assertRaisesRegex(tasks.TaskError, reason):
                tasks.get_step(p, ref)
        self.assertEqual(tasks.get_step(p, '2')['value'], 'W')
        with self.assertRaisesRegex(tasks.TaskError, 'ambiguous_parameter'):
            self.run_task(p, {'이름': 'N'}, lambda _: self.fail())
        with self.assertRaisesRegex(tasks.TaskError, 'recorded_window_mismatch'):
            tasks.replay(p, 'Agent.exe', 'Another form', {}, lambda _: self.fail())

    def test_uncertain_button_delivery_stops_no_replay(self):
        p = task(demo(), demo('조회', '', 'button', 'click'), demo('Family'))
        delivered = []
        def invoke(s):
            delivered.append(s['action'])
            return matched(s) if s['action'] == 'select' else {'status': 'delivered_unverified', 'input_dispatched': True}
        result = self.run_task(p, invoke=invoke)
        self.assertEqual((result['stopped_at'], delivered, result['input_dispatched']), (2, ['select', 'open'], True))
        for receipt in ({'status': 'verified', 'input_dispatched': False}, {'status': 'uncertain', 'input_dispatched': None}):
            result = self.run_task(p, invoke=lambda _: receipt)
            self.assertEqual((result['status'], result['stopped_at'], result['input_dispatched']), ('uncertain', 1, None))

    def test_explicit_instructions_use_fixed_and_required_values_preflight(self):
        a = compiled(demo(), 'MES SETUP조건을 N으로 해줘')
        b = compiled(demo('Family', 'secret-demo'), '입력받은 변수를 FAMILY조건에 입력해줘')
        c = compiled(dict(demo('Group'), alias='조'), '이 항목을 {조코드}로 설정')
        p = task(a, b, c)
        self.assertNotIn('secret-demo', json.dumps(p))
        result = self.run_task(p, invoke=lambda _: self.fail())
        self.assertEqual((result['status'], result['input_dispatched'], result['missing_parameters']),
                         ('needs_input', False, ['Family', '조코드']))
        actual = []
        result = self.run_task(p, {'FAMILY': 'Hybrid', '조코드': 'C'}, lambda s: actual.append(s['value']) or matched(s))
        self.assertEqual((actual, result['status']), (['N', 'Hybrid', 'C'], 'verified'))

    def test_fixed_alias_and_unparseable_text_do_not_change_value(self):
        a = dict(demo(), alias='MES')
        p = task(compiled(a, '이 항목을 N으로 설정'))
        with self.assertRaisesRegex(tasks.TaskError, 'fixed_value_override'):
            self.run_task(p, {'MES': 'Y'}, lambda _: self.fail())
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_requires_structure'):
            tasks.compile_instruction(a, '전에 쓰던 값으로 알아서')
        self.assertEqual(tasks.compile_instruction(a, '전에 쓰던 값으로 알아서', mode='fixed', argument='N')['value'], 'N')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_mismatch'):
            tasks.compile_instruction(a, 'Family를 N으로 설정', mode='fixed', argument='N')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
            tasks.compile_instruction(a, 'MES를 N으로 설정', mode='fixed', argument='Y')

    def test_explicitly_taught_location_can_only_be_activated_with_bound_instruction(self):
        raw = demo()
        raw.pop('value')
        raw.update(action='teach-target', recognition={'target': 'identified', 'action': 'requires_instruction',
                                                      'result': 'manual', 'reason': 'combo_choice_unproven'})
        self.assertEqual(task(raw)['steps'][0]['action'], 'manual')
        p = task(compiled(raw, '이 항목을 N으로 설정'))
        self.assertEqual(self.run_task(p)['status'], 'verified')
        for field in ('action', 'target', 'result', 'reason'):
            unsafe = dict(raw, recognition={**raw['recognition'], field: 'unrecognized'})
            with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_unavailable'):
                tasks.compile_instruction(unsafe, '이 항목을 N으로 설정')
        mismatch = dict(demo(), selection={'source': 'option_click', 'result': 'mismatch'})
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_unavailable'):
            tasks.compile_instruction(mismatch, '이 항목을 N으로 설정')

    def test_saved_bindings_are_revalidated(self):
        p = task(compiled(demo(), 'MES Setup을 N으로 선택'))
        p['steps'][0]['instruction']['value'] = 'P'
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
            self.run_task(p, invoke=lambda _: self.fail())
        p = task(compiled(demo('Family'), 'Family를 {제품군}로 선택'))
        p['steps'][0]['value'] = 'obsolete fallback'
        with self.assertRaisesRegex(tasks.TaskError, 'unexpected_task_step_data'):
            self.run_task(p, invoke=lambda _: self.fail())

    def test_unlabeled_combo_can_be_explicitly_linked_without_retargeting_it(self):
        raw = demo()
        raw.pop('field_label')
        raw['target']['locator']['automation_id'] = 'gcboStatus'
        sentence = 'MES SETUP조건을 N으로 해줘'
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_mismatch'):
            tasks.compile_instruction(raw, sentence, mode='fixed', argument='N')
        alias = tasks.bind_instruction_subject(raw, [], sentence, mode='fixed', argument='N')
        self.assertEqual(alias, 'MES SETUP')
        linked = dict(raw, alias=alias)
        plan = task(compiled(linked, sentence, mode='fixed', argument='N'))
        with tempfile.TemporaryDirectory() as directory:
            tasks.save_task(plan, directory)
            saved = tasks.load_task(plan['name'], directory)
        self.assertEqual(saved['steps'][0]['target'], 'gcboStatus')
        self.assertEqual(saved['steps'][0]['alias'], 'MES SETUP')
        self.assertEqual(saved['steps'][0]['instruction']['value'], 'N')
        self.assertEqual(self.run_task(saved)['status'], 'verified')

    def test_explicit_field_link_does_not_override_another_known_target(self):
        raw = demo()
        raw.pop('field_label')
        raw['target']['locator']['automation_id'] = 'gcboStatus'
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_mismatch'):
            tasks.bind_instruction_subject(raw, [demo('Family')], 'FAMILY조건을 N으로 해줘')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_target_mismatch'):
            tasks.bind_instruction_subject(demo('MES Setup'), [], 'Family조건을 N으로 해줘')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_binding_conflict'):
            tasks.bind_instruction_subject(raw, [], 'MES SETUP조건을 N으로 해줘', mode='fixed', argument='P')

    def test_label_case_and_spacing_allow_fixed_and_required_sentences(self):
        raw = demo('MES   Setup')
        raw['target']['locator']['automation_id'] = 'gcboStatus'
        self.assertEqual(tasks.compile_instruction(raw, 'mes setup 조건을 N으로 해줘')['value'], 'N')
        family = demo('FAMILY')
        family['target']['locator']['automation_id'] = 'gcboFamily'
        spec = tasks.compile_instruction(family, '입력받은 변수를 family 조건에 입력해줘',
                                         mode='required', argument='Family')
        self.assertEqual(spec['parameter'], 'Family')

    def test_button_instructions_must_use_recorded_capability(self):
        button = demo('조회', '', 'button', 'click')
        plan = task(compiled(button, '조회 버튼을 눌러줘'))
        self.assertEqual(plan['steps'][0]['instruction']['mode'], 'recorded')
        with self.assertRaisesRegex(tasks.TaskError, 'instruction_action_mismatch'):
            tasks.compile_instruction(button, '조회 버튼을 펼쳐줘')

    def test_variables_may_be_shared_if_explicitly_requested(self):
        shared = '이 항목을 {공통값}로 설정'
        p = task(compiled(demo('Family'), shared), compiled(demo('Category'), shared))
        actual = []
        self.run_task(p, {'공통값': 'A'}, lambda s: actual.append(s['value']) or matched(s))
        self.assertEqual(actual, ['A', 'A'])
        with self.assertRaisesRegex(tasks.TaskError, 'duplicate_override'):
            tasks.parse_overrides(['x=A', 'X=B'])

    def test_atomic_save_no_overwrite_and_strict_local_json(self):
        p = task(demo())
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'tasks'
            self.assertEqual(tasks.list_tasks(directory), [])
            self.assertFalse(directory.exists())
            path = tasks.save_task(p, directory)
            with self.assertRaisesRegex(tasks.TaskError, 'task_already_exists'):
                tasks.save_task(p, directory)
            self.assertEqual(tasks.load_task(p['name'], directory), p)
            self.assertEqual(tasks.list_tasks(directory), [p['name']])
            path.write_text('{"steps":[],"steps":[]}', encoding='utf-8')
            with self.assertRaisesRegex(tasks.TaskError, 'duplicate_task_key'):
                tasks.load_task(p['name'], directory)
            for name in ('../foo', 'dir/file', 'CON', 'foo:', 'bad '):
                with self.assertRaisesRegex(tasks.TaskError, 'invalid_task_name'):
                    tasks.check_new_task(name, directory)
