"""Exercise the Windows recorder's real Tk save button and CLI storage in an isolated project."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch


TITLE = 'Local draft test'
EXE = 'TrainingDesk.exe'
GESTURE = '10초 기다렸다가 안내가 있으면 닫고, 결과 표를 우클릭해서 엑셀로 보내줘'
CHILD = '상세 창의 내용에 맞춰 다음 항목을 골라줘'
SRC = Path(__file__).resolve().parents[1] / 'skills/desktop-select'


class Review:
    """Drive Tk's event queue exactly as a user moving between fields and buttons."""
    def __init__(self, root, ttk):
        self.root, self.ttk = root, ttk

    def widgets(self, parent):
        for child in parent.winfo_children():
            yield child
            yield from self.widgets(child)

    def field(self, name):
        return next(w for w in self.widgets(self.root) if w.winfo_name() == name)

    def button(self, *texts):
        return next(w for w in self.widgets(self.root)
                    if isinstance(w, self.ttk.Button) and str(w.cget('text')) in texts)

    def edit(self, text):
        entry = self.field('instruction')
        entry.delete(0, 'end')
        entry.insert(0, text)

    def save(self):
        return self.button('초안 저장', '저장')


def exercise(skill, project, baseline):
    import tkinter as tk
    from tkinter import ttk
    sys.path.insert(0, str(skill.resolve() / 'scripts'))
    import desktop_select as app
    import teach_capture as capture

    store = project / '.desktop-select/tasks'
    parent = dict(id='search', label='조회', action='click',
                  target=dict(kind='element', locator=dict(role='button', automation_id='search', name='조회')),
                  recognition=dict(target='identified', action='supported', result='manual'))
    gesture = capture.manual('right_click_requires_menu_teaching', 'gesture')
    child = dict(capture.manual('capture_target_unavailable', 'details'),
                 _capture_window=dict(exe='TrainingDetail.exe', title='Details', _openers=['search', 'gesture']))

    class Session:
        def __init__(self, binding, title):
            self.binding, self.started, self.ended = binding, False, False
            self.notes = capture.CaptureNotes()

        def start(self):
            self.started = True

        def poll(self):
            pass

        def finish(self, **_):
            self.ended = True
            return dict(status='recorded', steps=deepcopy([parent, gesture, child]), input_dispatched=False)

        def cancel(self):
            self.ended = True
            return dict(status='cancelled', steps=[], input_dispatched=False)

    tk_window = tk.Tk
    checks = []

    def cli(*argv):
        return app.run(app.parser().parse_args(list(argv)))

    def record(name, actions, *, offline=False):
        failures = []

        def create():
            root = tk_window()
            destroy = root.destroy

            def close():
                # This smoke opens multiple independent CLI editors in one
                # interpreter. Discard the closed interpreter's pending timers.
                for timer in root.tk.call('after', 'info'):
                    root.after_cancel(timer)
                destroy()

            root.destroy = close
            runner = iter(actions(Review(root, ttk)))

            def tick():
                try:
                    next(runner)
                except StopIteration:
                    return
                except Exception as error:
                    failures.append(error)
                    root.destroy()
                    return
                try:
                    root.after(60, tick)
                except tk.TclError:
                    pass  # A successful save closes the Tcl application.

            def expired():
                failures.append(TimeoutError('Recorder did not close after save'))
                root.destroy()

            root.after(12000, expired)
            root.after(60, tick)
            return root
        with patch.object(tk, 'Tk', create):
            if offline:
                with patch.object(app, 'windows', side_effect=AssertionError('Offline review tried to list windows')):
                    report = cli('review', '--task', name)
            else:
                report = cli('record', '--task', name, '--exe', EXE, '--window', TITLE)
        if failures:
            raise failures[0]
        return report

    def fill(editor):
        editor.button('시작').invoke()
        yield
        editor.button('마치고 검토').invoke()
        yield
        table = editor.field('steps')
        assert table.get_children() == ('search', 'gesture', 'details')
        editor.edit('조회 버튼을 눌러줘')
        table.selection_set('gesture')
        yield
        editor.edit(GESTURE)
        table.selection_set('details')
        yield
        editor.edit(CHILD)
        assert editor.field('instruction').get() == CHILD
        if baseline:
            assert 'disabled' in editor.save().state(), editor.save().state()
            checks.append('unresolved_window_disables_draft_save')
        else:
            assert 'disabled' not in editor.save().state(), editor.save().state()

    def remove_and_save(editor):
        yield from fill(editor)
        editor.button('선택 단계 삭제').invoke()
        yield
        assert editor.field('steps').get_children() == ('search', 'gesture')
        assert editor.field('instruction').get() == GESTURE
        editor.save().invoke()

    def save_orphan(editor):
        yield from fill(editor)
        editor.save().invoke()

    def reopen(editor):
        assert editor.field('steps').get_children() == ('search', 'gesture', 'details')
        editor.field('steps').selection_set('details')
        yield
        assert editor.field('instruction').get() == CHILD
        editor.button('선택 단계 삭제').invoke()
        yield
        assert editor.field('instruction').get() == GESTURE
        editor.save().invoke()

    def fault_retry(editor):
        editor.button('시작').invoke()
        yield
        editor.button('마치고 검토').invoke()
        yield
        table = editor.field('steps')
        for step in ('details', 'gesture'):
            table.selection_set(step)
            yield
            editor.button('선택 단계 삭제').invoke()
            yield
        assert table.get_children() == ('search',)
        editor.edit('조회 버튼을 눌러줘')
        with patch('os.rename', side_effect=PermissionError('Synthetic storage fault')) as publish:
            editor.save().invoke()
            publish.assert_called_once()
        yield
        assert table.winfo_exists() and table.get_children() == ('search',)
        assert editor.field('instruction').get() == '조회 버튼을 눌러줘'
        assert not (store / 'Retry save.json').exists()
        checks.append('disk_error_preserves_review')
        editor.save().invoke()

    def save_invalid_fields(editor):
        yield from fill(editor)
        editor.button('선택 단계 삭제').invoke()
        yield
        assert editor.field('instruction').get() == GESTURE
        editor.button('상세 설정').invoke()
        editor.field('alias').insert(0, '범위=미정')
        editor.field('mode').set('필수 변수')
        editor.field('argument').insert(0, '입력값')
        editor.save().invoke()

    def fix_fields(editor):
        editor.field('steps').selection_set('gesture')
        yield
        editor.button('상세 설정').invoke()
        assert editor.field('instruction').get() == GESTURE
        assert editor.field('alias').get() == '범위=미정'
        assert editor.field('mode').get() == '필수 변수'
        assert editor.field('argument').get() == '입력값'
        editor.field('alias').delete(0, 'end')
        editor.field('mode').set('문장 해석')
        editor.save().invoke()

    with ExitStack() as mocks:
        for obj, key, val in [(capture, 'CaptureSession', Session), (app, 'task_directory', lambda: store),
                              (app, 'windows', Mock(return_value=[])), (app, 'pick_window', Mock()),
                              (app.bridge, 'Binding', Mock()), (app, 'guard', Mock())]:
            mocks.enter_context(patch.object(obj, key, val))
        if baseline:
            try:
                record('Before fix', remove_and_save)
            except app.tasks.TaskError as error:
                assert error.code == 'invalid_step_instruction', error.code
                assert not app.tasks.list_tasks(store)
                checks.append('window_closed_then_invalid_step_instruction_no_saved_task')
            else:
                raise AssertionError('Expected 0.4.15 conversion failure')
        else:
            result = record('After delete', remove_and_save)
            loaded = app.tasks.load_task('After delete', store)
            assert result['status'] == 'recorded' and len(loaded['steps']) == 2
            guarded = loaded['steps'][1]
            assert guarded['instruction_draft'] == GESTURE and not app.tasks.is_recoverable(guarded)
            checks.append('after_delete_store_guarded_sentence_without_escalating_to_action')
            result = record('Recover me', save_orphan)
            document = app.tasks.read_document('Recover me', store)
            assert result['input_dispatched'] is False and document['kind'] == 'recording-review', result
            assert document['rows'][1]['form']['text'] == GESTURE, document
            listed = cli('tasks')
            assert 'Recover me' in listed['tasks'], listed
            summary = cli('show', '--task', 'Recover me')
            assert summary['task']['steps'][2]['text'] == CHILD, summary
            blocked = cli('replay', '--task', 'Recover me', '--exe', EXE, '--window', TITLE)
            assert blocked['input_dispatched'] is False and blocked['status'] == 'needs_review', blocked
            assert blocked['diagnostic'] == 'recording_draft_requires_review', blocked
            checks.append('orphan_step_saved_with_text_and_no_replay')
            result = record('Recover me', reopen)
            assert result['status'] == 'recorded', result
            loaded = app.tasks.load_task('Recover me', store)
            assert len(loaded['steps']) == 2 and loaded['steps'][1]['instruction_draft'] == GESTURE, loaded
            checks.append('reopen_original_name_preserve_sentences_delete_and_finalize')
            result = record('Retry save', fault_retry)
            assert result['status'] == 'recorded' and len(app.tasks.load_task('Retry save', store)['steps']) == 1, result
            assert 'disk_error_preserves_review' in checks
            checks.append('retry_atomically_publishes')
            result = record('Explicit fields', save_invalid_fields)
            assert result['draft'] and result['step_number'] == 2, result
            result = record('Explicit fields', fix_fields, offline=True)
            assert result['status'] == 'recorded' and 'draft' not in result, result
            saved = app.tasks.load_task('Explicit fields', store)
            assert saved['steps'][1]['instruction_draft'] == GESTURE, saved
            checks.append('offline_review_restores_text_alias_variable_and_finalizes')
    return checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill', type=Path, default=SRC)
    parser.add_argument('--baseline', action='store_true', help='Expect the two failures in 0.4.15.')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='desktop-select-draft-smoke-') as project:
        print(json.dumps(dict(checks=exercise(args.skill, Path(project), args.baseline), input_dispatched=False)))
