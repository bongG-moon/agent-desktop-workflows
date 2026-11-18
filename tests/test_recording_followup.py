"""Saved preparation is reused by the model rather than offered as another probe."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
from recording_followup import followup


class RecordingFollowupTests(unittest.TestCase):
    def ready(self, name='원본'):
        return dict(status='recorded', input_dispatched=False,
                    task=dict(name=name, exe='App.exe', window='업무 "창" '),
                    execution_plan=dict(status='saved', required_parameters=['Family']))

    def test_prepared_copy_name_and_parameters_drive_the_next_action(self):
        source = dict(self.ready(), draft=True, recover_args=['recover', '--task', '원본', '--prepare'])
        source['execution_plan'] = dict(status='not_compatible')
        source['live_recovery'] = dict(self.ready('보정 사본'), host_action='use_prepared_task')
        evidence = deepcopy(source['live_recovery'])
        result = followup(source)
        self.assertEqual(result['effective_task'], '보정 사본')
        self.assertEqual(result['next_action']['args'], [
            'replay', '--task', '보정 사본', '--exe', 'App.exe', '--window', '업무 "창" '])
        self.assertEqual(result['next_action']['required_parameters'], ['Family'])
        self.assertEqual(result['live_recovery'], evidence)
        self.assertNotIn('recover_args', result)

    def test_direct_saved_recording_supplies_registration_and_first_test_without_observation(self):
        result = followup(self.ready())
        registration = result['next_action']['register_and_test']
        self.assertEqual(registration['args'], ['register', '--task', '원본', '--run', '--summary'])
        self.assertEqual(registration['required_options'], ['--out'])
        self.assertFalse(result['input_dispatched'])

    def test_unresolved_binding_uses_existing_scope_and_template(self):
        receipt = dict(status='recorded', input_dispatched=False, task={'name':'원본'},
                       recover_args=['recover','--task','원본','--prepare'],
                       live_recovery=dict(status='needs_grounding', source_task='원본',
                            live_scopes=[{'targets': ['visible']}], bindings_template={'steps': []}))
        result = followup(receipt)
        self.assertEqual(result['next_action']['kind'], 'bind_existing_observation')
        self.assertEqual(result['next_action']['template'], 'live_recovery.bindings_template')
        self.assertNotIn('recover_args', result)

    def test_direct_prepare_failure_also_supplies_single_binding_continuation(self):
        receipt = dict(status='needs_grounding', input_dispatched=False, source_task='원본',
                       live_scopes=[], bindings_template={'steps': []})
        result = followup(receipt)
        self.assertEqual(result['next_action']['template'], 'bindings_template')
        self.assertEqual(result['next_action']['args'], ['recover', '--task', '원본'])

    def test_ambiguous_dispatch_never_becomes_a_replay_instruction(self):
        for status, dispatched in [('uncertain', None), ('delivered_unverified', True), ('recorded', None)]:
            receipt = dict(self.ready(), status=status, input_dispatched=dispatched)
            before = deepcopy(receipt)
            self.assertEqual(followup(receipt), before)
            self.assertNotIn('next_action', receipt)
