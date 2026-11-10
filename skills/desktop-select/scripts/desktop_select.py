"""One request, one live target. No recorder, desktop driver or saved handles."""
from __future__ import annotations

import argparse
from collections import deque
import ctypes as C
from ctypes import wintypes as W
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def load_bridge():
    here = Path(__file__).resolve()
    path = here.with_name('win_bridge.py')
    if (not path.exists() and here.parents[2].name == 'skills'
            and (here.parents[3] / 'engine/bridge-manifest.json').is_file()):
        path = here.parents[3] / 'scripts' / 'win_bridge.py'  # source checkout
    # Use only the reviewed bridge, even if the skill was partially copied
    # into a project containing an unrelated scripts directory.
    if not path.is_file() or sha256(path.read_bytes()).hexdigest() != 'a1d94dff366248bca7d4d8618c8258b7e10138df528bd34973dcb665501bdc72':
        raise RuntimeError('skill_incomplete')
    existing = sys.modules.get('win_bridge')
    if existing is not None and Path(existing.__file__).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location('win_bridge', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules['win_bridge'] = module
    return module


bridge = load_bridge()
from targeting import SelectionError, field_labels, resolve, scope_map
from live_action import execute


def windows(exe):
    """Enumerate top-level windows. Only obtain captions from the requested app."""
    bridge._windows()
    if not exe or Path(exe).name != exe or not exe.casefold().endswith('.exe'):
        raise SelectionError('exact_exe_name_required')
    records, truncated = [], False
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    def visit(hwnd, _):
        nonlocal truncated
        if not bridge.user32.IsWindowVisible(hwnd):
            return True
        pid = bridge.hwnd_pid(hwnd)
        try:
            actual, started = bridge._process_identity(pid)
        except (bridge.BridgeError, OSError):
            return True
        if actual.casefold() == exe.casefold():
            records.append({'exe_name': actual, 'pid': pid, 'hwnd': int(hwnd),
                            'title': bridge.window_text(hwnd), 'process_started_utc_ticks': started})
            if len(records) >= 256:
                truncated = True
                return False
        return True

    enum = bridge._sig(bridge.user32, 'EnumWindows', W.BOOL, callback_type, W.LPARAM)
    if not enum(callback_type(visit), 0) or truncated:
        raise SelectionError('window_list_incomplete')
    return records


def pick_window(records, title, hwnd):
    if not title.strip():
        raise SelectionError('exact_business_window_title_required')
    matches = [r for r in records if r['title'] == title and (hwnd is None or r['hwnd'] == hwnd)]
    if len(matches) != 1:
        raise SelectionError('ambiguous_window' if matches else 'window_not_found')
    return matches[0]


def guard(owner, surface, title, mutate=False):
    owner.check()
    surface.check()
    if bridge.window_text(owner.hwnd) != title or not bridge.user32.IsWindowVisible(owner.hwnd):
        raise SelectionError('business_window_changed')
    if owner.hwnd != surface.hwnd and (not bridge.owned(surface.hwnd, owner.hwnd)
            or not bridge.user32.IsWindowVisible(surface.hwnd)):
        raise SelectionError('popup_not_owned_or_closed')
    enabled = bridge._sig(bridge.user32, 'IsWindowEnabled', W.BOOL, W.HWND)
    if mutate and not enabled(surface.hwnd):
        raise SelectionError('window_disabled')


def owned_popups(owner):
    return [{'hwnd': h, 'title': bridge.window_text(h), 'class_name': bridge.window_class(h)}
            for h in owner.scoped_windows() if h != owner.hwnd and bridge.owned(h, owner.hwnd)]


def bind_surface(owner, selected):
    if selected is None:
        return owner
    if not any(p['hwnd'] == selected for p in owned_popups(owner)):
        raise SelectionError('popup_not_owned_or_closed')
    return bridge.Binding({'pid': owner.pid, 'exe_name': owner.exe, 'hwnd': selected,
                           'process_started_utc_ticks': owner.created})


def observe(binding, uia):
    """Read structural properties only. Never read values of unrelated fields/rows."""
    binding.check()
    root = uia.from_handle(binding.hwnd)
    if root is None or uia.pid(root) != binding.pid:
        raise SelectionError('root_unavailable')
    nodes, elements = [], []
    queue = deque([(root, '0', '', 0)])
    while queue:
        element, path, parent, depth = queue.popleft()
        if len(nodes) >= 2500 or depth > 18 or uia.pid(element) != binding.pid:
            raise SelectionError('incomplete_tree')
        # A protected subtree has no usable targets and leaks no labels.
        secure = uia.secure(element)
        node = {'path': path, 'parent_path': parent, 'secure': secure}
        if not secure:
            basic = uia.basic(element)
            bounds = uia.bounds(element)
            valid = bounds['width'] > 0 and bounds['height'] > 0
            node.update(basic, class_name=element.property(30012) or '', bounds=bounds, bounds_valid=valid,
                        enabled=element.property(30010) is True, offscreen=element.property(30022) is not False or not valid)
            if basic['role'] in {'combobox', 'textfield', 'checkbox', 'radiobutton', 'spinner'}:
                label = uia.related_element(element, 30018)
                if label is not None:
                    if (uia.pid(label) != binding.pid or not uia.within(label, root) or uia.secure(label)
                            or label.property(30022) is not False):
                        node['label_source'] = 'invalid_labeled_by'
                    else:
                        node.update(semantic_label=label.property(30005) or '', label_source='labeled_by')
            for i, child in enumerate(uia.children(element)):
                queue.append((child, path + '/' + str(i), path, depth + 1))
        elements.append(element)
        nodes.append(node)
    binding.check()
    return nodes, elements


LIST_ROLES = {'button', 'combobox', 'menuitem', 'treeitem', 'tab', 'table', 'toolbar', 'textfield', 'checkbox'}


def describe(node, regions, label):
    result = {k: node[k] for k in ('role', 'automation_id', 'name', 'enabled', 'offscreen')
              if node.get(k) not in (None, '')}
    result.update(label)
    result['regions'] = regions
    return result


def run(args):
    bridge.set_dpi_awareness()
    rows = windows(args.exe)
    if args.command == 'windows':
        return {'status': 'observed', 'windows': rows, 'input_dispatched': False}
    owner = bridge.Binding(pick_window(rows, args.window, args.hwnd))
    surface = bind_surface(owner, args.popup)
    mutate = args.command in {'select', 'open'}
    check = lambda: guard(owner, surface, args.window, mutate)
    check()
    with bridge.UIA() as uia:
        nodes, elements = observe(surface, uia)
        grouped, labels = scope_map(nodes), field_labels(nodes)
        if args.command == 'inspect' and not args.target:
            items = [describe(n, grouped[i], labels.get(i, {})) for i, n in enumerate(nodes) if not n['secure']
                     and n.get('role') in LIST_ROLES and (n.get('automation_id') or n.get('name') or i in labels)]
            return {'status': 'observed', 'targets': items, 'owned_popups': owned_popups(owner), 'input_dispatched': False}
        role = 'combobox' if args.command == 'select' else args.role
        def locate(tree):
            return resolve(tree, args.target, role=role, within=args.within)
        i, evidence = locate(nodes)
        element, node = elements[i], nodes[i]
        detail = describe(node, grouped[i], labels.get(i, {}))
        if args.command == 'inspect':
            result = {'status': 'resolved', 'target': detail, 'matched_by': evidence,
                      'actions': uia.actions(element), 'input_dispatched': False}
            if node['role'] == 'combobox':
                from live_action import combo_readback, option_inventory
                try:
                    result.update(combo_readback(surface, uia, element))
                except bridge.BridgeError:
                    result.update(value=None, value_evidence={'status': 'unavailable', 'source': 'none'})
                try:
                    result.update(option_inventory(surface, uia, element))
                except bridge.BridgeError:
                    result.update(options=[], options_status='unavailable')
            return result

        def still_valid():
            check()
            current, live = observe(surface, uia)
            j, basis = locate(current)
            if basis != evidence or not uia.same(element, live[j]):
                raise SelectionError('target_changed')
            check()

        command = 'select' if args.command == 'select' else ('expand' if args.expand else
                  'choose' if node['role'] in {'treeitem', 'tab', 'listitem'} else 'invoke')
        result = execute(surface, uia, element, node, command, getattr(args, 'value', None), still_valid=still_valid)
        return dict(result, target=detail, matched_by=evidence, business_commit='not_verified')


def parser():
    p = argparse.ArgumentParser(description='Select an exact field or command in one Windows business window.')
    commands = p.add_subparsers(dest='command', required=True)
    for verb in ('windows', 'inspect', 'select', 'open'):
        s = commands.add_parser(verb)
        s.add_argument('--exe', required=True, help='exact executable basename')
        if verb != 'windows':
            s.add_argument('--window', required=True, help='exact business window title from windows')
            s.add_argument('--hwnd', type=int, help='disambiguate windows with identical titles')
            s.add_argument('--popup', type=int, help='owned popup from current inspect output')
            s.add_argument('--target', required=verb != 'inspect', help='field label, name or automation ID')
            s.add_argument('--within', help='observed region, ancestor ID or name')
        if verb in ('inspect', 'open'):
            s.add_argument('--role', help='e.g. combobox, button, menuitem, treeitem')
        if verb == 'select':
            s.add_argument('--value', required=True, help='exact option text/code')
        if verb == 'open':
            s.add_argument('--expand', action='store_true', help='expand a submenu/tree item instead of invoking it')
    return p


def worker(args):
    try:
        result = run(args)
    except (SelectionError, bridge.BridgeError) as error:
        dispatched = getattr(error, 'input_dispatched', False)
        result = {'status': 'blocked' if dispatched is False else 'uncertain', 'diagnostic': error.code,
                  'input_dispatched': dispatched}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('verified', 'resolved', 'observed') else (2 if result['status'] == 'blocked' else 3)


def supervise(args, argv):
    # A third-party accessibility provider can hang inside a COM call. Terminate
    # our helper only; never assume a timed-out mutation failed or repeat it.
    mutating = args.command in ('open', 'select')
    try:
        child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                                '_worker', *argv], capture_output=True, encoding='utf-8', timeout=90)
        receipt = json.loads(child.stdout)
        if not isinstance(receipt, dict) or 'status' not in receipt or child.returncode not in (0, 2, 3):
            raise ValueError()
        print(json.dumps(receipt, ensure_ascii=False))
        return child.returncode
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        diagnostic = 'provider_timeout_or_interrupted'
    except (OSError, ValueError):
        diagnostic = 'provider_unavailable'
    print(json.dumps({'status': 'uncertain' if mutating else 'blocked', 'diagnostic': diagnostic,
                      'input_dispatched': None if mutating else False}))
    return 3 if mutating else 2


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    argv = sys.argv[1:]
    is_worker = argv[:1] == ['_worker']
    if is_worker:
        argv = argv[1:]
    args = parser().parse_args(argv)
    if args.command == 'inspect' and not args.target and (args.within or args.role):
        print(json.dumps({'status': 'blocked', 'diagnostic': 'specify_target_with_filter', 'input_dispatched': False}))
        return 2
    return worker(args) if is_worker else supervise(args, argv)


if __name__ == '__main__':
    raise SystemExit(main())
