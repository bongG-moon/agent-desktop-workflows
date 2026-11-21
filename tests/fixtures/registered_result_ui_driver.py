"""Drive a real Tk review using coordinates supplied by the isolated WPF fixture.

The smoke tool installs this as sitecustomize only in the registered runner's
child environment. The installed skill and the test window's input logic are real.
"""
import json
import os
from pathlib import Path
import sys


def reject_image_access(trace):
    """Fail this packaged runner if an element-only trial attempts a capture or loads the review UI."""
    def log(message):
        with open(trace, 'a', encoding='utf8') as stream:
            stream.write(message + '\n')
    def audit(event, arguments):
        if event == 'ctypes.dlsym' and arguments[1] == 'PrintWindow':
            log('capture_attempted')
            raise RuntimeError('readable_fixture_must_not_capture')
        if event == 'import' and arguments[0] == 'local_result_review':
            log('local_review_attempted')
            raise RuntimeError('readable_fixture_must_not_open_review')
    log('worker_guard_enabled' if '--_worker' in sys.argv else 'launcher_guard_enabled')
    sys.addaudithook(audit)


if os.environ.get('DESKTOP_SELECT_SMOKE_ELEMENT_TRACE'):
    reject_image_access(os.environ['DESKTOP_SELECT_SMOKE_ELEMENT_TRACE'])


def drive_review(root, config):
    import tkinter as tk
    from tkinter import ttk

    layout = config['layout']
    descendants = list(root.winfo_children())
    for widget in descendants:
        descendants.extend(widget.winfo_children())
    canvas, = [widget for widget in descendants if isinstance(widget, tk.Canvas)]
    entry, = [widget for widget in descendants if isinstance(widget, ttk.Entry)]
    confirm, = [widget for widget in descendants if isinstance(widget, ttk.Button)
                and widget.cget('text') == '이 행부터 이어서 실행']
    pixels = canvas.itemcget(canvas.find_all()[0], 'image')
    dimensions = [int(root.tk.call('image', axis, pixels)) for axis in ('width', 'height')]
    origin = layout['groupOrigin']
    if dimensions == layout['imageSize']:
        origin = [0, 0]
    else:
        assert dimensions == [end - start for end, start in zip(layout['groupEnd'], origin)], (dimensions, layout)

    def event(kind, key):
        x, y = layout[key]
        canvas.event_generate(kind, x=x-origin[0]-int(canvas.canvasx(0)),
                              y=y-origin[1]-int(canvas.canvasy(0)))

    def click(key):
        event('<ButtonPress-1>', key)
        event('<ButtonRelease-1>', key)

    event('<ButtonPress-1>', 'headerLeft')
    event('<B1-Motion>', 'headerRight')
    event('<ButtonRelease-1>', 'headerRight')
    click('decoy')
    entry.insert(0, 'OTHER')
    assert confirm.instate(['disabled'])
    confirm.invoke()
    assert root.winfo_exists() and confirm.instate(['disabled'])
    click('cell')
    entry.insert(0, config['expected'])
    assert confirm.instate(['!disabled'])
    Path(config['receipt']).write_text(json.dumps(dict(cropped=origin != [0, 0],
        wrong_value_rejected=True, canvas_drag=True, selected_cell=True)), encoding='utf8')
    confirm.invoke()


if '--_worker' in sys.argv and os.environ.get('DESKTOP_SELECT_SMOKE_UI_REVIEW'):
    import tkinter as tk
    original_mainloop = tk.Tk.mainloop
    config = json.loads(os.environ['DESKTOP_SELECT_SMOKE_UI_REVIEW'])

    def fixture_mainloop(root, *args):
        if root.title() == '결과 표에서 행 확인':
            def run():
                try:
                    drive_review(root, config)
                except Exception as exc:
                    Path(config['receipt']).write_text(repr(exc), encoding='utf8')
                    root.destroy()
            root.after(250, run)
        return original_mainloop(root, *args)

    tk.Tk.mainloop = fixture_mainloop
