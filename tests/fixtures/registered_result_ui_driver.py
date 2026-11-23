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


if '--_worker' in sys.argv and os.environ.get('DESKTOP_SELECT_SMOKE_UNKNOWN_OFFSCREEN'):
    # WPF peers expose bool IsOffscreen. Inject a missing value ONLY for this
    # fixture's recorded group, leaving its native bounds and every input real.
    import builtins
    original_import = builtins.__import__
    trace = os.environ['DESKTOP_SELECT_SMOKE_UNKNOWN_OFFSCREEN']

    def fixture_import(*args, **kwargs):
        module = original_import(*args, **kwargs)
        bridge = sys.modules.get('win_bridge')
        if bridge is not None and hasattr(bridge, 'Element'):
            builtins.__import__ = original_import
            original_property = bridge.Element.property

            def property_value(element, property_id):
                if property_id == 30022 and original_property(element, 30011) == 'PaintedResults':
                    with open(trace, 'a', encoding='utf8') as stream:
                        stream.write('fixture_group_offscreen_unknown\n')
                    return None
                return original_property(element, property_id)

            bridge.Element.property = property_value
        return module

    builtins.__import__ = fixture_import


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
    origin = layout.get('visibleOrigin', layout['groupOrigin'])
    if dimensions == layout['imageSize']:
        origin = [0, 0]
    else:
        end = layout.get('visibleEnd', layout['groupEnd'])
        assert dimensions == [stop - start for stop, start in zip(end, origin)], (dimensions, layout)

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
    covered_target = False
    if config.get('cover_hwnd'):
        # Cover only this isolated native fixture after selecting the current
        # cell, so production must restore the target before its final check.
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.GetForegroundWindow.restype = wintypes.HWND
        user.ShowWindow(config['cover_hwnd'], 5)
        user.SetForegroundWindow(config['cover_hwnd'])
        covered_target = int(user.GetForegroundWindow() or 0) == config['cover_hwnd']
        assert covered_target, 'fixture_cover_not_foreground'
    Path(config['receipt']).write_text(json.dumps(dict(cropped=origin != [0, 0],
        wrong_value_rejected=True, canvas_drag=True, selected_cell=True,
        crop_origin=origin, image_size=dimensions, expected=config['expected'],
        covered_target=covered_target)), encoding='utf8')
    confirm.invoke()


if '--_worker' in sys.argv and os.environ.get('DESKTOP_SELECT_SMOKE_UI_REVIEW'):
    import tkinter as tk
    original_mainloop = tk.Tk.mainloop
    config = json.loads(os.environ['DESKTOP_SELECT_SMOKE_UI_REVIEW'])

    def fixture_mainloop(root, *args):
        if root.title() == 'Desktop Select 결과 표 확인':
            def run():
                try:
                    drive_review(root, config)
                except Exception as exc:
                    Path(config['receipt']).write_text(repr(exc), encoding='utf8')
                    root.destroy()
            root.after(250, run)
        return original_mainloop(root, *args)

    tk.Tk.mainloop = fixture_mainloop
