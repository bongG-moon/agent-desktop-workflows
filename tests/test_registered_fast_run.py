"""Registered workflows should need one CLI call to run and report a verified workbook."""
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import python_workflow as flow


class RegisteredFastRunTests(unittest.TestCase):
    def setUp(self):
        self.recipe = dict(schema=1, name='Report', exe='Example.exe', window='Example', steps=[
            dict(action='invoke', selector=dict(role='button', name='Export')),
            dict(action='verify-file', output=dict(kind='file', path=r'C:\Reports\example.xlsx'))])
        self.result = dict(status='completed_unverified', input_dispatched=True, business_commit='not_verified',
                           steps=[dict(number=1, receipt=dict(status='delivered_unverified', input_dispatched=True)),
                                  dict(number=2, receipt=dict(status='verified', file_verified=True,
                                       diagnostic='export_workbook_verified', verified_path=r'C:\Reports\example.xlsx'))])

    def cli(self, result, *args):
        with (patch.object(flow, 'load', return_value=self.recipe),
              patch.object(flow.coordination, 'run_worker', return_value=flow.subprocess.CompletedProcess(
                  [], 3, json.dumps(result))) as worker, redirect_stdout(StringIO()) as stdout):
            code = flow.main(['run', '--recipe', 'workflow.json', *args])
        worker.assert_called_once()
        self.assertNotIn('--read-only', worker.call_args.args[0])
        return code, json.loads(stdout.getvalue())

    def test_one_command_reports_actual_output_and_preserves_delivery_semantics(self):
        code, report = self.cli(self.result, '--summary')
        self.assertEqual(code, 0)
        self.assertEqual(report['outcome'], 'workbook_verified')
        self.assertEqual(report['verified_outputs'][0]['path'], r'C:\Reports\example.xlsx')
        self.assertEqual(report['steps'][0]['status'], 'delivered_unverified')
        self.assertEqual(report['business_commit'], 'not_verified')
        self.assertEqual(self.cli(self.result)[0], 3)

    def test_summary_keeps_failures_and_unproven_delivery(self):
        for changes in (dict(status='uncertain'), dict(status='blocked', stopped_at=2), dict(stopped_at=2)):
            code, report = self.cli(dict(self.result, **changes), '--summary')
            self.assertNotEqual(code, 0)
            self.assertNotIn('outcome', report)
        del self.result['steps'][1]['receipt']['verified_path']
        self.assertEqual(self.cli(self.result, '--summary')[0], 3)

    def test_guide_exposes_physical_scope_and_expected_output_without_an_extra_probe(self):
        recipe = dict(self.recipe, steps=[
            dict(action='set-value', parameter='Family'),
            dict(action='context-menu', spec=dict(path=['Export', 'Active sheet']),
                 window_context=dict(title='Results')),
            dict(action='context-menu', spec=dict(path=['Show details']))])
        guide = flow.run_instructions(recipe, 'device-report')
        self.assertIn('["Results", "우클릭 → Export → Active sheet"]', guide)
        self.assertIn('["Example", "우클릭 → Show details"]', guide)
        self.assertIn('["Family"]', guide)
        self.assertIn('--allow-physical', guide)
        self.assertIn('`' + flow.RUN_COMMAND + ' --summary --set "Family=<값>"`', guide)
        self.assertIn('--allow-physical --remember-physical', guide)
        self.assertIn('화면 확인까지 승인받았으면 `--auto-repair-frame`', guide)
        self.assertIn('verified_outputs', guide)
        self.assertIn(flow.GUIDE_REFERENCE, guide)
        self.assertNotIn('--auto-repair-frame', flow.run_instructions(self.recipe, 'device-report'))
        self.assertNotIn('--allow-physical', flow.run_instructions(self.recipe, 'device-report'))
        keyboard = flow.run_instructions(dict(self.recipe, steps=[dict(action='submit')]), 'device-report')
        self.assertIn('--allow-physical', keyboard)
        self.assertNotIn('--auto-repair-frame', keyboard)
        self.assertNotIn('창 이미지 확인', keyboard)
        self.assertNotIn('창 이미지 확인', guide)

    def test_conditional_scene_requires_input_and_only_its_scoped_window_images(self):
        conditional = dict(action='conditional-open', label='Open the item', window_context=dict(title='Viewer'))
        menu = dict(action='context-menu', spec=dict(path=['Export']), window_context=dict(title='Results'))
        for steps in ([conditional, conditional], [conditional, menu]):
            with self.subTest(steps=steps):
                guide = flow.run_instructions(dict(self.recipe, steps=steps), 'device-report')
                self.assertIn('조건을 판단할 때 ["Viewer"] 창의 현재 이미지를 읽는다', guide)
                self.assertIn('실제 입력과 해당 창 이미지 확인이 모두 승인', guide)
                self.assertIn('실행 전에 한 번에 묻는다', guide)
                self.assertIn('`needs_scene`', guide)
                self.assertNotIn('실제 입력만 승인됐으면', guide)
                self.assertEqual('--auto-repair-frame' in guide, menu in steps)

    def test_shell_syntax_in_a_registered_name_stays_out_of_the_command(self):
        name = 'Family$(Get-Location)'
        guide = flow.run_instructions(dict(self.recipe, steps=[dict(action='set-value', parameter=name)]), 'device-report')
        self.assertIn(json.dumps([name]), guide)
        command = next(line for line in guide.splitlines() if line.startswith('`python '))
        self.assertNotIn(name, command)
        self.assertIn('--set "<변수명>=<값>"', command)

    def test_live_root_is_checked_once_before_two_actions_without_scanning_children(self):
        binding = MagicMock(hwnd=31, pid=42)
        uia = MagicMock()
        uia.pid.return_value = binding.pid
        recipe = dict(self.recipe, steps=[self.recipe['steps'][0]] * 2)
        events = []

        def read_once(owner, session):
            events.append('root')
            return probe(owner, session)

        def invoke(*args, **kwargs):
            events.append('invoke')
            return dict(status='delivered_unverified', diagnostic='business_result_not_verified',
                        input_dispatched=True)

        probe = flow.check_window_readability
        with (patch.object(flow, 'bind', return_value=binding) as bind,
              patch.object(flow.app.bridge, 'UIA') as session,
              patch.object(flow.app, 'guard'), patch.object(flow.app.visibility, 'restore_for_action'),
              patch.object(flow.lookup, 'resolve'), patch.object(flow.live, 'execute', side_effect=invoke),
              patch.object(flow, 'check_window_readability', side_effect=read_once)):
            session.return_value.__enter__.return_value = uia
            report = flow.run(recipe)
        self.assertEqual(events, ['root', 'invoke', 'invoke'])
        bind.assert_called_once()
        session.assert_called_once()
        uia.pid.assert_called_once_with(uia.from_handle.return_value)
        uia.children.assert_not_called()
        check = report['readability_check']
        self.assertEqual((check['scope'], check['count'], check['status'], check['input_dispatched']),
                         ('window_root', 1, 'observed', False))
        self.assertEqual(flow.summarize_execution(report, flow.selected_steps(recipe, None))['readability_check'], check)

    def test_supplied_hwnd_uses_only_that_window_and_still_guards_the_binding(self):
        row = dict(hwnd=42, title='Report')
        with (patch.object(flow.app.bridge, 'set_dpi_awareness'), patch.object(flow.app.bridge, 'Binding') as binding,
              patch.object(flow.app, 'guard') as guard, patch.object(flow.app, 'windows', return_value=[row]) as windows):
            self.assertIs(flow.bind('App.exe', 'Report', 42), binding.return_value)
            windows.assert_called_once_with('App.exe', only_hwnd=42)
            binding.assert_called_once_with(row)
            guard.assert_called_once_with(binding.return_value, binding.return_value, 'Report')
            for title, hwnd in [('Changed', 42), ('Report', 71)]:
                with self.assertRaisesRegex(flow.app.SelectionError, 'window_not_found'):
                    flow.bind('App.exe', title, hwnd)
            self.assertEqual(binding.call_count, 1)
            self.assertIs(flow.bind('App.exe', 'Report'), binding.return_value)
            windows.assert_called_with('App.exe')

    def test_unreadable_or_mismatched_root_stops_before_first_business_input(self):
        binding = MagicMock(hwnd=31, pid=42)
        recipe = dict(self.recipe, steps=self.recipe['steps'][:1])
        for pid, reason in [(None, 'window_root_unreadable'), (99, 'window_root_process_mismatch')]:
            with (self.subTest(pid=pid), patch.object(flow, 'bind', return_value=binding),
                  patch.object(flow.app.bridge, 'UIA') as sessions,
                  patch.object(flow.app.visibility, 'restore_for_action'),
                  patch.object(flow.lookup, 'resolve') as lookup, patch.object(flow.live, 'execute') as execute):
                uia = sessions.return_value.__enter__.return_value
                uia.pid.return_value = pid
                report = flow.run(recipe)
                self.assertEqual(report['status'], 'blocked')
                self.assertEqual(report['diagnostic'], reason)
                self.assertIs(report['input_dispatched'], False)
                self.assertEqual(report['readability_check']['count'], 1)
                self.assertEqual(report['steps'], [])
                uia.from_handle.assert_called_once_with(binding.hwnd)
                lookup.assert_not_called()
                execute.assert_not_called()

    def test_preexisting_registered_guide_updates_once_without_window_or_recipe_changes(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(flow, 'bind') as bind:
            skill = Path(folder) / 'registered'
            (skill / 'scripts').mkdir(parents=True)
            (skill / 'scripts/run.py').write_text(flow.RUNNER.replace('RUNTIME_PARENT', '2'), encoding='utf8')
            recipe = skill / 'workflow.json'
            recipe.write_text(json.dumps(self.recipe), encoding='utf8')
            original = recipe.read_bytes()
            guide = skill / 'SKILL.md'
            legacy = flow.workflow_notes(self.recipe, None, False)
            guide.write_text('---\nname: registered\ndescription: "test"\n---\n\n' + flow.REGISTERED_HEADING +
                             flow.GUIDE_BEGIN + 'Old run command\n' + flow.GUIDE_END + legacy +
                             'Extra user instructions\n', encoding='utf8')
            self.assertEqual(flow.refresh_guide(skill)['status'], 'guide_updated')
            saved = guide.read_bytes()
            self.assertEqual(flow.refresh_guide(skill)['status'], 'guide_current')
            self.assertEqual(saved, guide.read_bytes())
            self.assertIn(b'--summary', saved)
            self.assertIn(b' `/registered`', saved)
            self.assertIn(b'Skill(skill="registered")', saved)
            self.assertIn(b'Extra user instructions', saved)
            self.assertNotIn(b'Old run command', saved)
            self.assertNotIn('업무명, 대상 창과 순서'.encode(), saved)
            self.assertIn(legacy, (skill / flow.GUIDE_REFERENCE).read_text(encoding='utf8'))
            self.assertEqual(recipe.read_bytes(), original)
            bind.assert_not_called()

    def test_migration_preserves_an_existing_user_reference_with_the_same_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            skill = Path(folder) / 'registered'
            (skill / 'scripts').mkdir(parents=True)
            (skill / 'scripts/run.py').write_text(flow.RUNNER.replace('RUNTIME_PARENT', '2'), encoding='utf8')
            (skill / 'workflow.json').write_text(json.dumps(self.recipe), encoding='utf8')
            guide = skill / 'SKILL.md'
            guide.write_text('---\nname: registered\ndescription: "test"\n---\n\n' + flow.REGISTERED_HEADING +
                             'Additional operating condition\n', encoding='utf8')
            old = guide.read_bytes()
            reference = skill / flow.GUIDE_REFERENCE
            reference.write_text('My existing document', encoding='utf8')
            with self.assertRaisesRegex(flow.WorkflowError, 'guide_reference_conflict'):
                flow.refresh_guide(skill)
            self.assertEqual(guide.read_bytes(), old)
            self.assertEqual(reference.read_text(encoding='utf8'), 'My existing document')
