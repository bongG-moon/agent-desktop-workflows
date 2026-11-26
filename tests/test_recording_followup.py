"""Saved preparation is reused by the model rather than offered as another probe."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
from recording_followup import compact_preparation, followup
from recorder_launch import _receipt


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
        self.assertEqual(result['edit_args'], ['review', '--task', '원본', '--edit'])
        self.assertEqual(result['restart_args'], ['review', '--task', '원본', '--restart'])
        self.assertEqual(result['next_action']['args'], [
            'try-recording', '--task', '보정 사본', '--summary'])
        self.assertEqual(result['next_action']['required_parameters'], ['Family'])
        self.assertEqual(result['live_recovery'], evidence)
        self.assertNotIn('recover_args', result)

    def test_direct_saved_recording_supplies_registration_and_first_test_without_observation(self):
        result = followup(self.ready())
        registration = result['next_action']['register_and_test']
        self.assertEqual(registration['args'], ['register', '--task', '원본', '--run', '--summary'])
        self.assertEqual(registration['required_options'], ['--out'])
        self.assertFalse(result['input_dispatched'])

    def test_host_output_preserves_the_session_trial_and_effective_registration(self):
        token = '3' * 32
        for name in ('원본', '보정 사본'):
            with self.subTest(name=name):
                source = self.ready()
                if name != '원본':
                    source['execution_plan'] = dict(status='not_compatible')
                    source['live_recovery'] = dict(self.ready(name), host_action='use_prepared_task')
                session = _receipt(followup(source), token)
                rendered = followup(deepcopy(session))
                self.assertEqual(rendered['next_action']['args'], ['try-recording', '--session', token, '--summary'])
                self.assertEqual(rendered['next_action']['register_and_test']['args'],
                                 ['register', '--task', name, '--run', '--summary'])
                self.assertEqual(rendered, session)

    def test_cached_session_does_not_offer_prefix_bindings_to_fix_a_later_step(self):
        source = dict(status='recorded', input_dispatched=False, task={'name':'원본'},
                      live_recovery=dict(status='needs_grounding', source_task='원본',
                                         step_number=3, input_dispatched=False,
                                         steps=[{'number':3, 'text':'Open result', 'window':'Main'}],
                                         live_scopes=[{'targets':['unrelated control'] * 100}],
                                         bindings_template={'steps':[{'number':1}, {'number':2}]},
                                         next_action={'kind':'bind_existing_observation'}))
        session = _receipt(followup(source), '4' * 32)
        self.assertEqual(followup(deepcopy(session)), session)
        self.assertEqual(session['next_action'], dict(kind='review_unresolved_instruction',
                         number=3, text='Open result', window='Main'))
        rendered = compact_preparation(session)
        self.assertNotIn('live_scopes', rendered['live_recovery'])
        self.assertNotIn('bindings_template', rendered['live_recovery'])
        self.assertNotIn('next_action', rendered['live_recovery'])
        self.assertEqual(len(session['live_recovery']['live_scopes'][0]['targets']), 100)

    def test_unresolved_binding_uses_existing_scope_and_template(self):
        receipt = dict(status='recorded', input_dispatched=False, task={'name':'원본'},
                       recover_args=['recover','--task','원본','--prepare'],
                       live_recovery=dict(status='needs_grounding', source_task='원본',
                            step_number=2, live_scopes=[{'targets': ['visible']}],
                            bindings_template={'steps': [{'number':2, 'mode':'fixed'}]}))
        result = followup(receipt)
        self.assertEqual(result['next_action']['kind'], 'bind_existing_observation')
        self.assertEqual(result['next_action']['template'], 'live_recovery.bindings_template')
        self.assertNotIn('recover_args', result)

    def test_direct_prepare_failure_also_supplies_single_binding_continuation(self):
        receipt = dict(status='needs_grounding', input_dispatched=False, source_task='원본',
                       step_number=3, live_scopes=[], bindings_template={'steps': [{'number':2, 'through':4}]})
        result = followup(receipt)
        self.assertEqual(result['next_action']['template'], 'bindings_template')
        self.assertEqual(result['next_action']['args'], ['recover', '--task', '원본'])

    def test_summary_keeps_the_existing_binding_payload_when_current_step_is_actionable(self):
        source = dict(status='needs_grounding', source_task='원본', input_dispatched=False, step_number=3,
                      steps=[dict(number=2), dict(number=3, text='Read', scope_index=0)],
                      live_scopes=[{'targets': ['unrelated'] * 120}],
                      interpretation_hint=dict(number=3, selector={'role':'textfield','query':'Memo'}),
                      bindings_template={'source_fingerprint':'fingerprint', 'steps':[{'number':1}]})
        full = followup(source)
        compact = compact_preparation(full)
        self.assertEqual(compact['next_action']['kind'], 'bind_existing_observation')
        self.assertEqual(compact['bindings_template'], full['bindings_template'])
        self.assertEqual(compact['interpretation_hint'], full['interpretation_hint'])
        self.assertEqual(compact['steps'], [dict(number=3, text='Read')])
        self.assertNotIn('live_scopes', compact)

    def test_partial_menu_template_offers_the_current_step_image_and_preserves_consent(self):
        args = ['recover', '--task', '원본', '--only', '3', '--image']
        report = dict(status='needs_grounding', source_task='원본', input_dispatched=False, step_number=3,
                      steps=[dict(number=3, image_args=args)], live_scopes=[],
                      bindings_template={'steps':[{'number':1}]})
        result = compact_preparation(followup(report))
        self.assertEqual(result['next_action']['args'], args)
        self.assertTrue(result['next_action']['capture_requires_consent'])

    def test_ambiguous_dispatch_never_becomes_a_replay_instruction(self):
        for status, dispatched in [('uncertain', None), ('delivered_unverified', True), ('recorded', None)]:
            receipt = dict(self.ready(), status=status, input_dispatched=dispatched)
            before = deepcopy(receipt)
            self.assertEqual(followup(receipt), before)
            self.assertNotIn('next_action', receipt)
