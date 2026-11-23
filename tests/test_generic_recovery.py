"""Host meaning is bound once to observed/recorded identity, independent of prose."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_live_recording_recovery as fixture
import test_popup_read_capture as captured

app, tasks, recovery = fixture.app, fixture.tasks, fixture.recovery


class GenericRecovery(unittest.TestCase):
    setUp = fixture.LiveRecordingRecoveryTests.setUp
    args = fixture.LiveRecordingRecoveryTests.args
    bind = fixture.LiveRecordingRecoveryTests.bind

    def save_read(self, *, child=False):
        raw = captured.capture.capture_step(captured.field(id='read'))
        self.selector = tasks._recorded_selector(raw)
        self.sentence = '이 칸에 뭐라고 적혀 있는지 보여줄래'
        rows = []
        if child:
            opener = dict(fixture.button(), id='open', label='조회')
            rows.append(dict(step=opener, form=dict(alias='', text='조회 버튼을 눌러줘', mode=None, argument='')))
            raw['_capture_window'] = dict(exe='Demo.exe', title='Any detail', related=True, after_id='open')
        rows.append(dict(step=raw, form=dict(alias='', text=self.sentence, mode=None, argument='')))
        self.store.save_draft(rows, 'instruction_requires_structure', len(rows))
        source = tasks.read_document('내 녹화', self.directory)
        self.original = (self.directory/'내 녹화.json').read_bytes()
        return recovery._fingerprint(source)

    def binding(self, number=1, **changes):
        return dict(dict(number=number, mode='read-value', selector=deepcopy(self.selector)), **changes)

    def test_live_interpretation_keeps_unrecognized_prose_and_scoped_identity(self):
        fingerprint = self.save_read()
        self.observe.return_value = dict(targets=[dict(role='textfield', automation_id='txtComment',
            enabled=True, offscreen=False, recorded_selector=self.selector)])
        result = self.bind(fingerprint, [self.binding()])
        self.assertEqual(result['status'], 'recorded', result)
        step, = tasks.load_task('실행용', self.directory)['steps']
        self.assertEqual((step['action'], step['selector'], step['description']),
                         ('read-value', self.selector, self.sentence))
        self.assertEqual(result['execution_plan']['status'], 'saved', result)
        self.observe.assert_called_once()
        self.assertEqual(self.observe.call_args.kwargs['lookups']['selectors'], [self.selector])
        self.assertEqual((self.directory/'내 녹화.json').read_bytes(), self.original)
        self.assertFalse(result['input_dispatched'])

    def test_closed_child_uses_recorded_identity_and_defers_resolution(self):
        fingerprint = self.save_read(child=True)
        def scope(_app, _doc, owner, _rows, _sequence, number):
            if number == 2:
                raise tasks.TaskError('recorded_window_not_open')
            return owner
        with patch.object(recovery, '_bind_surface', side_effect=scope):
            result = self.bind(fingerprint, [self.binding(2)])
        self.assertEqual(result['status'], 'recorded', result)
        self.assertEqual(result['execution_plan']['status'], 'saved', result)
        step = tasks.load_task('실행용', self.directory)['steps'][1]
        self.assertEqual(step['window_context']['after_step'], 1)
        self.assertEqual(step['selector'], self.selector)
        self.observe.assert_not_called()
        self.assertFalse(result['input_dispatched'])

    def test_closed_child_cannot_invent_an_unrecorded_target(self):
        fingerprint = self.save_read(child=True)
        with patch.object(recovery, '_bind_surface', side_effect=tasks.TaskError('recorded_window_not_open')):
            result = self.bind(fingerprint, [self.binding(2, selector=dict(role='textfield', automation_id='other'))])
        self.assertEqual(result['diagnostic'], 'recorded_target_required_for_deferred_binding', result)
        self.observe.assert_not_called()
        self.assertFalse((self.directory/'실행용.json').exists())

    def test_first_prepare_returns_recorded_evidence_instead_of_inspect_loop(self):
        self.save_read(child=True)
        with patch.object(recovery, '_bind_surface', side_effect=tasks.TaskError('recorded_window_not_open')):
            report = recovery.run(app, self.args(prepare=True))
        self.assertEqual(report['diagnostic'], 'instruction_requires_structure', report)
        self.assertEqual(report['interpretation_hint']['selector'], self.selector)
        self.assertEqual(report['interpretation_hint']['validation'], 'after_recorded_opener')
        self.assertEqual(report['interpretation_hint']['text'], self.sentence)
        from recording_followup import followup
        self.assertEqual(followup(report)['next_action']['kind'], 'bind_existing_observation')
        self.observe.assert_not_called()

    def test_static_text_can_teach_a_read_without_becoming_a_button(self):
        raw = captured.field()
        raw['target']['locator'] = dict(role='statictext', automation_id='statusText', name='상태')
        source = captured.capture.capture_step(raw)
        self.assertEqual(source['action'], 'manual')
        selector = tasks._recorded_selector(source)
        self.assertEqual(selector, dict(role='statictext', automation_id='statusText'))
        step = tasks.interpret_step(source, '지금 표시된 상태가 뭔지 보고해줘', 'read-value', selector)
        self.assertEqual(step['action'], 'read-value')

    def test_unknown_mode_and_extra_payload_are_rejected(self):
        fingerprint = self.save_read()
        for update in (dict(mode='shell'), dict(code='anything')):
            with self.subTest(update=update):
                if 'mode' in update:
                    with self.assertRaisesRegex(tasks.TaskError, 'invalid_recovery_binding'):
                        self.bind(fingerprint, [self.binding(**update)])
                else:
                    self.observe.return_value = dict(targets=[])
                    report = self.bind(fingerprint, [self.binding(**update)])
                    self.assertEqual(report['diagnostic'], 'invalid_recovery_binding')


if __name__ == '__main__':
    unittest.main()
