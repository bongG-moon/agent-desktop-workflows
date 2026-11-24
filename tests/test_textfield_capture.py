"""Recorded ordinary text can be fixed, cleared or supplied per run without recapturing."""
import json
from pathlib import Path
import queue
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select  # Load pinned bridge for the source checkout.
import learned_tasks as tasks
import teach_capture as capture


def textfield(value='00042'):
    return dict(id='search-field', action='set-value', field_label='검색어', value=value,
                target=dict(kind='element', locator=dict(role='textfield', automation_id='searchText')),
                recognition=dict(target='identified', action='supported', result='checkable'))


class TextfieldCaptureTests(unittest.TestCase):
    def test_observed_text_drafts_preserve_literal_data_including_empty_clear(self):
        for value in ('00042', '한글 "따옴표" & A=B', ''):
            with self.subTest(value=value):
                step = capture.capture_step(textfield(value))
                self.assertIn(json.dumps(value, ensure_ascii=False), capture.default_text(step))
                saved = tasks.from_recording('검색', 'Example.exe', 'Example', capture.StepEdits().apply([step]))
                first = saved['steps'][0]
                self.assertEqual((first['action'], first['role']), ('fill', 'textfield'))
                self.assertEqual(first['instruction']['value'], value)
                self.assertEqual(first['instruction']['mode'], 'fixed')

    def test_unreadable_text_keeps_location_and_requires_parameter(self):
        raw = textfield()
        raw.pop('value')
        raw.update(action='manual', recognition=dict(target='identified', action='manual',
                   result='manual', reason='value_readback_unavailable'))
        step = capture.capture_step(raw)
        self.assertEqual(step['action'], 'teach-target')
        self.assertEqual(capture.default_text(step), '입력받은 변수를 검색어 입력란에 입력해줘')
        result = tasks.recorded_step(capture.StepEdits().apply([step])[0])
        self.assertEqual(result['instruction']['parameter'], '검색어')
        self.assertEqual(result['recorded_effect'], 'unobserved')

    def test_changed_value_on_same_field_keeps_explicit_runtime_variable(self):
        step = capture.capture_step(textfield())
        edits = capture.StepEdits()
        edits.store(step, '', '입력받은 {Search} 변수를 검색어 입력란에 입력해줘')
        changed = capture.capture_step(textfield('different demonstration'))
        changed['target']['locator'].update(name='volatile peer caption', runtime_id=[99],
                                            bounds=dict(x=100, y=200))
        saved = tasks.recorded_step(edits.apply([changed])[0])
        self.assertEqual(saved['instruction']['parameter'], 'Search')
        self.assertNotIn('value', saved)
        self.assertNotIn('value', saved['instruction'])

    def test_explicit_empty_fixed_value_has_clear_preview_and_is_not_missing_argument(self):
        step = capture.capture_step(textfield())
        edits = capture.StepEdits()
        edits.store(step, '', '검색어 입력란을 비워줘', 'fixed', '')
        instruction = edits.get(step)['instruction']
        self.assertEqual(instruction['value'], '')
        label, mode, arg, editable = capture.review_interpretation(step, edits.get(step))
        self.assertIn('비우기', label)
        self.assertEqual((mode, arg, editable), ('fixed', '', True))
        tasks.recorded_step(edits.apply([step])[0])

    def test_password_and_combo_editor_values_do_not_survive_capture(self):
        for update in ({'password': True}, {'secure': True},
                       {'ancestors': [dict(role='combobox', automation_id='ownerCombo')]}):
            with self.subTest(update=update):
                raw = textfield('PRIVATE')
                raw['field_label'] = 'PRIVATE'
                raw['target']['locator'].update(update)
                step = capture.capture_step(raw)
                self.assertEqual(step['action'], 'manual')
                self.assertNotIn('PRIVATE', json.dumps(step))
                self.assertFalse(tasks.is_recoverable(step))

    def test_text_point_to_value_refinement_preserves_step_edit(self):
        raw = textfield()
        raw.pop('value')
        raw.update(action='teach-target', recognition=dict(target='identified', action='requires_instruction',
                   result='manual', reason='text_value_unobserved'))
        first = capture.capture_step(raw)
        edits = capture.StepEdits()
        edits.store(first, '', '실행마다 받은 검색어를 사용해줘', 'required', 'Search')
        latest = capture.capture_step(textfield('new'))
        final = tasks.recorded_step(edits.apply([latest])[0])
        self.assertEqual((final['action'], final['instruction']['parameter']), ('fill', 'Search'))
        edits.forget(first['id'])
        self.assertEqual(edits.get(latest), {})


