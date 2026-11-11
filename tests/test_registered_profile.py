"""Saved workflow export preserves values and never treats descriptions as code."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/desktop-select/scripts'))
import intent_tasks
import learned_tasks as tasks
import registered_profile as profiles


def profile():
    return {'schema': 1, 'name': '등록한 조회 작업', 'exe': 'Example.exe', 'window': 'Example business form',
            'steps': [
                {'action': 'set-value', 'selector': {'automation_id': 'State', 'role': 'combobox'}, 'value': 'N'},
                {'action': 'set-value', 'selector': {'automation_id': 'Family', 'role': 'combobox'}, 'parameter': 'Family'},
                {'action': 'invoke', 'selector': {'automation_id': 'Query', 'role': 'button'}, 'label': '조회'},
                {'action': 'wait', 'seconds': 0.25},
                {'action': 'invoke', 'selector': {'automation_id': 'Export', 'role': 'button'}},
            ]}


def plan(*steps):
    return {'schema': 1, 'name': '원본 녹화', 'exe': 'Example.exe', 'window': 'Example business form', 'steps': list(steps)}


def saved_select(identifier, spec=None):
    result = {'action': 'select', 'role': 'combobox', 'match': 'automation_id', 'target': identifier,
              'recorded_effect': 'matched'}
    if spec:
        result['instruction'] = spec
    else:
        result.update(parameter=identifier, value='Old')
    return result


def selector(source, operation):
    return {'role': operation['role'], 'automation_id': operation['target'],
            'ancestors': [{'role': 'group', 'automation_id': 'Conditions', 'name': 'Filters'}]}


class RegisteredProfileTests(unittest.TestCase):
    def test_schema_normalizes_independent_copy_and_bounded_wait(self):
        original = profile()
        original['steps'][1]['selector']['ancestors'] = [{'role': 'group', 'name': 'Filters'}]
        original['steps'].append(dict(original['steps'][1], parameter='FAMILY', default='Old'))
        original['steps'].append({'action': 'wait', 'seconds': 120})
        normalized = profiles.validate(original)
        self.assertEqual(normalized['steps'][-2]['parameter'], 'Family')
        self.assertEqual(original['steps'][-2]['parameter'], 'FAMILY')
        normalized['steps'][1]['selector']['ancestors'][0]['name'] = 'Changed'
        self.assertEqual(original['steps'][1]['selector']['ancestors'][0]['name'], 'Filters')

    def test_closed_schema_rejects_unknown_data_invalid_roles_values_and_paths(self):
        base = profile()
        bad = [dict(base, schema=True), dict(base, code='print(1)'), dict(base, exe=r'C:\Example.exe'),
               dict(base, exe='Example.exe:other'), dict(base, steps=[]), dict(base, steps=base['steps'] * 21),
               dict(base, window='wrong\nwindow')]
        step = base['steps'][0]
        for change in ({'action': 'exec'}, {'code': 'os.system(...)'}, {'value': ''}, {'parameter': 'Family'},
                       {'default': 'Old'}, {'selector': {'automation_id': 'State', 'role': 'button'}},
                       {'selector': {'name': 'State', 'role': 'combobox'}},
                       {'selector': {'automation_id': 'State', 'role': 'combobox', 'point': [1, 2]}},
                       {'selector': {'automation_id': 'State', 'role': 'combobox', 'ancestors': [{}]}},
                       {'selector': {'automation_id': 'State', 'role': 'combobox', 'ancestors': [
                           {'automation_id': 'Parent', 'role': 'group'}] * 17}}):
            bad.append(dict(base, steps=[dict(step, **change)]))
        for value in (-1, 121, True, float('nan'), float('inf'), '1'):
            bad.append(dict(base, steps=[{'action': 'wait', 'seconds': value}]))
        for value in (' spaced ', 'bad=name', '{Family}', '', None):
            bad.append(dict(base, steps=[dict(base['steps'][1], parameter=value)]))
        for item in bad:
            with self.subTest(item=item), self.assertRaises(profiles.ProfileError):
                profiles.validate(item)

    def test_profile_errors_are_cli_task_errors(self):
        with self.assertRaises(tasks.TaskError):
            profiles.validate({})

    def test_instruction_like_value_is_preserved_as_data_not_generated_python(self):
        value = '__import__("os").system("example")'
        requested = profile()
        requested['steps'][0]['value'] = value
        self.assertEqual(profiles.validate(requested)['steps'][0]['value'], value)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'literal-value'
            profiles.export_skill(requested, out)
            self.assertEqual(profiles.load(out / 'workflow.json')['steps'][0]['value'], value)
            self.assertNotIn(value, (out / 'scripts/run.py').read_text(encoding='utf8'))

    def test_legacy_fixed_required_and_saved_default_remain_distinct(self):
        old = plan(saved_select('State', {'mode': 'fixed', 'text': '', 'value': 'N'}),
                   saved_select('Family', {'mode': 'required', 'text': '', 'parameter': 'Family'}),
                   saved_select('Version'),
                   {'action': 'open', 'target': 'Query', 'match': 'automation_id', 'role': 'button', 'expand': False})
        original = copy.deepcopy(old)
        calls = []
        def resolve(source, operation):
            calls.append((copy.deepcopy(source), copy.deepcopy(operation)))
            result = selector(source, operation)
            source.clear()
            operation.clear()
            return result
        exported = profiles.from_task(old, None, resolve)
        first, second, third, last = exported['steps']
        self.assertEqual(first['value'], 'N')
        self.assertNotIn('parameter', first)
        self.assertEqual(second['parameter'], 'Family')
        self.assertNotIn('default', second)
        self.assertEqual((third['parameter'], third['default']), ('Version', 'Old'))
        self.assertEqual(last['action'], 'invoke')
        self.assertEqual(exported['name'], old['name'])
        self.assertEqual(old, original)
        self.assertEqual(len(calls), 4)

    def test_intent_registration_uses_confirmed_operations_and_one_live_resolution_each(self):
        source = intent_tasks.make_step({'role': 'group', 'automation_id': 'Conditions'},
                                        '이 조건에는 {Family}를 입력하고 조회해 줘')
        old = plan(source)
        resolution = {'schema': 1, 'plan_digest': intent_tasks.plan_digest(old), 'steps': [
            {'number': 1, 'actions': [
                {'action': 'select', 'target': 'Family', 'parameter': 'Family'},
                {'action': 'open', 'target': 'Query', 'role': 'button', 'completion': 'delivery'},
            ]}]}
        original = copy.deepcopy((old, resolution))
        resolve = Mock(side_effect=selector)
        result = profiles.from_task(old, resolution, resolve)
        self.assertEqual([step['action'] for step in result['steps']], ['set-value', 'invoke'])
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(resolve.call_args_list[0].args[0], source)
        self.assertEqual((old, resolution), original)
        with self.assertRaisesRegex(profiles.ProfileError, 'registered_resolution_required'):
            profiles.from_task(old, None, resolve)
        changed = copy.deepcopy(old)
        changed['steps'][0]['instruction']['text'] += '!'
        with self.assertRaisesRegex(ValueError, 'resolution_plan_changed'):
            profiles.from_task(changed, resolution, resolve)

    def test_unsupported_later_action_fails_before_resolving_any_targets(self):
        source = intent_tasks.make_step({'role': 'group', 'automation_id': 'Conditions'}, '설정하고 항목 선택')
        old = plan(source)
        resolution = {'schema': 1, 'plan_digest': intent_tasks.plan_digest(old), 'steps': [
            {'number': 1, 'actions': [{'action': 'select', 'target': 'State', 'value': 'N'},
                                      {'action': 'open', 'target': 'Item', 'role': 'listitem'}]}]}
        resolve = Mock(side_effect=AssertionError('registration should fail before observing targets'))
        with self.assertRaisesRegex(profiles.ProfileError, 'registered_step_requires_supported_action'):
            profiles.from_task(old, resolution, resolve)
        for unsupported in ({'action': 'manual', 'reason': 'capture_target_unavailable'},
                            {'action': 'open', 'target': 'Expand', 'role': 'combobox',
                             'match': 'automation_id', 'expand': True}):
            with self.assertRaisesRegex(profiles.ProfileError, 'registered_step_requires_supported_action'):
                profiles.from_task(plan(saved_select('State'), unsupported), None, resolve)
        resolve.assert_not_called()

    def test_bounded_local_load_rejects_duplicate_keys_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'workflow.json'
            path.write_bytes(b'\xef\xbb\xbf' + json.dumps(profile(), ensure_ascii=False).encode('utf8'))
            self.assertEqual(profiles.load(path), profile())
            for data, code in [(b'{"schema":1,"schema":1}', 'duplicate_registered_key'),
                               (b'\xff', 'invalid_registered_json'),
                               (b' ' * (profiles.MAX_BYTES + 1), 'registered_profile_too_large')]:
                path.write_bytes(data)
                with self.assertRaisesRegex(profiles.ProfileError, code):
                    profiles.load(path)

    def test_existing_or_redirected_export_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'saved-workflow'
            target.mkdir()
            sentinel = target / 'keep.txt'
            sentinel.write_text('keep', encoding='utf8')
            with self.assertRaisesRegex(profiles.ProfileError, 'registered_export_already_exists'):
                profiles.export_skill(profile(), target)
            self.assertEqual(sentinel.read_text(encoding='utf8'), 'keep')
            for basename in ('BadName', 'spaces here', '한글', '.hidden'):
                with self.assertRaisesRegex(profiles.ProfileError, 'invalid_registered_skill_slug'):
                    profiles.export_skill(profile(), Path(folder) / basename)
            original = Path.lstat
            def reparse(path, *args, **kwargs):
                if path == Path(folder):
                    info = Mock(st_mode=0, st_file_attributes=1024)
                    return info
                return original(path, *args, **kwargs)
            with patch.object(Path, 'lstat', reparse), self.assertRaisesRegex(profiles.ProfileError, 'redirected_registered_path'):
                profiles.export_skill(profile(), Path(folder) / 'new-workflow')

    def test_export_creates_data_and_fixed_runner_using_sibling_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            out = base / 'registered-example'
            original = profile()
            result = profiles.export_skill(original, out)
            self.assertEqual(result['status'], 'exported')
            self.assertFalse(result['input_dispatched'])
            self.assertEqual(result['parameters'], ['Family'])
            self.assertEqual(profiles.load(out / 'workflow.json'), original)
            self.assertIn('0.6.0', (out / 'SKILL.md').read_text(encoding='utf8'))
            self.assertEqual((out / 'scripts/run.py').read_text(encoding='utf8'), profiles.RUNNER)
            runtime = base / 'desktop-select/scripts'
            runtime.mkdir(parents=True)
            (runtime / 'registered_runtime.py').write_text(
                'import json, sys\ndef main(*, profile_path):\n'
                ' print(json.dumps({"path":str(profile_path),"args":sys.argv[1:]}))\n return 0\n', encoding='utf8')
            run = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(out / 'scripts/run.py'),
                                  '--set', 'Family=Hybrid', '--read-only'], capture_output=True, text=True,
                                 encoding='utf8', timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            observed = json.loads(run.stdout)
            self.assertEqual(Path(observed['path']), out / 'workflow.json')
            self.assertEqual(observed['args'], ['--set', 'Family=Hybrid', '--read-only'])
            self.assertEqual(original, profile())
