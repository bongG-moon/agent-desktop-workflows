"""Replace a reviewed task without destroying its original bytes or another edit."""
import json
import os
from pathlib import Path
import tempfile
import uuid

import learned_tasks as tasks
from intent_tasks import plan_digest


def replace_task(plan, expected_digest, directory=None):
    tasks._validate(plan)
    data = (json.dumps(plan, ensure_ascii=False, indent=2)+'\n').encode('utf8')
    if len(data) > tasks.MAX_FILE_BYTES:
        raise tasks.TaskError('task_too_large')
    base = tasks._directory(directory)
    path = base / (tasks._name(plan['name'])+'.json')
    lock = path.with_suffix('.edit-lock')
    temp = None
    descriptor = None
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        current = tasks.load_task(plan['name'], directory)
        if plan_digest(current) != expected_digest:
            raise tasks.TaskError('task_changed_during_edit')
        history = base / 'history'
        tasks._deny_redirect(history)
        history.mkdir(exist_ok=True)
        backup = history / (plan['name']+'.'+uuid.uuid4().hex+'.json')
        tasks._deny_redirect(path)
        original = path.read_bytes()
        if len(original) > tasks.MAX_FILE_BYTES or plan_digest(json.loads(original)) != expected_digest:
            raise tasks.TaskError('task_changed_during_edit')
        with backup.open('xb') as f:
            f.write(original)
            f.flush()
            os.fsync(f.fileno())
        with tempfile.NamedTemporaryFile(mode='wb', dir=base, prefix='.edit-', delete=False) as f:
            temp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tasks._deny_redirect(path)
        if plan_digest(tasks.load_task(plan['name'], directory)) != expected_digest:
            raise tasks.TaskError('task_changed_during_edit')
        os.replace(temp, path)
        return {'path': str(path), 'backup': str(backup)}
    except FileExistsError as error:
        raise tasks.TaskError('task_already_being_edited') from error
    except OSError as error:
        raise tasks.TaskError('task_store_unavailable') from error
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        if descriptor is not None:
            os.close(descriptor)
            lock.unlink(missing_ok=True)
