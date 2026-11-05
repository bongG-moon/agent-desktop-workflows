"""Local desktop adapter. No dependency on another installed automation system."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
import observe
import interact
from local_core import EngineError, validate_local_directory

BRIDGE = ROOT / 'scripts' / 'py_helper.py'
BRIDGE_SOURCES = ('scripts/py_helper.py', 'scripts/win_bridge.py', 'scripts/recorder.py')
MAX_JSON = 16 * 1024 * 1024
ALIASES = {'edit': 'textfield', 'pane': 'group', 'listitem': 'listitem',
           'listbox': 'list', 'option': 'listitem',
           'menu item': 'menuitem', 'combo box': 'combobox', 'check box': 'checkbox'}


def local_dir(path, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise EngineError('absolute_local_path_required')
    if create and not path.exists():
        # Validate the nearest existing ancestor before creating anything.
        parent = path.parent
        while not parent.exists():
            parent = parent.parent
        validate_local_directory(parent)
        path.mkdir(parents=True, exist_ok=True)
    return validate_local_directory(path)


def read_json(path):
    path = Path(path)
    local_dir(path.parent)
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or info.st_size > MAX_JSON:
        raise EngineError('invalid_local_file')
    return json.loads(path.read_text(encoding='utf-8-sig'),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_json')))


def write_json(path, value, *, replace=False):
    path = Path(path)
    local_dir(path.parent)
    if ':' in path.name:
        raise EngineError('invalid_output_file')
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    if len(data.encode('utf-8')) > MAX_JSON:
        raise EngineError('output_too_large')
    def validate_destination():
        local_dir(path.parent)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or not path.is_file() or info.st_nlink > 1:
            raise EngineError('invalid_output_file')
    validate_destination()
    if not replace:
        with path.open('x', encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temporary = path.with_name('.output-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        validate_destination()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bridge_script():
    manifest = read_json(ROOT / 'engine' / 'bridge-manifest.json')
    if manifest.get('version') != '0.1.4' or set(manifest.get('files', {})) != set(BRIDGE_SOURCES):
        raise EngineError('bridge_manifest_invalid')
    for name in BRIDGE_SOURCES:
        path = ROOT / name
        local_dir(path.parent)
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or not path.is_file():
            raise EngineError('bridge_source_redirected')
        if manifest['files'][name] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise EngineError('bridge_source_hash_mismatch')
    return BRIDGE


def _native(command, payload, run_dir, *, timeout=15, on_progress=None, stop_event=None):
    if command not in {'windows', 'inspect', 'probe', 'point', 'activate', 'record'}:
        raise EngineError('native_command_not_allowed')
    run = local_dir(Path(run_dir) / ('native-' + uuid.uuid4().hex), True)
    request, response = run / 'request.json', run / 'response.json'
    write_json(request, payload)
    environment = observe.child_environment(run)
    # Isolated Python loads only the bundled transparent source. No compiler or
    # additional executable is generated, downloaded, or used as a fallback.
    proc = subprocess.Popen([sys.executable, '-I', '-X', 'utf8', '-B', str(_bridge_script()), command, '--request', str(request),
                             '--response', str(response)], cwd=run, env=environment,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    started, cancel_at, last_progress = time.monotonic(), None, None
    try:
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set() and cancel_at is None:
                cancel_at = time.monotonic()
                if command == 'record' and payload.get('stop_path'):
                    Path(payload['stop_path']).touch(exist_ok=True)
            if time.monotonic() - started > timeout or (cancel_at and time.monotonic() - cancel_at > 8):
                raise EngineError('native_timeout')
            if on_progress and payload.get('progress_path'):
                try:
                    progress = read_json(payload['progress_path'])
                    signature = json.dumps(progress, sort_keys=True, ensure_ascii=True)
                    if signature != last_progress:
                        on_progress(progress)
                        last_progress = signature
                except (OSError, ValueError, EngineError):
                    pass
            time.sleep(0.10)
        if not response.is_file():
            raise EngineError('native_response_missing')
        value = read_json(response)
        if not isinstance(value, dict) or type(value.get('ok')) is not bool:
            raise EngineError('native_response_invalid')
        return value
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=3)


def default_store():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'AgentDesktopWorkflows'


def inventory(work_dir=None):
    root = local_dir(work_dir or default_store() / 'observations', True)
    result = _native('windows', {}, root)
    if result.get('ok') is not True or not isinstance(result.get('data'), list):
        raise EngineError('window_inventory_unavailable')
    return result['data']


def _binding(binding):
    if (not isinstance(binding, dict) or type(binding.get('pid')) is not int
            or type(binding.get('hwnd')) is not int or binding['pid'] <= 0 or binding['hwnd'] <= 0):
        raise EngineError('invalid_target_binding')
    return {'pid': binding['pid'], 'hwnd': binding['hwnd'],
            'exe_name': observe.exe_name(binding.get('exe_name'))}


def inspect(binding, run_dir):
    value = _native('inspect', _binding(binding), local_dir(run_dir, True))
    data = value.get('data')
    if value.get('ok') is not True or not isinstance(data, dict) or not isinstance(data.get('nodes'), list):
        raise EngineError('target_observation_unavailable')
    if data.get('pid') != binding['pid'] or data.get('hwnd') != binding['hwnd']:
        raise EngineError('target_observation_mismatch')
    return data


def record(binding, output_dir, on_progress=None, stop_event=None, auto_start=False):
    output = local_dir(output_dir, True)
    capture = local_dir(output / ('record-' + uuid.uuid4().hex), True)
    payload = dict(_binding(binding), output_dir=str(capture),
                   progress_path=str(capture / 'progress.json'), draft_path=str(capture / 'draft.json'),
                   stop_path=str(capture / 'stop.request'), auto_start=bool(auto_start))
    result = _native('record', payload, capture, timeout=1800,
                     on_progress=on_progress, stop_event=stop_event)
    draft = capture / 'draft.json'
    if result.get('ok') is not True or not draft.is_file():
        raise EngineError('recording_not_saved')
    read_json(draft)
    return draft


def role(value):
    value = str(value or '').replace('ControlType.', '').lower()
    return ALIASES.get(value, value)


def node_id(node):
    native = node.get('native_id')
    if isinstance(native, dict) and native.get('kind') == 'automation_id':
        return native.get('value')
    return node.get('automation_id')


def _matches(node, selector):
    checks = []
    if selector.get('automation_id'):
        checks.append(node_id(node) == selector['automation_id'])
    if selector.get('name'):
        checks.append(node.get('name') == selector['name'])
    if selector.get('role'):
        checks.append(role(node.get('role')) == role(selector['role']))
    return bool(checks) and all(checks)


def resolve(nodes, locator):
    if not isinstance(locator, dict) or not (locator.get('automation_id') or locator.get('name')):
        raise EngineError('target_locator_incomplete')
    by_path = {str(n.get('path')): n for n in nodes if isinstance(n, dict)}
    matches = []
    for node in nodes:
        if not isinstance(node, dict) or not _matches(node, locator):
            continue
        required = list(locator.get('ancestors') or [])
        parent = node.get('parent_path')
        seen = set()
        while required and parent is not None and str(parent) in by_path and str(parent) not in seen:
            seen.add(str(parent))
            ancestor = by_path[str(parent)]
            if _matches(ancestor, required[0]):
                required.pop(0)
            parent = ancestor.get('parent_path')
        if not required:
            matches.append(node)
    if len(matches) != 1:
        raise EngineError('target_missing' if not matches else 'target_ambiguous')
    return matches[0]


def _class(value):
    # WinForms runtime suffix changes between launches; the declared class prefix remains.
    return re.sub(r'\.app\..*$', '', value or '')


def _local_file_state(path):
    path = Path(path)
    if not path.is_absolute():
        raise EngineError('file_condition_requires_absolute_path')
    local_dir(path.parent)
    if not path.exists():
        return {'exists': False}
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 1024 or not path.is_file():
        raise EngineError('invalid_result_file')
    return {'exists': True, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'creation_ns': info.st_ctime_ns}


class DesktopAdapter:
    def __init__(self, target_binding, run_dir):
        self.binding = _binding(target_binding)
        self.run_dir = local_dir(run_dir, True)
        self.main = inspect(self.binding, self.run_dir)
        self.lifetime = self.main.get('process_started_utc_ticks')
        if not isinstance(self.lifetime, str) or not self.lifetime:
            raise EngineError('process_lifetime_unavailable')

    def identity(self):
        return {'process_started_utc_ticks': self.lifetime,
                'main_window': dict(self.binding, title=self.main.get('title'),
                                    class_name=_class(self.main.get('class_name')))}

    def _window(self, step):
        main = inspect(self.binding, self.run_dir)
        if main.get('process_started_utc_ticks') != self.lifetime:
            raise EngineError('process_lifetime_changed')
        desired = step.get('target', {}).get('window') or {}
        title, classname = desired.get('title'), desired.get('class_name')
        def match(window):
            return ((not title or window.get('title') == title)
                    and (not classname or _class(window.get('class_name')) == _class(classname)))
        if match(main):
            return self.binding, main
        # Never bind another independent business window merely because its PID matches.
        candidates = [w for w in inventory(self.run_dir) if w.get('pid') == self.binding['pid'] and match(w)
                      and (w.get('owner_hwnd') == self.binding['hwnd']
                           or w.get('root_owner_hwnd') == self.binding['hwnd'])]
        if len(candidates) > 1:
            # Nested owned menus commonly share a blank title and WinForms class.
            # Their live contents must identify exactly one current surface.
            matches = []
            for candidate in candidates:
                candidate_binding = _binding(candidate)
                observed = inspect(candidate_binding, self.run_dir)
                if observed.get('complete') is not True:
                    raise EngineError('related_window_unproven')
                try:
                    resolve(observed.get('nodes', []), step.get('target', {}).get('locator'))
                except EngineError as error:
                    if str(error) == 'target_missing':
                        continue
                    raise EngineError('related_window_unproven') from error
                matches.append((candidate_binding, observed))
            if len(matches) == 1:
                return matches[0]
            raise EngineError('related_window_unproven')
        if len(candidates) != 1:
            raise EngineError('related_window_unproven')
        binding = _binding(candidates[0])
        return binding, inspect(binding, self.run_dir)

    def prepare(self, step, value):
        self._window(step)
        expected = step.get('expect', {})
        if expected.get('kind') == 'file':
            return {'file_before': _local_file_state(expected['path'])}
        return {}

    def execute(self, step, value=None):
        try:
            return self._execute(step, value)
        except EngineError as error:
            # All potentially dispatched branches return their own uncertain receipt.
            return {'status': 'not_delivered', 'input_dispatched': False,
                    'diagnostic': str(error) if re.fullmatch('[a-z_]{1,80}', str(error)) else 'preflight_failed'}

    def _execute(self, step, value=None):
        action = step['action']
        if action in {'wait', 'manual'}:
            return {'status': 'not_delivered', 'input_dispatched': False}
        binding, current = self._window(step)
        target = step['target']
        if target['kind'] == 'manual':
            return {'status': 'needs_review', 'input_dispatched': False,
                    'diagnostic': 'target_requires_manual_binding'}
        if target['kind'] == 'region':
            if target.get('reviewed') is not True:
                raise EngineError('recorded_region_requires_review')
            if action not in {'click', 'right-click', 'double-click'}:
                raise EngineError('region_action_not_supported')
            node = resolve(current['nodes'], target['locator'])
            if role(node.get('role')) == 'window' or current.get('complete') is not True:
                raise EngineError('region_identity_unproven')
            request = dict(binding, process_started_utc_ticks=self.lifetime)
            activation = _native('activate', request, self.run_dir)
            if activation.get('ok') is not True:
                return {'status': 'not_delivered', 'input_dispatched': False,
                        'diagnostic': 'target_activation_unavailable'}
            point = target.get('point', {})
            for key in ('x_ratio', 'y_ratio'):
                if type(point.get(key)) not in (int, float) or not 0 < point[key] < 1:
                    raise EngineError('invalid_recorded_point')
            try:
                result = _native('point', dict(request, locator=target['locator'], point=point, action=action), self.run_dir)
            except (Exception, KeyboardInterrupt):
                return {'status': 'uncertain', 'input_dispatched': None,
                        'diagnostic': 'point_delivery_unknown'}
            data = result.get('data') if isinstance(result.get('data'), dict) else result
            dispatched = data.get('input_dispatched')
            if type(dispatched) is not bool:
                dispatched = None
            receipt = {'status': 'executed_unverified' if result.get('ok') and dispatched is True
                       else 'not_delivered' if dispatched is False else 'uncertain',
                       'input_dispatched': dispatched, 'effect_verification': 'unavailable'}
            diagnostic = data.get('diagnostic')
            if isinstance(diagnostic, str) and re.fullmatch('[a-z_]{1,80}', diagnostic):
                receipt['diagnostic'] = diagnostic
            return receipt
        if target['kind'] != 'element':
            raise EngineError('unknown_target_kind')
        result = interact.run_inspect(SimpleNamespace(**binding, output=str(self.run_dir)))
        if result.get('status') != 'ready':
            raise EngineError('action_inspection_unavailable')
        session = self.run_dir / result['receipt']
        observation = read_json(session / 'inspection.json')
        node = resolve(observation['nodes'], target['locator'])
        combo = role(node.get('role')) == 'combobox'
        if combo and action in interact.VALUE_ACTIONS and value == '':
            return {'status': 'not_delivered', 'input_dispatched': False,
                    'diagnostic': 'empty_combo_value_requires_review'}
        if not node.get('ref'):
            return {'status': 'not_delivered', 'input_dispatched': False,
                    'diagnostic': 'target_has_no_action_ref'}
        if action == 'select' and isinstance(node.get('available_actions'), list) and not (
                {'Select', 'Click'} & set(node['available_actions'])):
            return {'status': 'not_delivered', 'input_dispatched': False,
                    'diagnostic': 'selection_action_not_supported'}
        args = SimpleNamespace(session=str(session), ref=node['ref'], action=action,
                               value=value if action in interact.VALUE_ACTIONS else None,
                               headed=bool(step.get('headed')), direction=step.get('direction'),
                               amount=step.get('amount'))
        if action == 'scroll':
            args.direction, args.amount = args.direction or 'down', args.amount or 3
        result = interact.run_act(args)
        if combo and action in {'set-value', 'type', 'select', 'clear'}:
            return self._combo_readback(result, step, '' if action == 'clear' else value)
        return result

    def _combo_readback(self, result, step, value):
        """A raw combo ValuePattern receipt is separate from selected-value evidence.

        Read the same target once through the Python helper after known delivery.
        Preserve the engine receipt and never repeat input or try another action.
        """
        if not isinstance(result, dict) or result.get('input_dispatched') is not True:
            return result
        status = result.get('status')
        if status not in {'completed', 'executed_unverified', 'verification_failed'}:
            return result
        check_step = dict(step, expect={'kind': 'value', 'value': value})
        try:
            checked = self.check(check_step, value)
        except KeyboardInterrupt:
            return dict(result, status='interrupted', engine_status=status,
                        effect_verification='unavailable', diagnostic='combo_readback_interrupted',
                        value_readback={'matched': None, 'diagnostic': 'combo_readback_interrupted'})
        except Exception:
            checked = {'matched': None, 'diagnostic': 'combo_value_unavailable'}
        output = dict(result, engine_status=status, value_readback=checked)
        matched = checked.get('matched')
        if matched is True:
            output.update(status='completed', effect_verification='matched', diagnostic='combo_value_observed')
        elif matched is False:
            output.update(status='verification_failed', effect_verification='mismatch', diagnostic='combo_value_mismatch')
        else:
            output.update(status='executed_unverified',
                          effect_verification='unavailable', diagnostic=checked.get('diagnostic', 'combo_value_unavailable'))
        return output

    def check(self, step, value=None):
        expected = step.get('expect', {})
        kind = expected.get('kind', 'none')
        if kind in {'none', 'manual'}:
            return {'matched': None, 'diagnostic': 'completion_not_automatically_observable'}
        if kind == 'file':
            state = _local_file_state(expected['path'])
            before = step.get('_prepared', {}).get('file_before')
            if not isinstance(before, dict):
                return {'matched': None, 'diagnostic': 'file_baseline_unavailable'}
            return {'matched': state.get('exists') is True and state.get('size', 0) > 0
                    and state != before, 'diagnostic': 'new_or_changed_result_file'}
        check_step = dict(step)
        if kind in {'exists', 'value', 'checked'} and isinstance(expected.get('target'), dict):
            check_step['target'] = expected['target']
        try:
            _, observed = self._window(check_step)
            node = resolve(observed['nodes'], check_step['target']['locator'])
        except EngineError as error:
            if str(error) == 'target_missing':
                return {'matched': False, 'diagnostic': 'target_missing'}
            return {'matched': None, 'diagnostic': 'target_not_uniquely_observed'}
        if node.get('secure') is True:
            return {'matched': None, 'diagnostic': 'protected_target'}
        if kind == 'exists':
            return {'matched': True}
        if kind == 'value':
            wanted = expected.get('value', value)
            actual = node.get('value')
            if role(node.get('role')) == 'combobox':
                if observed.get('complete') is not True:
                    return {'matched': None, 'diagnostic': 'combo_value_unavailable'}
                evidence = node.get('value_evidence')
                state = evidence.get('status') if isinstance(evidence, dict) else None
                if (state != 'observed' or evidence.get('source') not in {'parent', 'editor', 'selection', 'corroborated'}
                        or not isinstance(actual, str) or not actual or not isinstance(wanted, str) or not wanted):
                    return {'matched': None, 'diagnostic': 'combo_value_conflict' if state == 'conflict' else 'combo_value_unavailable'}
                return {'matched': actual == wanted, 'diagnostic': 'combo_value_observed'}
            return {'matched': actual == wanted if isinstance(actual, str) and isinstance(wanted, str) else None}
        if kind == 'checked':
            checked = node.get('checked')
            return {'matched': checked == expected.get('value') if type(checked) is bool else None}
        return {'matched': None, 'diagnostic': 'unsupported_completion_condition'}
