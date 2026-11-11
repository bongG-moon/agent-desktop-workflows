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
        named = deepcopy(recording()['steps'][0])
        named['match'] = 'name'
        for unsupported in (manual, expanded, choice, named):
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
