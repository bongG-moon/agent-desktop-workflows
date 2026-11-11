"""Exercise real Tk review controls with local synthetic captured locations."""
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

    def combo(key, value=None):
        target = {'kind': 'element', 'locator': {'role': 'combobox', 'automation_id': key}}
        step = {'id': key, 'label': key, 'action': 'set-value', 'target': target,
                'recognition': {'target': 'identified', 'action': 'supported', 'result': 'checkable'}}
        if value is None:
            step.update(action='teach-target', recognition={'target': 'identified', 'action': 'requires_instruction',
                        'result': 'manual', 'reason': 'combo_choice_unproven'})
        else:
            step['value'] = value
        return step

    raw = [combo('stateField'), combo('familyField', 'Hybrid'), capture.manual('capture_target_unavailable', 'bad'),
           {'id': 'query', 'label': 'Query', 'action': 'click',
            'target': {'kind': 'element', 'locator': {'role': 'button', 'automation_id': 'query'}},
            'recognition': {'target': 'identified', 'action': 'supported', 'result': 'manual'}}]

    class Session:
        def __init__(self, binding, title):
            self.binding = binding
            self.notes = capture.CaptureNotes()
            self.started = self.ended = False

        def start(self):
            self.started = True
            for item in raw:
                self.notes.add(item)

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
                except Exception as error:
                    errors.append(repr(error))
                    root.destroy()
            root.after(160, run)

        def begin():
            button('시작').invoke()
            later(review)

        def review():
            button('마치고 검토').invoke()
            later(first)

        def first():
            assert len(named('steps').get_children()) == 4
            write('instruction', 'MES Setup조건을 N으로 설정')
            button('문장의 이름을 이 대상에 연결').invoke()
            assert named('alias').get() == 'MES Setup'
            button('적용하고 다음 단계').invoke()
            later(second)

        def second():
            assert named('steps').selection() == ('familyField',)
            assert 'Hybrid' in named('instruction').get()
            checks.append('observed_action_draft')
            write('instruction', '이 항목을 {Family}로 설정')
            button('적용하고 다음 단계').invoke()
            later(remove)

        def remove():
            assert named('steps').selection() == ('bad',)
            button('선택 단계 삭제').invoke()
            assert len(named('steps').get_children()) == 3
            assert 'bad' not in named('steps').get_children()
            checks.append('delete_unrecognized_step')
            named('steps').selection_set('query')
            later(save)

        def save():
            button('저장').invoke()

        later(begin)
        root.after(15000, root.destroy)
        return root

    with patch.object(tk, 'Tk', side_effect=create), patch.object(capture, 'CaptureSession', Session):
        result = capture.capture(Mock(), title='Local recording editor test')
    if errors:
        raise AssertionError(errors)
    assert result['status'] == 'recorded', result
    plan = tasks.from_recording('editor test', 'Example.exe', 'Local test', result['steps'])
    tasks._validate(plan)
    assert [step['action'] for step in plan['steps']] == ['select', 'select', 'open']
    assert plan['steps'][0]['instruction']['value'] == 'N'
    assert plan['steps'][1]['instruction']['parameter'] == 'Family'
    checks += ['alias_link_to_recorded_identity', 'apply_and_next', 'fixed_value_saved', 'runtime_variable_saved']
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({'status': 'passed', 'checks': checks, 'steps': len(plan['steps']),
                       'business_input_dispatched': False, 'interaction': 'native Tk widget callbacks'}, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'passed', 'checks': len(checks), 'output': str(args.out)}))


if __name__ == '__main__':
    main()