class TextfieldWorkerTests(unittest.TestCase):
    def setUp(self):
        self.binding, self.uia = Mock(hwnd=5), Mock()
        self.notes = capture.CaptureNotes()
        self.worker = capture._Worker(self.binding, Mock(), self.notes, queue.Queue(), queue.Queue())
        self.worker.uia = self.uia
        self.uia.secure.return_value = False
        self.uia.same.side_effect = lambda first, second: first is second
        self.uia.combo_observation.return_value = None
        self.uia.value.return_value = 'old'
        self.element, self.root = object(), object()
        self.observed = dict(element=self.element, root=self.root, window=5,
            node=dict(secure=False, enabled=True, role='textfield', semantic_label='검색어',
                      available_actions=['SetValue'], locator=textfield()['target']['locator']))
        self.uia.locator.return_value = self.observed['node']['locator']
        for method, result in (('textfield_scope', self.root), ('text_readback',
                               dict(value='old', value_evidence=dict(status='observed', source='value_pattern')))):
            patched = patch.object(capture.live_action, method, return_value=result)
            patched.start()
            self.addCleanup(patched.stop)

    def test_click_then_text_change_uses_generic_recorder_with_same_step_id(self):
        first_id = self.worker.capture_action(dict(action='click'), self.observed)
        first, = self.notes.snapshot()
        self.assertEqual(first['action'], 'teach-target')
        self.worker._commit_value('00042')
        final, = self.notes.snapshot()
        self.assertEqual(final['id'], first_id)
        self.assertEqual(final['value'], '00042')
        saved = tasks.recorded_step(capture.StepEdits().apply([final])[0])
        self.assertEqual((saved['action'], saved['instruction']['value']), ('fill', '00042'))

    def test_location_only_click_does_not_fix_the_existing_text(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker.finalize_watch(True)
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'teach-target')
        saved = tasks.recorded_step(capture.StepEdits().apply([final])[0])
        self.assertEqual(saved['instruction']['mode'], 'required')
        self.assertNotIn('old', json.dumps(saved))

    def test_late_protection_discards_previously_watched_value(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        capture.live_action.textfield_scope.side_effect = capture.bridge.BridgeError('target_protected')
        self.worker._commit_value('PRIVATE')
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'manual')
        self.assertEqual(final['recognition']['reason'], 'protected_input_not_recorded')
        self.assertNotIn('PRIVATE', json.dumps(final))

    def test_unnamed_field_keeps_one_value_step_and_the_same_enter_target(self):
        selector = dict(anchor=dict(role='group', name='Conditions'), role='textfield',
                        point=[.4, .2], class_name='NativeEditor')
        self.observed['node']['locator'] = selector
        self.uia.focused.return_value = self.element
        with patch('control_lookup.resolve', return_value=self.element):
            self.worker.capture_action(dict(action='click'), self.observed)
            first, = self.notes.snapshot()
            self.worker._commit_value("Status = 'N'")
            recorded, = self.notes.snapshot()
            self.assertEqual(recorded['id'], first['id'])
            self.assertEqual(recorded['target']['locator'], selector)
            self.assertEqual(recorded['value'], "Status = 'N'")
            self.worker.watched_samples.append((time.monotonic() - .01, "Status = 'N'"))
            self.assertTrue(self.worker._capture_submit(dict(at=time.monotonic())))
        value, submit = self.notes.snapshot()
        self.assertEqual(value['value'], "Status = 'N'")
        self.assertEqual(submit['action'], 'submit')
        self.assertEqual(submit['target'], value['target'])

    def test_activation_click_refines_the_same_field_before_recording_its_value(self):
        selector = dict(anchor=dict(role='group', name='Conditions'), role='textfield',
                        point=[.4, .2], class_name='NativeEditor')
        focused = Mock()
        focused.property.side_effect = lambda p: 50004 if p == 30003 else None
        self.observed['node']['locator'] = dict(role='textfield')
        self.uia.focused.return_value = focused
        self.uia.pid.return_value = self.binding.pid
        self.uia.within.return_value = True
        self.uia.bounds.return_value = dict(x=0,y=0,width=100,height=100)
        self.uia.node.return_value = dict(self.observed['node'], locator=selector)
        with patch('control_lookup.resolve', return_value=focused):
            self.worker.capture_action(dict(action='click',x=50,y=50), self.observed)
            first, = self.notes.snapshot()
            self.worker._commit_value('entered text')
            last, = self.notes.snapshot()
        self.assertEqual(first['action'], 'teach-target')
        self.assertEqual(last['action'], 'set-value')
        self.assertEqual(last['id'], first['id'])
        self.assertEqual(last['value'], 'entered text')

    def test_enter_does_not_use_a_previous_field_after_focus_moves(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        self.uia.focused.return_value = object()
        before = self.notes.snapshot()
        self.assertFalse(self.worker._capture_submit(dict(at=time.monotonic())))
        self.assertEqual(self.notes.snapshot(), before)

    def test_passive_tabgroup_activation_refines_the_actual_focused_editor(self):
        focused = Mock()
        focused.property.side_effect = lambda p: 50004 if p == 30003 else None
        self.uia.focused.return_value = focused
        self.uia.pid.return_value = self.binding.pid
        self.uia.within.return_value = True
        self.uia.bounds.return_value = dict(x=20, y=20, width=100, height=30)
        self.uia.node.return_value = self.observed['node']
        passive = dict(self.observed, node=dict(secure=False, enabled=True, role='tabgroup',
            available_actions=[], locator=dict(role='tabgroup', name='Data'),
            bounds=dict(x=0,y=0,width=300,height=300)))
        first_id = self.worker.capture_action(dict(action='click', x=50, y=35), passive)
        first, = self.notes.snapshot()
        self.assertEqual(first['id'], first_id)
        self.assertEqual(first['action'], 'teach-target')
        self.assertEqual(first['target']['locator']['role'], 'textfield')

    def test_late_typed_editor_replaces_only_its_exact_passive_activation_id(self):
        now = time.monotonic()
        passive = dict(self.observed, node=dict(secure=False, enabled=True, role='tabgroup',
            available_actions=[], locator=dict(role='tabgroup', name='Data'),
            bounds=dict(x=0, y=0, width=300, height=300)))
        self.uia.focused.return_value = None
        self.worker.capture_action(dict(action='click', scope_proven=True, window=5,
            x=50, y=35, at=now-.4), passive)
        unknown, = self.notes.snapshot()
        self.assertEqual(unknown['action'], 'manual')
        self.worker.last_key = now-.2
        self.observed['node']['bounds'] = dict(x=20,y=20,width=100,height=30)
        self.worker.watch(self.observed)
        final, = self.notes.snapshot()
        self.assertEqual(final['id'], unknown['id'])
        self.assertEqual(final['action'], 'set-value')
        self.assertEqual(final['value'], 'old')

    def test_late_typed_editor_does_not_consume_a_different_unknown_click(self):
        now = time.monotonic()
        passive = dict(self.observed, node=dict(secure=False, enabled=True, role='tabgroup',
            available_actions=[], locator=dict(role='tabgroup', name='Data'),
            bounds=dict(x=0,y=0,width=300,height=300)))
        self.uia.focused.return_value = None
        self.worker.capture_action(dict(action='click', scope_proven=True, window=5,
            x=250, y=200, at=now-.4), passive)
        unknown, = self.notes.snapshot()
        self.worker.last_key = now-.2
        self.observed['node']['bounds'] = dict(x=20,y=20,width=100,height=30)
        self.worker.watch(self.observed)
        self.assertEqual(self.notes.snapshot(), [unknown])

    def test_late_typed_editor_cannot_refine_without_a_scoped_activation(self):
        now = time.monotonic()
        self.worker._pending_field_activation = None
        passive = dict(self.observed, node=dict(secure=False, enabled=True, role='tabgroup',
            available_actions=[], locator=dict(role='tabgroup', name='Data'),
            bounds=dict(x=0,y=0,width=300,height=300)))
        self.uia.focused.return_value = None
        self.worker.capture_action(dict(action='click', scope_proven=False, window=5,
            x=50, y=35, at=now-.4), passive)
        unknown, = self.notes.snapshot()
        self.worker.last_key = now-.2
        self.observed['node']['bounds'] = dict(x=20,y=20,width=100,height=30)
        self.worker.watch(self.observed)
        self.assertEqual(self.notes.snapshot(), [unknown])

    def test_queued_typing_keeps_the_real_value_when_first_read_finishes_after_paste(self):
        now = time.monotonic()
        value = 'the complete inserted string'
        capture.live_action.text_readback.return_value = dict(value=value, value_evidence=dict(status='observed'))
        self.worker.capture_action(dict(action='click',at=now-.5), self.observed)
        first, = self.notes.snapshot()
        self.assertEqual(first['action'], 'teach-target')
        self.worker.handle(dict(kind='typing',at=now-.2))
        recorded, = self.notes.snapshot()
        self.assertEqual(recorded['id'], first['id'])
        self.assertEqual(recorded['value'], value)
        self.uia.focused.return_value = self.element
        self.assertTrue(self.worker._capture_submit(dict(at=now-.1)))
        recorded, submitted = self.notes.snapshot()
        self.assertEqual(recorded['value'], value)
        self.assertEqual(submitted['action'], 'submit')

    def test_keyboard_event_before_field_activation_does_not_create_a_value_step(self):
        now = time.monotonic()
        self.worker.capture_action(dict(action='click',at=now), self.observed)
        self.worker.handle(dict(kind='typing',at=now-.1))
        recorded, = self.notes.snapshot()
        self.assertEqual(recorded['action'], 'teach-target')

    def test_enter_uses_proven_native_before_focus_when_submit_recreates_peer(self):
        self.observed['node']['hwnd'] = 123
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker.watched_samples.append((time.monotonic() - .01, 'old'))
        self.uia.focused.side_effect = capture.bridge.BridgeError('target_unavailable')
        event = dict(at=time.monotonic(), submit_focus=123, submit_window=5)
        self.assertTrue(self.worker._capture_submit(event))
        self.assertEqual(self.notes.snapshot()[-1]['action'], 'submit')
        self.uia.focused.assert_not_called()

    def test_key_classification_keeps_text_and_modified_enter_separate(self):
        pump = capture._RowHookPump(self.binding, Mock(), queue.Queue(), queue.Queue())
        def classify(code, **modifiers):
            return pump._key_event(code, **dict(dict(ctrl=False, shift=False, alt=False, win=False), **modifiers))
        self.assertEqual(classify(9, shift=True), dict(kind='press-key', keys='Shift+Tab'))
        self.assertEqual(classify(0x1b), dict(kind='press-key', keys='Esc'))
        self.assertEqual(classify(ord('F'), ctrl=True), dict(kind='press-key', keys='Ctrl+F'))
        self.assertEqual(classify(13), dict(kind='enter'))
        self.assertEqual(classify(13, ctrl=True), dict(kind='press-key', keys='Ctrl+Enter'))
        for code, mods in [(ord('A'), {}), (ord('V'), dict(ctrl=True)), (ord('X'), dict(ctrl=True))]:
            self.assertEqual(classify(code, **mods), dict(kind='typing'))
        for code, mods in [(13, dict(alt=True)), (9, dict(alt=True)), (ord('F'), dict(ctrl=True, alt=True))]:
            self.assertEqual(classify(code, **mods), dict(kind='shortcut'))

    def _native_key_field(self):
        self.binding.pid = 42
        self.element = Mock()
        self.properties = {30020: 77, 30010: True, 30009: True, 30003: 50004}
        self.element.property.side_effect = self.properties.get
        self.observed['element'] = self.element
        self.uia.from_handle.side_effect = {5: self.root, 77: self.element}.get
        self.uia.pid.return_value = 42
        for name, result in [('root_window', 5), ('hwnd_pid', 42)]:
            fake = patch.object(capture.bridge, name, return_value=result)
            fake.start()
            self.addCleanup(fake.stop)
        parent = patch('control_lookup._raw_parent', return_value=self.root)
        parent.start()
        self.addCleanup(parent.stop)
        visible = patch.object(capture.live_action, '_visible', return_value=True)
        visible.start()
        self.addCleanup(visible.stop)
        user = patch.object(capture.bridge, 'user32', create=True)
        self.native = user.start()
        self.native.IsWindow.return_value = True
        self.addCleanup(user.stop)
        return dict(kind='press-key', keys='Tab', submit_focus=77, submit_window=5, at=time.monotonic())

    def test_tab_records_pre_key_field_and_does_not_invent_its_value(self):
        event = self._native_key_field()
        self.worker.capture_action(dict(action='click'), self.observed)
        event['at'] = time.monotonic()
        self.uia.focused.return_value = object()  # Windows has moved on to the next field.
        self.worker.handle(event)
        recorded, = self.notes.snapshot()
        self.assertEqual((recorded['action'], recorded['keys']), ('press-key', 'Tab'))
        self.assertEqual(recorded['target'], textfield()['target'])
        self.assertEqual(capture.default_text(recorded), 'Tab 키 누르기')
        self.assertEqual(self.notes.times[recorded['id']], event['at'])

    def test_focused_unchanged_combo_then_down_does_not_require_an_invented_value(self):
        event = self._native_key_field()
        self.properties[30003] = 50003
        self.observed['node'].update(role='combobox', locator=dict(role='combobox', automation_id='family'))
        self.uia.combo_observation.return_value = self.observed
        self.uia.locator.return_value = self.observed['node']['locator']
        self.uia.focused.return_value = self.element
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker.handle(dict(event, keys='Down', at=time.monotonic()))
        key, = self.notes.snapshot()
        self.assertEqual((key['action'], key['keys']), ('press-key', 'Down'))

    def test_native_combo_editor_records_down_on_the_owning_combo(self):
        event = self._native_key_field()
        combo_element = Mock()
        combo_element.property.side_effect = {30003: 50003, 30009: True, 30010: True}.get
        selector = dict(role='combobox', automation_id='family')
        self.observed.update(element=combo_element)
        self.observed['node'].update(role='combobox', locator=selector)
        self.uia.combo_observation.return_value = self.observed
        self.uia.locator.side_effect = lambda item, root: selector if item is combo_element else {'role': 'textfield'}
        self.worker.capture_action(dict(action='click'), self.observed)
        parent = {self.element: combo_element, combo_element: self.root}
        with patch('control_lookup._raw_parent', side_effect=lambda uia, item: parent[item]), \
                patch.object(capture.live_action, '_combo_visible', return_value=True):
            self.worker.handle(dict(event, keys='Down', at=time.monotonic()))
        key, = self.notes.snapshot()
        self.assertEqual((key['keys'], key['target']['locator']), ('Down', selector))

    def test_confirmed_typing_and_shortcut_produce_two_ordered_steps(self):
        event = self._native_key_field()
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker._commit_value('changed')
        self.worker.watched_samples.append((time.monotonic(), 'changed'))
        self.worker.handle(dict(event, keys='Ctrl+S', at=time.monotonic()))
        value, key = self.notes.snapshot()
        self.assertEqual((value['action'], value['value']), ('set-value', 'changed'))
        self.assertEqual((key['action'], key['keys']), ('press-key', 'Ctrl+S'))

    def test_virtual_field_uses_focus_observed_on_both_sides_of_shortcut(self):
        event = self._native_key_field()
        self.properties.update({30020: 0, 30008: True})
        self.worker.capture_action(dict(action='click'), self.observed)
        self.uia.focused.return_value = self.element
        self.worker.handle(dict(event, keys='F5', submit_focus=5, at=time.monotonic()))
        key, = self.notes.snapshot()
        self.assertEqual((key['keys'], key['target']), ('F5', textfield()['target']))

    def test_virtual_tab_cannot_bind_a_different_post_key_field(self):
        event = self._native_key_field()
        self.properties.update({30020: 0, 30008: True})
        self.worker.capture_action(dict(action='click'), self.observed)
        self.uia.focused.return_value = object()
        self.worker.handle(dict(event, submit_focus=5, at=time.monotonic()))
        self.assertEqual(self.notes.snapshot()[-1]['action'], 'manual')

    def test_escape_can_close_the_previously_focused_native_field(self):
        event = self._native_key_field()
        self.worker.capture_action(dict(action='click'), self.observed)
        self.native.IsWindow.return_value = False
        self.binding.check.side_effect = capture.bridge.BridgeError('window_closed')
        self.worker.handle(dict(event, keys='Esc', at=time.monotonic()))
        self.assertEqual(self.notes.snapshot()[-1]['keys'], 'Esc')

    def test_unscoped_or_protected_key_does_not_retarget_new_focus(self):
        event = self._native_key_field()
        self.worker.handle(dict(event, submit_window=6))
        self.uia.secure.return_value = True
        self.worker.handle(event)
        self.assertEqual([step['action'] for step in self.notes.snapshot()], ['manual', 'manual'])
        self.uia.locator.assert_not_called()
        self.uia.focused.assert_not_called()

    def test_proven_unprotected_field_keeps_key_clue_if_locator_missing(self):
        event = self._native_key_field()
        self.uia.locator.return_value = dict(role='textfield')
        self.uia.anchored_field.return_value = None
        self.worker.handle(dict(event, keys='Ctrl+S'))
        row, = self.notes.snapshot()
        self.assertEqual(row['action'], 'manual')
        self.assertEqual(capture.default_text(row), 'Ctrl+S 키 누르기')

    def test_recent_command_click_is_recorded_after_its_dialog_closes(self):
        now = time.monotonic()
        raw = dict(id='finish', action='click', label='Finish',
                   target=dict(kind='element', locator=dict(role='button', name='Finish')),
                   recognition=dict(target='identified', action='supported', result='manual'))
        self.worker.command_snapshot = (now-.1, [(dict(x=0,y=0,width=100,height=100), raw)])
        self.binding.check.side_effect = capture.bridge.BridgeError('target_window_mismatch')
        down = dict(kind='down', action='click', token=7, window=5, scope_proven=True, at=now, x=50, y=50)
        self.worker.handle(down)
        self.assertEqual(self.notes.snapshot(), [])
        self.worker.tick()  # Await the paired release before closing the recorder part.
        self.worker.handle(dict(kind='up', token=7, inside=True, at=now+.1, x=50, y=50))
        recorded, = self.notes.snapshot()
        self.assertEqual(recorded['target']['locator'], dict(role='button', name='Finish'))
        self.assertEqual(recorded['action'], 'click')

    def test_command_snapshot_never_substitutes_an_unscoped_or_stale_click(self):
        now = time.monotonic()
        self.worker.command_snapshot = (now-5, [(dict(x=0,y=0,width=100,height=100), dict(id='finish'))])
        event = dict(action='click',window=5,scope_proven=True,at=now,x=50,y=50)
        self.assertIsNone(self.worker._command_before(event))
        self.worker.command_snapshot = (now-.1, self.worker.command_snapshot[1])
        self.assertIsNone(self.worker._command_before(dict(event,scope_proven=False)))
        self.assertIsNone(self.worker._command_before(dict(event,window=7)))

    def test_distinct_clicks_on_one_cached_command_keep_distinct_step_ids(self):
        now = time.monotonic()
        raw = dict(id='command-identity', action='click', label='Next',
                   target=dict(kind='element', locator=dict(role='button', name='Next')),
                   recognition=dict(target='identified', action='supported', result='manual'))
        self.worker.command_snapshot = (now-.1, [(dict(x=0,y=0,width=100,height=100), raw)])
        event = dict(action='click',window=5,scope_proven=True,at=now,x=50,y=50)
        first = self.worker._command_before(event)
        second = self.worker._command_before(dict(event, at=now+.1))
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(first['id'], raw['id'])
        self.assertEqual(first['target'], second['target'])
        self.assertEqual(raw['id'], 'command-identity')

    def test_command_inventory_refreshes_while_user_pauses_in_the_input_field(self):
        self.worker.watched = self.observed
        self.worker._last_command_refresh = time.monotonic() - 2
        def refreshed():
            self.worker._last_command_refresh = time.monotonic()
        with (patch.object(capture.recorder.CaptureWorker,'tick'),
              patch.object(self.worker,'_refresh_commands',side_effect=refreshed) as refresh):
            self.worker.tick()
            self.worker.tick()
        refresh.assert_called_once()

    def test_late_closed_dialog_read_keeps_the_last_bounded_command_evidence(self):
        snapshot = (time.monotonic() - .2, [(dict(x=0,y=0,width=100,height=100), dict(id='finish'))])
        self.worker.command_snapshot = snapshot
        with patch('control_lookup.find_all',side_effect=capture.bridge.BridgeError('target_window_mismatch')):
            self.worker._cache_commands()
        self.assertIs(self.worker.command_snapshot, snapshot)

    def test_unnamed_field_anchor_is_checked_against_the_actual_element(self):
        field = Mock()
        field.property.side_effect = lambda p: 'NativeEditor' if p == 30012 else None
        ancestor = Mock()
        uia = Mock()
        uia.binding = Mock(pid=7)
        uia.parent.return_value = ancestor
        uia.pid.return_value = 7
        uia.secure.return_value = False
        uia.basic.return_value = dict(role='group', name='Conditions')
        uia.same.side_effect = lambda a, b: a is b
        uia.bounds.side_effect = lambda e: dict(x=120, y=60, width=80, height=20) if e is field else dict(x=100, y=20, width=200, height=100)
        with patch('control_lookup.resolve', return_value=field):
            selector = capture._CaptureUIA.anchored_field(uia, field, ancestor)
        self.assertEqual(selector['point'], [.3, .5])
        self.assertEqual(selector['anchor'], dict(role='group', name='Conditions'))
        with patch('control_lookup.resolve', return_value=object()):
            self.assertIsNone(capture._CaptureUIA.anchored_field(uia, field, ancestor))

    def test_clicked_passive_caption_records_its_unique_selection_item(self):
        item = Mock()
        item.property.side_effect = lambda p: {30003: 50007, 30010: True, 30022: False}.get(p)
        item_node = dict(role='listitem', name='Spreadsheet', secure=False, enabled=True,
                         available_actions=['Click', 'Select'], locator=dict(role='listitem', name='Spreadsheet'))
        self.observed['node'] = dict(role='statictext', name='Spreadsheet', secure=False,
                                     enabled=True, available_actions=[])
        self.uia.parent.return_value = item
        self.uia.pid.return_value = self.binding.pid
        self.uia.within.return_value = True
        self.uia.bounds.return_value = dict(x=0, y=0, width=200, height=100)
        self.uia.node.return_value = item_node
        with (patch('control_lookup.resolve', return_value=item),
              patch('provider_visibility.visible', return_value=True)):
            self.worker.capture_action(dict(action='click', x=50, y=50), self.observed)
        recorded, = self.notes.snapshot()
        self.assertEqual(recorded['action'], 'select')
        self.assertEqual(recorded['target']['locator']['name'], 'Spreadsheet')

    def test_passive_caption_is_promoted_before_any_image_row_classification(self):
        item = Mock()
        item.property.side_effect = lambda p: {30003: 50007, 30010: True, 30022: False}.get(p)
        self.observed['node'] = dict(role='statictext', name='Spreadsheet', secure=False,
                                     enabled=True, available_actions=['SetFocus'])
        self.uia.parent.return_value = item
        self.uia.pid.return_value = self.binding.pid
        self.uia.within.return_value = True
        self.uia.bounds.return_value = dict(x=0,y=0,width=100,height=100)
        self.uia.node.return_value = dict(role='listitem', name='Spreadsheet', secure=False,
            enabled=True, available_actions=['Click','Select'], locator=dict(role='listitem',name='Spreadsheet'))
        self.worker.row_tracker = Mock()
        self.worker.row_tracker.before.return_value = None
        event = dict(kind='down',action='click',window=5,scope_proven=True,at=time.monotonic(),x=50,y=50)
        with (patch.object(capture.recorder.CaptureWorker,'observe_down',return_value=self.observed),
              patch('control_lookup.resolve',return_value=item),
              patch('provider_visibility.visible',return_value=True),
              patch('row_vision._row_layout',side_effect=AssertionError('caption became painted row'))):
            resolved = self.worker.observe_down(event)
            self.assertEqual(resolved['node']['role'], 'listitem')
            self.assertNotIn('painted_row', resolved)

    def test_unreadable_value_preserves_known_location_without_guessing(self):
        capture.live_action.text_readback.return_value = dict(value=None, value_evidence=dict(status='unavailable'))
        self.uia.value.return_value = None
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker.finalize_watch(True)
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'teach-target')
        self.assertNotIn('value', final)

    def test_combo_editor_owner_error_prevents_independent_watch(self):
        capture.live_action.textfield_scope.side_effect = capture.bridge.BridgeError('textfield_requires_combo')
        self.worker.capture_action(dict(action='click'), self.observed)
        final, = self.notes.snapshot()
        self.assertEqual(final['recognition']['reason'], 'textfield_requires_combo')
        capture.live_action.text_readback.assert_not_called()
        self.assertIsNone(self.worker.watched)

    def test_known_combo_owner_is_retained_when_its_inner_editor_is_clicked(self):
        owner = dict(element=object(), root=self.root, window=5,
                     node=dict(self.observed['node'], role='combobox', semantic_label='Family',
                               locator=dict(role='combobox', automation_id='family')))
        self.uia.combo_observation.return_value = owner
        self.worker.cache_options = Mock()
        self.worker.capture_action(dict(action='click'), self.observed)
        step, = self.notes.snapshot()
        self.assertEqual(step['target']['locator'], owner['node']['locator'])
        self.assertEqual(step['recognition']['reason'], 'combo_choice_unproven')
        self.assertEqual(capture.default_text(step), '입력받은 변수를 Family 조건에 입력해줘')
        capture.live_action.text_readback.assert_not_called()
        capture.live_action.textfield_scope.assert_not_called()


