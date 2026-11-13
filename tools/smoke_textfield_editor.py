"""Exercise native recording review controls with synthetic ordinary input events."""
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
    import desktop_select  # Load the verified bridge for source checkouts.
    import teach_capture as capture
    import learned_tasks as tasks
    import tkinter as tk
    from tkinter import ttk

    def field(key, label, role, value=None):
        step = {'id': key, 'label': label, 'field_label': label, 'action': 'set-value',
                'target': {'kind': 'element', 'locator': {'role': role, 'automation_id': key}},
                'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'}}
        if value is None:
            step.update(action='teach-target', recognition={'target': 'identified', 'action': 'requires_instruction',
                        'result': 'manual', 'reason': 'text_value_unobserved' if role == 'textfield' else 'combo_choice_unproven'})
        else:
            step['value'] = value
        return step

    raw = [field('search', '검색어', 'textfield', '00042'), field('family', 'Family', 'combobox', 'Hybrid'),
           field('comment', '설명', 'textfield'), field('clear', '초기화', 'textfield', 'old'),
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

        def write(name, text):
            widget = named(name)
            widget.delete(0, 'end')
            widget.insert(0, text)

        def later(fn):
            def run():
                try:
                    fn()
                except Exception as exc:
                    errors.append(f'{fn.__name__}: {exc!r}')
                    root.destroy()
            root.after(180, run)

        def begin():
            button('시작').invoke()
            later(review)

        def review():
            button('마치고 검토').invoke()
            later(search)

        def search():
            assert named('instruction').get() == '검색어 입력란을 "00042"로 설정해줘', named('instruction').get()
            assert any(isinstance(w, ttk.Label) and w.cget('text') == '값 지정' for w in widgets(root))
            write('instruction', '입력받은 변수를 검색어 입력란에 입력해줘')
            named('mode').set('필수 변수')
            write('argument', 'Search')
            button('적용하고 다음 단계').invoke()
            later(combo)

        def combo():
            assert named('steps').selection() == ('family',), (named('steps').selection(), named('preview').cget('text'))
            assert named('instruction').get() == 'Family 조건을 Hybrid로 설정해줘'
            checks.append('literal_text_draft_changed_to_runtime_parameter')
            # Leave the combo draft untouched while editing an unrelated field.
            button('적용하고 다음 단계').invoke()
            later(unknown)

        def unknown():
            assert named('steps').selection() == ('comment',), (named('steps').selection(), named('preview').cget('text'))
            assert named('instruction').get() == '입력받은 변수를 설명 입력란에 입력해줘'
            checks.append('unreadable_input_keeps_required_parameter_draft')
            button('적용하고 다음 단계').invoke()
            later(clear)

        def clear():
            assert named('steps').selection() == ('clear',), (named('steps').selection(), named('preview').cget('text'))
            write('instruction', '초기화 입력란을 비워줘')
            named('mode').set('고정값')
            write('argument', '')
            assert '입력란 비우기' in named('preview').cget('text'), named('preview').cget('text')
            button('적용하고 다음 단계').invoke()
            later(remove)

        def remove():
            assert named('steps').selection() == ('remove',)
            button('선택 단계 삭제').invoke()
            assert 'remove' not in named('steps').get_children()
            checks.append('incorrect_step_deleted')
            named('steps').selection_set('search')
            later(revisit)

        def revisit():
            assert named('mode').get() == '필수 변수'
            assert named('argument').get() == 'Search'
            checks.append('parameter_survives_navigation_and_deletion')
            button('저장').invoke()

        later(begin)
        root.after(15000, root.destroy)
        return root

    with patch.object(tk, 'Tk', side_effect=create), patch.object(capture, 'CaptureSession', Session):
        result = capture.capture(Mock(), title='Local text input review test')
    assert not errors, errors
    assert result['status'] == 'recorded', result
    plan = tasks.from_recording('입력란 검토', 'Example.exe', 'Local test', result['steps'])
    tasks._validate(plan)
    steps = plan['steps']
    assert len(steps) == 4
    assert (steps[0]['action'], steps[0]['instruction']['parameter']) == ('fill', 'Search')
    assert (steps[1]['action'], steps[1]['instruction']['value']) == ('select', 'Hybrid')
    assert (steps[2]['action'], steps[2]['instruction']['parameter']) == ('fill', '설명')
    assert (steps[3]['action'], steps[3]['instruction']['value']) == ('fill', '')
    checks += ['saved_fill_parameter', 'unchanged_combo_literal', 'saved_unknown_input_parameter', 'saved_explicit_empty_clear']
    report = {'status': 'passed', 'checks': checks, 'steps': len(steps), 'business_input_dispatched': False,
              'interaction': 'native Tk widget callbacks; synthetic recorded events; no physical recording'}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': len(checks), 'output': str(args.out)}))


if __name__ == '__main__':
    main()
