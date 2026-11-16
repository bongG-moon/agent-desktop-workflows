"""Demonstrate a context menu on a Windows fixture, then replay the recorder's own task."""
import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

from smoke_desktop_context import ROOT, compile_fixture, decoded_cells
from smoke_native import Fixture


def exercise(skill, work):
    sys.path.insert(0, str(skill / 'scripts'))
    import desktop_select as app
    import context_menu as menu
    import row_action
    import teach_capture

    win = app.bridge
    win.set_dpi_awareness()
    exe = compile_fixture(work)
    path = ['Export to excel', 'Active sheet']
    workbook = work / 'result.xlsx'
    fixture = Fixture(exe, str(workbook), '--group')
    session = None
    try:
        observed = fixture.command('reference-ready')
        binding = win.Binding(app.pick_window(app.windows(exe.name), observed['title'], observed['hwnd']))
        row_action.foreground(binding)
        x, y, w, h = observed['grid_bounds']
        point = (x + w // 3, y + h // 2)
        spec = menu.teach_at(binding, point, path)
        session = teach_capture.CaptureSession(binding, observed['title'])
        session.start()
        # Real button events go through the hook. The demonstration pointer
        # pauses on each actual item, as when a person reads its caption.
        def click(point, kind, check):
            check()
            win.user32.SetCursorPos(*point)
            time.sleep(.6)
            check()
            assert win.cursor() == point, 'pointer_moved'
            assert win._send_point(*point, kind)['ok']
            time.sleep(.45)

        with win.UIA() as uia:
            region = menu._region(binding, uia, spec['target'])
            snapshot = menu._menus(binding, uia)
            prior = set(menu._scoped_windows(binding))
            click(point, 'right-click', lambda: menu._hit_region(binding, uia, region, point))
            parent, anchor = None, (*point, 1, 1)
            gap = max(6, round(12 * binding.info()['dpi'] / 96))
            for name in path:
                deadline = time.monotonic() + 3
                while True:
                    items = menu._menus(binding, uia)
                    item = menu._choose(uia, items, snapshot, name, anchor, parent, gap)
                    if item:
                        break
                    assert time.monotonic() < deadline, (name, [i.name for i in items], fixture.command('state'))
                    time.sleep(.05)
                if name == path[-1]:
                    click(item.center, 'click', lambda: menu._hit_item(binding, uia, item, parent, prior, gap))
                else:
                    menu._hit_item(binding, uia, item, parent, prior, gap)
                    win.user32.SetCursorPos(*item.center)
                    time.sleep(.7)
                prior, snapshot, parent, anchor = {i.surface for i in items}, items, item, item.box
        time.sleep(.4)
        raw = session.finish()['steps']
        task = app.tasks.from_recording('Ordinary export', exe.name, observed['title'], raw)
        assert len(task['steps']) == 1 and task['steps'][0]['action'] == 'result-menu', [
            (s['action'], s.get('reason'), s.get('menu_anchor')) for s in task['steps']]
        recorded = task['steps'][0]['context']['menu']
        assert recorded == spec, (recorded, spec)
        after = fixture.command('state')
        assert (after['right_clicks'], after['leaves'], after['unrelated']) == (1, 1, 0), after
        reference_cells = decoded_cells(workbook)
        workbook.rename(work / 'demonstrated.xlsx')
        app.tasks.save_task(task, work)
        persisted = app.tasks.load_task(task['name'], work)
        result = menu.execute(binding, persisted['steps'][0]['context']['menu'], binding.check)
        assert result.get('menu_path_completed'), result
        deadline = time.monotonic() + 3
        while True:
            after = fixture.command('state')
            if (after['right_clicks'], after['leaves'], after['unrelated']) == (2, 2, 0):
                break
            assert time.monotonic() < deadline, after
            time.sleep(.05)
        assert decoded_cells(workbook) == reference_cells
        print('Ordinary mouse recording saved one group-backed nested menu; replay reached the same leaf once.')
    finally:
        if session is not None and not session.ended:
            session.cancel()
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    args = parser.parse_args()
    with TemporaryDirectory(prefix='ordinary-context-') as folder:
        exercise(args.skill_dir.resolve(), Path(folder))
