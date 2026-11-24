"""The real-model benchmark permits only its named synthetic work and issued repairs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import benchmark_recorded_host as benchmark


class RecordedHostPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.skill = self.project / '.claude/skills/desktop-select'
        self.skill.mkdir(parents=True)
        self.registered = self.project / '.claude/skills' / benchmark.REGISTERED
        self.desktop = self.skill / 'scripts/desktop_select.py'
        self.workflow = self.skill / 'scripts/python_workflow.py'
        self.runner = self.registered / 'scripts/run.py'
        self.validate = benchmark.permission_validator(self.root, self.project, self.skill)

    def command(self, script, *args):
        def literal(value):
            if isinstance(value, Path) and value.is_relative_to(self.project):
                return str(value.relative_to(self.project))
            return str(value)
        quoted = ' '.join('"' + literal(v) + '"' for v in (script, *args))
        return dict(tool_name='PowerShell', input=dict(command='python -X utf8 -B ' + quoted))

    def receipt(self, receipt, event_type='user'):
        event = dict(event=dict(type=event_type, message=dict(content=[
            dict(type='tool_result', content=json.dumps(receipt))])))
        with (self.root / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')

    def repair(self, ticket='a' * 48, frame='b' * 64):
        self.receipt(dict(status='blocked', menu_repair=dict(
            ticket=ticket, frame=frame, width=400, height=200,
            image=str(Path(tempfile.gettempdir()) / 'desktop-select-grounding' / (frame + '.png')),
            frame_args=['--repair-frame', ticket, '--allow-physical'],
            resume_args=['--resume-menu', ticket, '--frame', frame, '--point', '<x>', '<y>',
                         '--allow-physical', '--summary', '--set', 'Family=Hybrid'])))
        return ticket, frame

    def test_first_trial_and_exact_registration_are_allowed(self):
        self.assertTrue(self.validate(self.command(self.desktop, 'try-recording', '--task', benchmark.ORIGINAL,
                                                  '--set', 'Family=Hybrid', '--allow-physical')))
        self.assertTrue(self.validate(self.command(self.desktop, 'try-recording', '--task', benchmark.ORIGINAL,
                                                  '--set', 'Family=Hybrid', '--set', 'MES Setup=N', '--allow-physical')))
        self.assertTrue(self.validate(self.command(self.desktop, 'try-recording', '--task', benchmark.ORIGINAL,
                                                  '--set', 'Family=Hybrid', '--allow-physical', '--auto-repair-frame',
                                                  '--summary')))
        self.assertTrue(self.validate(self.command(self.workflow, 'register', '--task', benchmark.ORIGINAL,
                                                  '--out', self.registered)))
        self.assertTrue(self.validate(self.command(self.runner, '--summary', '--set', 'Family=Hybrid',
                                                  '--allow-physical')))

    def test_wrong_task_parameter_destination_or_extra_shell_are_refused(self):
        for args in [('try-recording', '--task', 'Other task'),
                     ('try-recording', '--task', benchmark.ORIGINAL, '--set', 'Family=Other'),
                     ('try-recording', '--task', benchmark.ORIGINAL, '--set', 'Family=Hybrid', '--set', 'Family=Hybrid'),
                     ('try-recording', '--task', benchmark.ORIGINAL, '--trial-id', 'force-repeat'),
                     ('try-recording', '--task', benchmark.ORIGINAL, '--run'),
                     ('try-recording', '--task', benchmark.ORIGINAL, '--task', benchmark.EFFECTIVE),
                     ('try-recording', '--task', benchmark.ORIGINAL, '--allow-physical', '--background-only')]:
            with self.subTest(args=args):
                self.assertFalse(self.validate(self.command(self.desktop, *args)))
        self.assertFalse(self.validate(self.command(self.workflow, 'register', '--task', benchmark.ORIGINAL,
                                                   '--out', self.project / 'other')))
        request = self.command(self.desktop, 'try-recording', '--task', benchmark.ORIGINAL)
        request['input']['command'] += '; Write-Output done'
        self.assertFalse(self.validate(request))

    def test_required_arguments_cannot_be_omitted(self):
        self.assertFalse(self.validate(self.command(self.desktop, 'try-recording', '--allow-physical')))
        self.assertFalse(self.validate(self.command(self.workflow, 'register', '--task', benchmark.ORIGINAL)))
        self.assertFalse(self.validate(self.command(self.desktop, 'show', '--prepare')))

    def test_corrected_task_name_must_be_returned_by_the_engine(self):
        corrected = 'Synthetic saved device report corrected'
        call = self.command(self.workflow, 'register', '--task', corrected, '--out', self.registered)
        self.assertFalse(self.validate(call))
        self.receipt(dict(status='verified', effective_task=corrected))
        self.assertTrue(self.validate(call))

    def test_issued_menu_ticket_frame_and_bounded_point_only(self):
        ticket, frame = 'a' * 48, 'b' * 64
        call = self.command(self.runner, '--resume-menu', ticket, '--frame', frame, '--point', 100, 50,
                            '--allow-physical', '--summary', '--set', 'Family=Hybrid')
        self.assertFalse(self.validate(call))
        self.repair(ticket, frame)
        self.assertTrue(self.validate(call))
        self.assertTrue(self.validate(self.command(self.runner, '--repair-frame', ticket, '--allow-physical')))
        for other_ticket, other_frame, point in [('c' * 48, frame, (100, 50)),
                                                (ticket, 'd' * 64, (100, 50)),
                                                (ticket, frame, (400, 50)),
                                                (ticket, frame, (100, 200))]:
            with self.subTest(ticket=other_ticket, frame=other_frame, point=point):
                self.assertFalse(self.validate(self.command(self.runner, '--resume-menu', other_ticket,
                    '--frame', other_frame, '--point', *point, '--allow-physical')))
        self.assertFalse(self.validate(self.command(self.runner, '--resume-menu', ticket)))

    def test_a_frame_from_another_issued_ticket_cannot_be_mixed(self):
        first, _ = self.repair()
        _, second_frame = self.repair('c' * 48, 'd' * 64)
        self.assertFalse(self.validate(self.command(self.runner, '--resume-menu', first, '--frame', second_frame,
                                                   '--point', 10, 10, '--allow-physical')))

    def test_trial_inspection_only_accepts_returned_receipt_id(self):
        receipt = 'c' * 64
        call = self.command(self.desktop, 'inspect-trial', '--receipt', receipt)
        self.assertFalse(self.validate(call))
        self.receipt(dict(status='blocked', result_inspection=dict(args=['inspect-trial', '--receipt', receipt])))
        self.assertTrue(self.validate(call))
        self.assertFalse(self.validate(self.command(self.desktop, 'inspect-trial', '--receipt', 'd' * 64)))

    def test_public_menu_trial_uses_issued_receipt_and_frame(self):
        receipt, frame = 'a' * 64, 'b' * 64
        inspect = self.command(self.desktop, 'inspect-menu-trial', '--receipt', receipt, '--allow-physical',
                               '--set', 'Family=Hybrid', '--summary')
        self.assertFalse(self.validate(inspect))
        self.receipt(dict(status='blocked', menu_repair=dict(
            frame_args=['inspect-menu-trial', '--receipt', receipt, '--allow-physical', '--set', 'Family=Hybrid'])))
        self.assertTrue(self.validate(inspect))
        resume = self.command(self.desktop, 'resume-menu-trial', '--receipt', receipt, '--frame', frame,
                              '--point', 100, 50, '--allow-physical', '--set', 'Family=Hybrid', '--summary')
        self.assertFalse(self.validate(resume))
        self.receipt(dict(status='observed', frame=frame, width=400, height=200,
            resume_args=['resume-menu-trial', '--receipt', receipt, '--frame', frame, '--point', '<x>', '<y>',
                         '--allow-physical', '--set', 'Family=Hybrid']))
        self.assertTrue(self.validate(resume))
        self.assertFalse(self.validate(self.command(self.desktop, 'resume-menu-trial', '--receipt', receipt,
                                                   '--allow-physical')))
        self.assertFalse(self.validate(self.command(self.desktop, 'inspect-trial', '--receipt', receipt)))

    def test_menu_input_pause_requires_returned_frameless_resume(self):
        receipt = 'a' * 64
        resume = self.command(self.desktop, 'resume-menu-trial', '--receipt', receipt, '--allow-physical')
        self.assertFalse(self.validate(resume))
        self.receipt(dict(status='blocked', resume_args=['resume-menu-trial', '--receipt', receipt, '--allow-physical']))
        self.assertTrue(self.validate(resume))

    def test_assistant_text_does_not_grant_a_ticket(self):
        self.receipt(dict(status='observed', frame_args=['--repair-frame', 'e' * 48]), event_type='assistant')
        self.assertFalse(self.validate(self.command(self.runner, '--repair-frame', 'e' * 48)))

    def test_image_read_requires_a_returned_image(self):
        _, frame = self.repair()
        image = Path(tempfile.gettempdir()) / 'desktop-select-grounding' / (frame + '.png')
        self.assertTrue(self.validate(dict(tool_name='Read', input=dict(file_path=str(image)))))
        self.assertFalse(self.validate(dict(tool_name='Read', input=dict(file_path=str(image.with_name('c' * 64 + '.png'))))))

    def test_first_trial_prompt_does_not_pre_supply_an_execution_route(self):
        prompt = benchmark.request(dict(fixtureWindow='Synthetic window', preparedReceipt={'old_route': 'register --run'}))
        self.assertIn(benchmark.ORIGINAL, prompt)
        self.assertIn('합성 녹화', prompt)
        self.assertNotIn('old_route', prompt)
        self.assertNotIn('register --run', prompt)
        self.assertNotIn(benchmark.EFFECTIVE, prompt)

    def test_file_verification_timing_includes_trial_step_receipts(self):
        self.assertTrue(benchmark.verified_output(dict(status='verified', steps=[
            dict(receipt=dict(status='verified', workbook_verified=True))])))
        self.assertTrue(benchmark.verified_output(dict(status='verified', verified_outputs=[{'path': 'report.xlsx'}])))
        self.assertFalse(benchmark.verified_output(dict(status='verified', steps=[
            dict(receipt=dict(status='verified', effect_verification='matched'))])))
        self.assertFalse(benchmark.verified_output(dict(status='blocked', steps=[
            dict(receipt=dict(status='verified', workbook_verified=True))])))


if __name__ == '__main__':
    unittest.main()
