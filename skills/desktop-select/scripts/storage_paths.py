"""Choose a local task store from the installation, never the current directory.

This module only selects paths. The task store's existing local-path and reparse
checks still apply before any file is read or written. No folder is created here.
"""
import os
from pathlib import Path


class StorageError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _absolute(path):
    # Keep symlinks/junctions visible to the task store's ancestor checks.
    # Path.resolve() would follow them before those checks can inspect them.
    return Path(os.path.abspath(path))


def storage_info(*, module_path=None, home=None, env=None):
    """Describe the selected store without creating or inspecting stored tasks.

    A project installation belongs to the directory above .claude/skills. An
    unpacked portable installation keeps data below its own skill folder. A
    user-global installation must explicitly name its store, because HOME is
    not an unambiguous project. Explicit stores must be absolute so changing a
    launcher's working directory cannot silently change the selected tasks.
    """
    environment = os.environ if env is None else env
    if 'DESKTOP_SELECT_TASK_DIR' in environment:
        value = environment['DESKTOP_SELECT_TASK_DIR']
        if (not isinstance(value, str) or not value.strip() or value != value.strip()
                or any(ord(character) < 32 for character in value)):
            raise StorageError('invalid_task_directory')
        path = Path(value)
        if not path.is_absolute():
            raise StorageError('task_directory_absolute_required')
        return dict(scope='custom', project_root=None,
                    task_directory=str(_absolute(path)), source='DESKTOP_SELECT_TASK_DIR')

    module = _absolute(__file__ if module_path is None else module_path)
    skill = module.parent.parent
    if (skill.parent.name.casefold() == 'skills'
            and skill.parent.parent.name.casefold() == '.claude'):
        project = skill.parent.parent.parent
        user_home = _absolute(Path.home() if home is None else home)
        if project == user_home:
            raise StorageError('project_store_required')
        return dict(scope='project', project_root=str(project),
                    task_directory=str(project / '.desktop-select' / 'tasks'),
                    source='skill_installation')

    return dict(scope='portable', project_root=str(skill),
                task_directory=str(skill / '.desktop-select' / 'tasks'),
                source='portable_installation')


def default_store(*, module_path=None, home=None, env=None):
    return Path(storage_info(module_path=module_path, home=home, env=env)['task_directory'])
