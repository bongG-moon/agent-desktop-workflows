"""Integration contracts for free descriptions, scoped execution and delivery stops."""
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select as app
import intent_replay as replay
import intent_tasks as intent
import learned_tasks as tasks


def verified(_=None):
    return {'status': 'verified', 'input_dispatched': True, 'effect_verification': 'matched'}


def node(path, role, identity, name=''):
    return dict(path=path, parent_path=path.rpartition('/')[0], role=role, automation_id=identity,
                name=name, secure=False, enabled=True)


class ReplayContractTests(unittest.TestCase):
    def step(self, text='근처에서 조건을 찾은 다음 요청한 작업을 처리해 줘', **kw):
        return intent.make_step({'anchor': {'role': 'combobox', 'automation_id': 'Family',
                               'ancestors': [{'role': 'group', 'automation_id': 'Filters'}]}}, text, **kw)

    def plan(self, *steps):
        return dict(schema=1, name='Saved instructions', exe='Example.exe', window='Business', steps=list(steps))

    def resolution(self, plan, mapping):
        return dict(schema=1, plan_digest=intent.plan_digest(plan),
                    steps=[dict(number=n, actions=actions) for n, actions in mapping.items()])

    def run_plan(self, plan, invoke=verified, values=None, resolution=None, only=None):
        return tasks.replay(plan, 'Example.exe', 'Business', values or {}, invoke,
                            resolution=resolution, only=only)

    def legacy(self, *, mode='required', alias='Old family', label='Family'):
        raw = {'action': 'set-value', 'field_label': label, 'value': 'Old', 'alias': alias,
               'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'},
               'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': label}}}
        text = f'입력받은 변수를 {label}에 입력해줘' if mode == 'required' else f'{label}를 N으로 설정해줘'
        raw['instruction'] = tasks.compile_instruction(raw, text)
        return tasks.recorded_step(raw)

    def test_free_sentence_without_parser_grammar_requests_interpretation_before_input(self):
        text = '화면에서 지난번에 가리킨 곳을 참고해서 조건을 적절히 정리해요.\n그 다음 조회해 주세요!'
        plan = self.plan(self.step(text))
        calls = []
        report = self.run_plan(plan, lambda s: calls.append(s) or verified())
        self.assertEqual(report['status'], 'needs_interpretation')
        self.assertEqual(report['task']['steps'][0]['instruction']['text'], text)
        self.assertEqual(report['plan_digest'], intent.plan_digest(plan))
        self.assertFalse(calls)

    def test_missing_anchor_is_saved_and_returns_repair_request_without_input(self):
        plan = self.plan(intent.make_step(None, '이 근처 상태를 바꿔줘'))
        report = self.run_plan(plan, lambda _: self.fail('must not dispatch'))
        self.assertEqual((report['status'], report['stopped_at']), ('needs_target', 1))
        self.assertEqual(report['instruction'], '이 근처 상태를 바꿔줘')

    def test_blank_draft_stops_before_any_action_even_with_typed_resolution(self):
        for text in ('', ' \n\t '):
            plan = self.plan(self.step('첫 번째 동작'), self.step(text))
            resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')],
                                                2: [dict(action='select', target='Family', value='Hybrid')]})
            calls = []
            with self.subTest(text=text):
                report = self.run_plan(plan, lambda s: calls.append(s) or verified(), resolution=resolution)
                self.assertEqual((report['status'], report['stopped_at'], report['input_dispatched']),
                                 ('needs_instruction', 2, False))
                self.assertEqual(app.result_code(report), 2)
                self.assertFalse(calls)
                report = self.run_plan(plan, lambda s: calls.append(s) or verified(), resolution=resolution, only='1')
                self.assertEqual((report['status'], len(calls)), ('verified', 1))

    def test_model_inferred_runtime_parameter_preflight_and_original_are_preserved(self):
        plan = self.plan(self.step('제품 종류를 사용자에게 물어보고 조건에 반영해줘'))
        original = deepcopy(plan)
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', parameter='Product')]})
        original_resolution = deepcopy(resolution)
        calls = []
        report = self.run_plan(plan, lambda s: calls.append(s) or verified(), resolution=resolution)
        self.assertEqual((report['status'], report['missing_parameters']), ('needs_input', ['Product']))
        self.assertFalse(calls)
        report = self.run_plan(plan, lambda s: calls.append(s) or verified(), {'product': 'Hybrid'}, resolution)
        self.assertEqual(report['status'], 'verified')
        self.assertEqual(calls[0]['operation']['value'], 'Hybrid')
        self.assertNotIn('parameter', calls[0]['operation'])
        self.assertEqual(plan, original)
        self.assertEqual(resolution, original_resolution)

    def test_invalid_later_action_is_rejected_before_any_earlier_operation(self):
        plan = self.plan(self.step(), self.step())
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')],
                                            2: [dict(action='open', role='button', target='Query', shell='anything')]})
        calls = []
        with self.assertRaisesRegex(intent.IntentError, 'unexpected_resolved_action_data'):
            self.run_plan(plan, lambda s: calls.append(s) or verified(), resolution=resolution)
        self.assertFalse(calls)

    def test_explicit_parameter_must_not_be_literalized(self):
        plan = self.plan(self.step('입력받은 변수를 FAMILY조건에 입력해줘'))
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='Hybrid')]})
        with self.assertRaisesRegex(intent.IntentError, 'resolution_parameter_not_used'):
            self.run_plan(plan, lambda _: self.fail('must not dispatch'), resolution=resolution)

    def test_only_limits_required_inputs_but_resolution_still_binds_whole_plan(self):
        plan = self.plan(self.step('{First}로 입력해줘'), self.step('{Second}로 입력해줘'))
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', parameter='First')],
                                            2: [dict(action='select', target='Family', parameter='Second')]})
        calls = []
        report = self.run_plan(plan, lambda s: calls.append(s) or verified(), {'First': 'Hybrid'}, resolution, only='1')
        self.assertEqual(report['status'], 'verified')
        self.assertEqual(len(calls), 1)
        plan['steps'][1]['instruction']['text'] += ' now'
        with self.assertRaisesRegex(intent.IntentError, 'resolution_plan_changed'):
            self.run_plan(plan, values={'First': 'Hybrid'}, resolution=resolution, only='1')

    def test_delivery_opt_in_continues_known_invoke_without_claiming_verified(self):
        plan = self.plan(self.step())
        resolution = self.resolution(plan, {1: [dict(action='open', role='button', target='Query', completion='delivery'),
                                            dict(action='select', target='Family', value='Hybrid')]})
        calls = []
        def invoke(step):
            calls.append(step)
            if len(calls) == 1:
                return dict(status='delivered_unverified', input_dispatched=True, method='Invoke',
                            diagnostic='business_result_not_verified')
            return verified()
        result = self.run_plan(plan, invoke, resolution=resolution)
        self.assertEqual((result['status'], len(calls)), ('completed_unverified', 2))
        self.assertEqual(result['business_commit'], 'not_verified')

    def test_default_completion_does_not_continue_unverified_invoke(self):
        plan = self.plan(self.step())
        resolution = self.resolution(plan, {1: [dict(action='open', role='button', target='Query'),
                                            dict(action='select', target='Family', value='Hybrid')]})
        calls = []
        def invoke(step):
            calls.append(step)
            return dict(status='delivered_unverified', input_dispatched=True, method='Invoke',
                        diagnostic='business_result_not_verified')
        result = self.run_plan(plan, invoke, resolution=resolution)
        self.assertEqual((result['status'], len(calls)), ('delivered_unverified', 1))

    def test_uncertain_or_unknown_delivery_is_never_retried_or_continued(self):
        plan = self.plan(self.step())
        resolution = self.resolution(plan, {1: [dict(action='open', role='button', target='Query', completion='delivery'),
                                            dict(action='select', target='Family', value='Hybrid')]})
        for status, sent, diagnostic in (('uncertain', None, 'pattern_delivery_unknown'),
                                          ('delivered_unverified', True, 'current_state_unavailable'),
                                          ('blocked', False, 'target_changed')):
            calls = []
            def invoke(step):
                calls.append(step)
                return dict(status=status, input_dispatched=sent, method='Invoke', diagnostic=diagnostic)
            with self.subTest(status=status):
                result = self.run_plan(plan, invoke, resolution=resolution)
                self.assertEqual(result['status'], status)
                self.assertEqual(len(calls), 1)

    def test_malformed_verified_receipt_cannot_authorize_next_operation(self):
        plan = self.plan(self.step())
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N'),
                                            dict(action='select', target='Family', value='Hybrid')]})
        for sent in ('missing', None, 1):
            calls = []
            def invoke(step):
                calls.append(step)
                result = dict(status='verified', effect_verification='matched')
                if sent != 'missing':
                    result['input_dispatched'] = sent
                return result
            with self.subTest(sent=sent):
                result = self.run_plan(plan, invoke, resolution=resolution)
                self.assertEqual(result['status'], 'uncertain')
                self.assertEqual(len(calls), 1)

    def test_mixed_legacy_alias_remains_a_valid_required_parameter(self):
        plan = self.plan(self.step(), self.legacy())
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')]})
        calls = []
        report = self.run_plan(plan, lambda s: calls.append(s) or verified(), {'Old family': 'Hybrid'}, resolution)
        self.assertEqual((report['status'], len(calls)), ('verified', 2))
        self.assertEqual(calls[1]['value'], 'Hybrid')

    def test_mixed_duplicate_legacy_alias_cannot_change_two_different_fields(self):
        plan = self.plan(self.step(), self.legacy(alias='Shared'), self.legacy(alias='Shared', label='Status'))
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')]})
        calls = []
        with self.assertRaisesRegex(tasks.TaskError, 'ambiguous_parameter'):
            self.run_plan(plan, lambda s: calls.append(s) or verified(), {'Shared': 'Hybrid'}, resolution)
        self.assertFalse(calls)
        # Choosing one of those steps explicitly makes its alias unambiguous.
        result = self.run_plan(plan, lambda s: calls.append(s) or verified(), {'Shared': 'Hybrid'}, only='2')
        self.assertEqual((result['status'], len(calls)), ('verified', 1))

    def test_mixed_shared_explicit_parameter_can_still_supply_multiple_legacy_steps(self):
        plan = self.plan(self.step(), self.legacy(alias='First'), self.legacy(alias='Second'))
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')]})
        calls = []
        result = self.run_plan(plan, lambda s: calls.append(s) or verified(), {'Family': 'Hybrid'}, resolution)
        self.assertEqual((result['status'], len(calls)), ('verified', 3))
        self.assertEqual([s['value'] for s in calls[1:]], ['Hybrid', 'Hybrid'])

    def test_resolution_file_oserrors_return_structured_block_without_window_access(self):
        plan = self.plan(self.step())
        args = SimpleNamespace(command='replay', task=plan['name'], set=[], exe='Example.exe', window='Business',
                               hwnd=None, only=None, resolution='missing-resolution.json')
        for failure in (FileNotFoundError('gone'), PermissionError('unreadable')):
            output = io.StringIO()
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(app.bridge, 'set_dpi_awareness'), \
                    patch.object(app.tasks, 'load_task', return_value=plan), \
                    patch.object(Path, 'open', side_effect=failure), \
                    patch.object(app, 'windows') as windows, patch.object(sys, 'stdout', output):
                status = app.worker(args)
            report = json.loads(output.getvalue())
            self.assertEqual((status, report['status'], report['diagnostic'], report['input_dispatched']),
                             (2, 'blocked', 'resolution_file_unavailable', False))
            windows.assert_not_called()

    def test_mixed_legacy_manual_preflight_stops_before_earlier_intent(self):
        plan = self.plan(self.step(), {'action': 'manual', 'reason': 'target_unavailable'})
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')]})
        calls = []
        report = self.run_plan(plan, lambda s: calls.append(s) or verified(), resolution=resolution)
        self.assertEqual((report['status'], report['stopped_at']), ('needs_review', 2))
        self.assertFalse(calls)

    def test_mixed_fixed_override_rejected_before_earlier_intent(self):
        plan = self.plan(self.step(), self.legacy(mode='fixed'))
        resolution = self.resolution(plan, {1: [dict(action='select', target='Family', value='N')]})
        calls = []
        with self.assertRaisesRegex(tasks.TaskError, 'fixed_value_override'):
            self.run_plan(plan, lambda s: calls.append(s) or verified(), {'Old family': 'Hybrid'}, resolution)
        self.assertFalse(calls)

    def test_legacy_only_selection_does_not_require_other_intents_to_resolve(self):
        plan = self.plan(self.step('{New}로 넣어줘'), self.legacy())
        calls = []
        result = self.run_plan(plan, lambda s: calls.append(s) or verified(), {'Family': 'Hybrid'}, only='2')
        self.assertEqual((result['status'], len(calls)), ('verified', 1))
        self.assertNotEqual(calls[0]['action'], 'intent-execute')


class ScopedIntentContractTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [node('0', 'window', 'Business'), node('0/0', 'group', 'Filters'),
                      node('0/0/0', 'combobox', 'Family'), node('0/0/1', 'button', 'LocalQuery', 'Query'),
                      node('0/1', 'group', 'OtherFilters'), node('0/1/0', 'button', 'OtherQuery', 'Query')]
        self.step = intent.make_step({'role': 'combobox', 'automation_id': 'Family',
                                     'ancestors': [{'role': 'group', 'automation_id': 'Filters'}]},
                                    '가리킨 조건 옆의 조회를 눌러줘')

    def test_candidate_scope_excludes_same_named_other_group_button(self):
        scoped, original, anchor = replay.scope(self.nodes, self.step)
        index, _ = app.resolve(scoped, 'Query', role='button')
        self.assertEqual(self.nodes[original[index]]['automation_id'], 'LocalQuery')
        self.assertEqual(anchor, 2)
        self.assertNotIn('OtherQuery', [n['automation_id'] for n in scoped])

    def test_missing_or_ambiguous_anchor_never_broadens_to_window(self):
        for nodes, diagnostic in (([n for n in self.nodes if n['automation_id'] != 'Family'], 'intent_anchor_missing'),
                                  (self.nodes + [node('0/0/2', 'combobox', 'Family')], 'intent_anchor_ambiguous')):
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(intent.IntentError, diagnostic):
                replay.scope(nodes, self.step)

    def test_real_action_resolves_only_scoped_target_and_rechecks_anchor_identity(self):
        operation = replay.validate_action(dict(action='open', role='button', match='name', target='Query'))
        args = SimpleNamespace(command='open', role='button', target='Query', window='Business', popup=None,
                               within=None, expand=False, value=None)
        elements = [object() for _ in self.nodes]
        for replace_anchor in (False, True):
            uia = MagicMock()
            uia.__enter__.return_value = uia
            uia.same.side_effect = lambda a, b: a is b
            fresh = list(elements)
            if replace_anchor:
                fresh[2] = object()
            calls = []
            def execute(binding, actual_uia, target, *_, still_valid):
                still_valid()
                calls.append(target)
                return verified()
            with self.subTest(replace_anchor=replace_anchor), patch.object(app, 'guard'), \
                    patch.object(app, 'restore_for_action', return_value=False), \
                    patch.object(app.bridge, 'UIA', return_value=uia), \
                    patch.object(app, 'observe', side_effect=[(self.nodes, elements), (self.nodes, fresh)]), \
                    patch.object(app, 'add_display_properties'), patch.object(app, 'execute', side_effect=execute):
                if replace_anchor:
                    with self.assertRaisesRegex(app.SelectionError, 'demonstrated_anchor_changed'):
                        app.action(args, object(), learned=operation, anchor=self.step)
                    self.assertFalse(calls)
                else:
                    self.assertEqual(app.action(args, object(), learned=operation, anchor=self.step)['status'], 'verified')
                    self.assertEqual(calls, [elements[3]])


if __name__ == '__main__':
    unittest.main()
