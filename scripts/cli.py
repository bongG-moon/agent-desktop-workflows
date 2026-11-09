"""File-based CLI for an independent local workflow application."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
import uuid

import desktop
from storage import Store, StoreError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError('invalid_arguments')


def _safe(run=None, *, status=None, diagnostic=None):
    run = run or {}
    reported_status = status or run.get('status', 'unknown')
    allowed_statuses = {'unknown', 'written', 'imported', 'recorded_needs_review', 'completed',
                        'completed_unverified', 'verified', 'running', 'needs_review', 'cancelled',
                        'interrupted', 'blocked'}
    result = {'product': 'Agent Desktop Workflows', 'version': '0.1.5',
              'status': reported_status if reported_status in allowed_statuses else 'unknown',
              'model_calls': 0, 'network_requests_by_wrapper': 0}
    for source, target in [('id', 'run_id'), ('run_id', 'run_id'), ('workflow_id', 'workflow_id')]:
        value = run.get(source)
        if isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value):
            result[target] = value
    for key in ('task_verified', 'input_dispatched'):
        if type(run.get(key)) is bool or key in run and run[key] is None:
            result[key] = run[key]
    if diagnostic:
        result['diagnostic'] = diagnostic if re.fullmatch('[a-z_]{1,80}', diagnostic) else 'local_operation_failed'
    return result


def workflow_id(store, name):
    if re.fullmatch('[a-f0-9]{32}', name):
        return name
    matches = [item for item in store.list_workflows() if item.get('name') == name]
    if len(matches) != 1:
        raise ValueError('workflow_name_not_unique')
    return matches[0]['id']


def output_path(store, value):
    """Explicit query exports can be refreshed, never application state or code."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('absolute_local_path_required')
    desktop.local_dir(path.parent)
    canonical = path.resolve(strict=False)
    if any(part.casefold() in {'.git', '.claude', '.codex', '.aws', '.ssh'} for part in canonical.parts):
        raise ValueError('output_reserved_location')
    roots = [Path(store.root), desktop.default_store()]
    if any(canonical.is_relative_to(root.resolve(strict=False)) for root in roots):
        raise ValueError('output_inside_workflow_store')
    # Also protect another recognized workflow store selected in an earlier command.
    for ancestor in canonical.parents:
        if all((ancestor / name).is_dir() for name in ('workflows', 'recordings', 'runs', 'locks')):
            raise ValueError('output_inside_workflow_store')
    package = Path(__file__).resolve().parents[1]
    if canonical.is_relative_to(package):
        relative = canonical.relative_to(package)
        reserved_dirs = {'scripts', 'engine', 'bin', 'tools', 'tests', 'references', '.github'}
        reserved_files = {'license', 'notice.txt', 'third_party_notices.txt', 'upstream.json',
                          'readme.md', 'skill.md', 'start.cmd', '사용가이드.html',
                          'package-manifest.json', '.gitignore', '.gitattributes'}
        if (not relative.parts or relative.parts[0].casefold() in reserved_dirs
                or str(relative).casefold() in reserved_files):
            raise ValueError('output_overwrites_product_file')
    # Pass the original path so write_json can still reject redirected components.
    return path


def main(argv=None):
    parser = Parser(description='Local recording, repair and workflow replay. Read the bundled guide for setup.')
    sub = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
    ui = sub.add_parser('ui')
    ui.add_argument('--store')
    ui.add_argument('--demo', action='store_true')
    for name in ('list', 'show', 'record', 'run', 'resume', 'repair-resume', 'windows', 'status', 'import'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--store')
        if name in ('list', 'show', 'windows', 'status'):
            cmd.add_argument('--output', required=True)
        if name in ('show', 'run'):
            cmd.add_argument('--workflow', required=True)
        if name in ('resume', 'repair-resume', 'status'):
            cmd.add_argument('--run', required=True)
        if name == 'import':
            cmd.add_argument('--file', required=True)
        if name in ('record', 'run', 'resume', 'repair-resume'):
            cmd.add_argument('--pid', type=int, required=True)
            cmd.add_argument('--hwnd', type=int, required=True)
            cmd.add_argument('--exe-name', required=True)
        if name == 'run':
            cmd.add_argument('--parameters-file')
        if name == 'resume':
            cmd.add_argument('--confirm-step')
    try:
        args = parser.parse_args(argv)
        if args.command == 'ui':
            from ui import launch
            launch(store_path=args.store, demo=args.demo)
            return 0
        from workflows import Runner
        store = Store(Path(args.store) if args.store else None)
        if args.command in ('list', 'show', 'windows', 'status'):
            destination = output_path(store, args.output)
            if args.command == 'list':
                data = store.list_workflows()
            elif args.command == 'show':
                data = store.load(workflow_id(store, args.workflow))
            elif args.command == 'windows':
                data = desktop.inventory(Path(store.root) / 'observations')
            else:
                data = store.load_run(args.run)
            desktop.write_json(destination, data, replace=True)
            result = _safe(status='written')
            if isinstance(data, list):
                result['count'] = len(data)
        elif args.command == 'import':
            saved = store.import_recording(Path(args.file))
            result = _safe({'workflow_id': saved['id']}, status='imported')
        else:
            binding = {'pid': args.pid, 'hwnd': args.hwnd, 'exe_name': args.exe_name}
            if args.command == 'record':
                root = Path(store.root) / 'capture'
                recorded = desktop.record(binding, root)
                saved = store.import_recording(recorded)
                result = _safe({'workflow_id': saved['id']}, status='recorded_needs_review')
            else:
                runner = Runner(store, desktop.DesktopAdapter)
                if args.command == 'run':
                    parameters = desktop.read_json(args.parameters_file) if args.parameters_file else None
                    run = runner.start(workflow_id(store, args.workflow), binding, parameters=parameters)
                elif args.command == 'repair-resume':
                    run = runner.fork_repaired(args.run, binding)
                else:
                    run = runner.resume(args.run, binding, confirm_step_id=args.confirm_step)
                result = _safe(run)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0 if result['status'] in {'written', 'imported', 'recorded_needs_review', 'completed', 'completed_unverified', 'verified'} else 2
    except (Exception, KeyboardInterrupt) as error:
        diagnostic = str(error) if isinstance(error, (ValueError, desktop.EngineError, StoreError)) else 'local_operation_failed'
        result = _safe(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'blocked', diagnostic=diagnostic)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
