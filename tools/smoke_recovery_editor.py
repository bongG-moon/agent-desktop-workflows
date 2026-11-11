"""Run real Tk controls for drafts, natural-language recovery, navigation and save."""
import argparse
import json
from pathlib import Path
import sys
from unittest.mock import Mock, patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skill', type=Path, default=Path(__file__).resolve().parents[1] / 'skills/desktop-select')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.skill.resolve() / 'scripts'))
    import desktop_select
    import teach_capture as capture
    import learned_tasks as tasks
    import tkinter as tk
    from tkinter import ttk

    def combo(key, label, value=None):
        step = {'id': key, 'label': label, 'field_label': label, 'action': 'set-value',
                'target': {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': key}},
                'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'}}
        if value is None:
            step.update(action='teach-target', recognition={'target': 'identified', 'action': 'requires_instruction',
                        'result': 'manual', 'reason': 'combo_choice_unproven'})
        else:
            step['value'] = value
        return step

    raw = [combo('familyField', 'Family'), combo('stateField', 'MES Setup', 'N'),
           capture.manual('capture_target_unavailable', 'semantic'),
           capture.manual('capture_target_unavailable', 'prose'),
           combo('generationField', 'Generation', 'A'),
           {'id': 'query', 'label': '조회', 'action': 'click',
            'target': {'kind': 'element', 'locator': {'role': 'button', 'automation_id': 'query', 'name': '조회'}},
            'recognition': {'target': 'identified', 'action': 'supported', 'result': 'manual'}},
           capture.manual('protected_input_not_recorded', 'remove')]

    class Session:
        def __init__(self, binding, title):
            self.binding, self.notes = binding, capture.CaptureNotes()
            self.started = self.ended = False

        def start(self):
            self.started = True
            for step in raw:
                self.notes.add(step)

        def poll(self):
            pass

        def finish(self, **kwargs):
            self.ended = True
            return {'status': 'recorded', 'steps': self.notes.snapshot(), 'input_dispatched': False}

        def cancel(self):
            self.ended = True
            return {'status': 'cancelled', 'steps': [], 'input_dispatched': False}

    original_tk, errors, checks = tk.Tk, [], []
    freeform = '이번에 요청한 제품군에 맞춰 가운데 조건을 바꿔줘'
    known_prose = '이번에 요청한 세대에 맞춰 선택해줘'

    def create():
        root = original_tk()

        def widgets(parent):
            for child in parent.winfo_children():
                yield child
                yield from widgets(child)

        def named(name):
            return next(w for w in widgets(root) if w.winfo_name() == name)

        def button(text):
            return next(w for w in widgets(root) if isinstance(w, ttk.Button) and w.cget('text') == text)

        def write(text):
            widget = named('instruction')
            widget.delete(0, 'end')
            widget.insert(0, text)

        def later(fn):
            def run():
                try:
                    fn()
                except Exception as exc:
                    errors.append(repr(exc))
                    root.destroy()
            root.after(180, run)

        def begin():
            button('시작').invoke()
            later(review)

        def review():
            button('마치고 검토').invoke()
            later(drafts)

        def drafts():
            assert named('instruction').get() == '입력받은 변수를 Family 조건에 입력해줘'
            assert 'MES Setup' in named('steps').item('stateField')['values'][3]
            assert 'N' in named('steps').item('stateField')['values'][3]
            checks.append('generated_field_and_value_drafts')
            # Skip the observed-value row entirely: Save must persist its draft.
            named('steps').selection_set('semantic')
            later(semantic)

        def semantic():
            assert '대상 이름' in named('instruction').get()
            write('MES Setup 조건을 N으로 설정해줘')
            button('적용하고 다음 단계').invoke()
            later(prose)

        def prose():
            assert named('steps').selection() == ('prose',)
            write(freeform)
            button('적용하고 다음 단계').invoke()
            later(known)

        def known():
            assert named('steps').selection() == ('generationField',)
            write(known_prose)
            button('적용하고 다음 단계').invoke()
            later(command)

        def command():
            assert named('steps').selection() == ('query',)
            assert named('instruction').get() == '조회 버튼을 눌러줘'
            checks.append('apply_next_accepts_unknown_prose')
            named('steps').selection_set('prose')
            later(revisit)

        def revisit():
            assert named('instruction').get() == freeform
            checks.append('draft_survives_navigation')
            named('steps').selection_set('remove')
            later(remove)

        def remove():
            button('선택 단계 삭제').invoke()
            assert 'remove' not in named('steps').get_children()
            checks.append('incorrect_step_deleted')
            later(save)

        def save():
            button('저장').invoke()

        later(begin)
        root.after(15000, root.destroy)
        return root

    with patch.object(tk, 'Tk', side_effect=create), patch.object(capture, 'CaptureSession', Session):
        result = capture.capture(Mock(), title='Local description recovery test')
    assert not errors, errors
    assert result['status'] == 'recorded', result
    plan = tasks.from_recording('recovery test', 'Example.exe', 'Local test', result['steps'])
    tasks._validate(plan)
    steps = plan['steps']
    assert steps[0]['instruction']['parameter'] == 'Family'
    assert steps[1]['instruction']['value'] == 'N'
    assert (steps[2]['match'], steps[2]['target'], steps[2]['instruction']['value']) == ('query', 'MES Setup', 'N')
    assert steps[3]['instruction_draft'] == freeform
    assert (steps[4]['target'], steps[4]['instruction_draft']) == ('generationField', known_prose)
    assert steps[5]['instruction']['text'] == '조회 버튼을 눌러줘'
    checks += ['untouched_default_saved', 'unreadable_combo_variable_saved', 'unknown_target_live_query_saved',
               'freeform_prose_saved_for_host_interpretation', 'known_target_prose_preserves_identity',
               'button_named_instruction_saved']
    report = {'status': 'passed', 'checks': checks, 'steps': len(steps), 'business_input_dispatched': False,
              'interaction': 'native Tk widget callbacks; synthetic recorded events'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': len(checks), 'output': str(args.out)}))


if __name__ == '__main__':
    main()
