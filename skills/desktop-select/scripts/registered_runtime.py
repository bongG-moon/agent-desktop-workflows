"""Run registered SetValue/Invoke steps in one UIA session without tree snapshots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import desktop_select as app
import live_action as live
import native_targets
import registered_profile as profiles


READBACK_DELAY = .35


def _need(ok, reason):
    if not ok:
        raise app.bridge.BridgeError(reason)


def _combined(previous, current):
    return None if previous is None or current is None else bool(previous or current)


def values_for(profile, overrides):
    values = {k.casefold(): v for k, v in overrides.items()}
    _need(len(values) == len(overrides) and all(app.tasks._text(v, 10000) for v in values.values()),
          'invalid_override')
    parameters = {s['parameter'].casefold() for s in profile['steps'] if 'parameter' in s}
    _need(not set(values) - parameters, 'unknown_parameter')
    missing = list(dict.fromkeys(s['parameter'] for s in profile['steps']
                                if 'parameter' in s and s['parameter'].casefold() not in values
                                and 'default' not in s))
    return values, missing


def _wait(binding, title, seconds):
    deadline = time.monotonic() + seconds
    while True:
        app.guard(binding, binding, title)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.2, remaining))


def _check(binding, uia, root, element, selector, title, method):
    app.guard(binding, binding, title, mutate=True)
    current = native_targets.resolve(binding, uia, selector, root=root)
    _need(uia.same(element, current), 'registered_target_changed')
    _need(element.property(30010) is True, 'target_disabled')
    _need(('SetValue' if method == 'SetValue' else 'Click') in uia.actions(element),
          'registered_pattern_unavailable')
    shown = (live._combo_visible(binding, uia, element, root) if method == 'SetValue'
             else app.visible(binding, uia, element, root))
    _need(shown, 'target_offscreen')


def _dispatch(binding, uia, root, step, title, value=None, *, expected_element=None, authorize=None):
    selector = step['selector']
    method = 'SetValue' if step['action'] == 'set-value' else 'Invoke'
    receipt = dict(status='blocked', input_dispatched=False, method=method, target=selector,
                   business_commit='not_verified')
    pending = False

    def dispatching(_method):
        nonlocal pending
        pending = True
        receipt['input_dispatched'] = None

    try:
        if method == 'SetValue':
            # Direct commands also enter here without profile validation.
            _need(isinstance(value, str) and bool(value.strip()) and '\0' not in value
                  and len(value.encode('utf8')) <= 10000, 'invalid_combo_value')
        if authorize is not None:
            authorize()
        element = native_targets.resolve(binding, uia, selector, root=root)
        if expected_element is not None:
            _need(uia.same(element, expected_element), 'registered_target_changed')
        def check():
            if authorize is not None:
                authorize()
            _check(binding, uia, root, element, selector, title, method)
        check()
        if method == 'SetValue':
            before = live.combo_readback(binding, uia, element)
            receipt['before'] = before
            if (before.get('value_evidence', {}).get('status') == 'observed'
                    and before.get('value') == value):
                return dict(receipt, status='verified', diagnostic='value_already_selected',
                            effect_verification='matched', after=before)
        # No option inventory, expansion, selection fallback or input retry.
        live._call(element, method, value, check, dispatching)
        pending = False
        receipt['input_dispatched'] = True
        if method == 'Invoke':
            return dict(receipt, status='delivered_unverified', diagnostic='business_result_not_verified',
                        effect_verification='unavailable')
        _wait(binding, title, READBACK_DELAY)
        current = native_targets.resolve(binding, uia, selector, root=root)
        _need(uia.same(element, current), 'registered_target_changed')
        if authorize is not None:
            authorize()
        after = live.combo_readback(binding, uia, current)
        matched = after.get('value_evidence', {}).get('status') == 'observed' and after.get('value') == value
        return dict(receipt, status='verified' if matched else 'delivered_unverified', after=after,
                    effect_verification='matched' if matched else 'unavailable',
                    diagnostic='value_matched' if matched else 'value_not_verified')
    except Exception as error:
        return dict(receipt, status='uncertain' if pending else
                    'delivered_unverified' if receipt['input_dispatched'] else 'blocked',
                    diagnostic=getattr(error, 'code', 'provider_error'), effect_verification='unavailable')


def run(profile, overrides, *, read_only=False, hwnd=None):
    """Read-only mode neither restores a window nor waits for or dispatches actions."""
    started = time.monotonic()
    profile = profiles.validate(profile)
    values, missing = values_for(profile, overrides)
    if missing and not read_only:
        return dict(status='needs_input', missing_parameters=missing, input_dispatched=False, steps=[])
    report = dict(status='observed' if read_only else 'verified', input_dispatched=False, steps=[],
                  business_commit='not_verified')
    metrics = dict(uia_sessions=0, full_tree_snapshots=0, execution_processes=1)
    phase = started
    try:
        app.bridge.set_dpi_awareness()
        row = app.pick_window(app.windows(profile['exe']), profile['window'], hwnd)
        binding = app.bridge.Binding(row)
        app.guard(binding, binding, profile['window'])
        report['window_restored'] = False if read_only else app.restore_for_action(binding, profile['window'])
        metrics['window_seconds'] = round(time.monotonic() - phase, 4)
        with app.bridge.UIA() as uia:
            metrics['uia_sessions'] = 1
            root = uia.from_handle(binding.hwnd)
            phase = time.monotonic()
            # Find only registered IDs. Do not read the whole window or values of unrelated fields.
            for step in profile['steps']:
                if step['action'] != 'wait':
                    native_targets.resolve(binding, uia, step['selector'], root=root)
            metrics['preflight_seconds'] = round(time.monotonic() - phase, 4)
            for number, step in enumerate(profile['steps'], 1):
                phase = time.monotonic()
                if read_only:
                    if step['action'] == 'wait':
                        continue
                    item = native_targets.resolve(binding, uia, step['selector'], root=root)
                    receipt = dict(status='observed', target=step['selector'], input_dispatched=False,
                                   actions=uia.actions(item))
                    if step['action'] == 'set-value':
                        receipt.update(live.combo_readback(binding, uia, item))
                elif step['action'] == 'wait':
                    _wait(binding, profile['window'], step['seconds'])
                    receipt = dict(status='waited', seconds=step['seconds'], input_dispatched=False,
                                   business_result='not_verified')
                else:
                    value = (values.get(step['parameter'].casefold(), step.get('default'))
                             if 'parameter' in step else step.get('value'))
                    receipt = _dispatch(binding, uia, root, step, profile['window'], value)
                report['steps'].append(dict(number=number, label=step.get('label', ''), receipt=receipt,
                                            elapsed_seconds=round(time.monotonic() - phase, 4)))
                report['input_dispatched'] = _combined(report['input_dispatched'], receipt['input_dispatched'])
                if not read_only and receipt['status'] not in ('verified', 'waited'):
                    # Only a definitely returned Invoke may continue to the registered next step.
                    delivered = (step['action'] == 'invoke' and receipt['status'] == 'delivered_unverified'
                                 and receipt['input_dispatched'] is True
                                 and receipt.get('diagnostic') == 'business_result_not_verified')
                    if not delivered:
                        report.update(status=receipt['status'], diagnostic=receipt.get('diagnostic'), stopped_at=number)
                        break
                    report['status'] = 'completed_unverified'
    except Exception as error:
        report.update(status='blocked' if report['input_dispatched'] is False else 'uncertain',
                      diagnostic=getattr(error, 'code', 'provider_error'))
    metrics['elapsed_seconds'] = round(time.monotonic() - started, 4)
    return dict(report, metrics=metrics)


def main(profile_path, argv=None):
    parser = argparse.ArgumentParser(description='Run registered Windows actions without repeated model discovery.')
    parser.add_argument('--set', action='append', default=[], metavar='NAME=VALUE')
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--hwnd', type=int)
    parser.add_argument('--_worker', action='store_true', help=argparse.SUPPRESS)
    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    launched = False
    executing = False
    try:
        profile = profiles.load(profile_path)
        overrides = app.tasks.parse_overrides(args.set)
        values_for(profile, overrides)
        if args._worker:
            executing = True
            result = run(profile, overrides, read_only=args.read_only, hwnd=args.hwnd)
        else:
            # One timeout boundary for the whole workflow, not a fresh process per step.
            timeout = 90 + sum(s.get('seconds', 0) for s in profile['steps']) + len(profile['steps'])
            launched = True
            child = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(sys.argv[0]).resolve()),
                                    '--_worker', *argv], capture_output=True, encoding='utf8', timeout=timeout)
            result = json.loads(child.stdout)
            if child.returncode not in (0, 2, 3) or not isinstance(result, dict) or 'status' not in result:
                raise ValueError('invalid_execution_receipt')
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        result = dict(status='blocked' if args.read_only else 'uncertain',
                      diagnostic='provider_timeout_or_interrupted', input_dispatched=False if args.read_only else None)
    except Exception as error:
        uncertain = (launched or executing) and not args.read_only
        result = dict(status='uncertain' if uncertain else 'blocked',
                      diagnostic=getattr(error, 'code', 'registered_workflow_unavailable'),
                      input_dispatched=None if uncertain else False)
    print(json.dumps(result, ensure_ascii=False))
    return app.result_code(result)
