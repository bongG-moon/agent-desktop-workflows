"""Exercise the installed skill on an owner-drawn WinForms region with no exposed cells."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from smoke_native import Fixture


ROOT = Path(__file__).resolve().parents[1]


def exercise(skill, work):
    sys.path.insert(0, str(skill / 'scripts'))
    import desktop_select as desktop
    import context_menu
    from row_action import foreground

    work.mkdir()
    exe = work / 'NativeFixture.exe'
    net = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319'
    subprocess.run([str(net / 'csc.exe'), '/nologo', '/target:exe', '/out:' + str(exe),
                    '/r:System.Web.Extensions.dll', '/r:System.Drawing.dll', '/r:System.Windows.Forms.dll',
                    str(ROOT / 'tests/fixtures/NativeFixture.cs')], check=True)
    desktop.bridge.set_dpi_awareness()
    fixture = Fixture(exe)
    try:
        binding = desktop.bridge.Binding(dict(pid=fixture.proc.pid, hwnd=fixture.ready['hwnd'], exe_name=exe.name))
        def guard():
            desktop.guard(binding, binding, 'Workflow Native Synthetic Fixture', True)
        foreground(binding)
        with desktop.bridge.UIA() as uia:
            nodes, elements = desktop.observe(binding, uia, identities_only=True)
            region, = [element for node, element in zip(nodes, elements)
                       if node.get('automation_id') == 'ResultRegion']
            assert not list(uia.children(region)), 'fixture must hide its cells'
            bounds = uia.bounds(region)
        point = [bounds['x'] + bounds['width']//2, bounds['y'] + bounds['height']//2]
        spec = context_menu.teach_at(binding, point, ['Export', 'Export to Excel'])
        missing = dict(spec,path=['Nonexistent parent','Export to Excel'])
        failed = context_menu.execute(binding, missing, guard)
        state = fixture.command('state')
        assert (failed['leaf_dispatched'] is False and failed['status'] == 'blocked' and
                state['context_count'] == 1 and state['export_count'] == 0), (failed,state['export_count'])
        fixture.command('close-menu')
        fixture.command('reset')
        # Moving the form makes replay recalculate the screen point from the recorded region.
        fixture.command('move')
        receipt = context_menu.execute(binding, spec, guard)
        assert receipt['leaf_dispatched'] is True and receipt['menu_path_completed'] is True, receipt
        deadline = time.monotonic() + 3
        while True:
            state = fixture.command('state')
            if state['export_count'] or time.monotonic() >= deadline:
                break
            time.sleep(.08)
        assert state['context_count'] == state['export_count'] == 1 and not state['menu_open']
        print('WinForms: missing path stopped; moved owner-drawn region received one right-click and one export.')
    finally:
        fixture.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill-dir', type=Path, default=ROOT / 'skills/desktop-select')
    parser.add_argument('--work-dir', required=True, type=Path, help='new absolute directory')
    args = parser.parse_args()
    if os.name != 'nt' or not args.work_dir.is_absolute() or args.work_dir.exists():
        parser.error('Windows and a new absolute work directory are required')
    exercise(args.skill_dir.resolve(), args.work_dir)
