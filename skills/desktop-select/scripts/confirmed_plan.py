"""Reuse successful typed instructions; targets are still resolved live on every run."""
import json
import os
from pathlib import Path
import tempfile

import learned_tasks as tasks
from intent_tasks import plan_digest, validate_resolution
from intent_replay import validate_action


def _path(plan, directory):
    folder = tasks._directory(directory) / 'confirmed-plans'
    tasks._deny_redirect(folder)
    path = folder / (plan_digest(plan) + '.json')
    tasks._deny_redirect(path)
    return path


def load(plan, directory=None):
    """A changed instruction, position, order or app identity has a different key."""
    path = _path(plan, directory)
    try:
        with path.open('rb') as stream:
            data = stream.read(tasks.MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > tasks.MAX_FILE_BYTES:
        raise tasks.TaskError('confirmed_plan_too_large')
    result = json.loads(data, object_pairs_hook=tasks._unique_keys)
    validate_resolution(plan, result, validate_action)
    return result


def save(plan, resolution, directory=None):
    """Save the parameterized template, never runtime overrides or execution receipts."""
    validate_resolution(plan, resolution, validate_action)
    data = (json.dumps(resolution, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf8')
    if len(data) > tasks.MAX_FILE_BYTES:
        raise tasks.TaskError('confirmed_plan_too_large')
    path = _path(plan, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks._deny_redirect(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.plan-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        tasks._deny_redirect(path)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def replay(plan, exe, window, overrides, invoke, *, directory=None, only=None, resolution=None):
    """Use a prior successful interpretation without changing the execution contract."""
    source = 'provided' if resolution is not None else 'none'
    cache_issue = None
    if source == 'none':
        try:
            resolution = load(plan, directory)
            if resolution is not None:
                source = 'confirmed'
        except (ValueError, OSError, RuntimeError):
            # An invalid/unreadable cache cannot dispatch. Request interpretation
            # with the original task instead of making a user repair cache files.
            resolution = None
            cache_issue = 'confirmed_plan_unavailable'
    result = tasks.replay(plan, exe, window, overrides, invoke, only=only, resolution=resolution)
    result['resolution_source'] = source
    if cache_issue:
        result['plan_reuse'] = cache_issue
    if source == 'provided' and only is None and result['status'] in ('verified', 'completed_unverified'):
        try:
            save(plan, resolution, directory)
            result['plan_reuse'] = 'saved'
        except (ValueError, OSError, RuntimeError):
            # The UI operation already happened; cache I/O must not become an
            # execution failure that could provoke a repeated input.
            result['plan_reuse'] = 'save_unavailable'
    elif source == 'confirmed':
        result['plan_reuse'] = 'reused'
    return result