class ClosedDialogFieldTests(unittest.TestCase):
    setUp = TextfieldWorkerTests.setUp

    def test_watch_keeps_validated_native_focus_even_if_display_node_has_no_handle(self):
        hwnd = 0x81230063
        native_element = Mock()
        native_element.property.return_value = hwnd - 2**32
        self.observed['element'] = native_element
        self.observed['node'].pop('hwnd', None)
        with (patch.object(capture.bridge, 'hwnd_pid', return_value=self.binding.pid),
              patch.object(capture.bridge, 'root_window', return_value=self.observed['window'])):
            self.worker.capture_action(dict(action='click'), self.observed)
        self.assertEqual(self.observed['_recorded_native_focus'], hwnd)

        def read_focus(thread, info):
            info._obj.focus = 0xffffffff00000000 | hwnd
            return True

        events = queue.Queue()
        hook = capture._RowHookPump(self.binding, capture.bridge, events, queue.Queue())
        with (patch.object(capture.bridge, '_sig', return_value=read_focus),
              patch.object(capture.bridge, 'root_window', return_value=self.observed['window']) as root):
            hook._emit({'kind': 'enter'})
        root.assert_called_once_with(hwnd)
        self.assertEqual(events.get_nowait()['submit_focus'], self.observed['_recorded_native_focus'])

    def test_closed_dialog_keeps_only_the_last_pre_transition_observed_value(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        before = self.notes.snapshot()[0]
        now = time.monotonic()
        self.worker.last_key = now - .2
        self.worker.watched_samples.clear()
        self.worker.watched_samples.append((now-.1, 'chosen-folder'))
        self.worker.watched_samples.append((now+.1, 'too-late'))
        capture.live_action.textfield_scope.side_effect = capture.bridge.BridgeError('target_window_mismatch')
        self.worker.freeze_watch(now)
        final, = self.notes.snapshot()
        self.assertEqual(final['id'], before['id'])
        self.assertEqual(final['value'], 'chosen-folder')
        self.assertEqual(final['target'], before['target'])
        self.assertIsNone(self.worker.watched)

    def test_popup_disappearing_between_samples_does_not_replace_verified_value_with_manual(self):
        self.worker.capture_action(dict(action='click'), self.observed)
        self.worker._commit_value('chosen-folder')
        now = time.monotonic()
        self.worker.last_key = now - .2
        self.worker.watched_samples.append((now-.05, 'chosen-folder'))
        capture.live_action.textfield_scope.side_effect = capture.bridge.BridgeError('target_window_mismatch')
        self.worker.finalize_watch(True)
        final, = self.notes.snapshot()
        self.assertEqual(final['action'], 'set-value')
        self.assertEqual(final['value'], 'chosen-folder')


if __name__ == '__main__':
    unittest.main()
