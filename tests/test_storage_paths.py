"""Project recordings stay with the installed project across launch directories."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import storage_paths as storage


class StoragePathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / 'person'
        self.project = self.base / 'project-a'
        self.module = self.project / '.claude' / 'skills' / 'desktop-select' / 'scripts' / 'storage_paths.py'

    def info(self, **kwargs):
        return storage.storage_info(module_path=self.module, home=self.home, **kwargs)

    def test_project_store_is_outside_replaceable_skill_folder(self):
        observed = self.info(env={})
        self.assertEqual(observed['scope'], 'project')
        self.assertEqual(Path(observed['project_root']), self.project)
        self.assertEqual(Path(observed['task_directory']), self.project / '.desktop-select' / 'tasks')
        self.assertEqual(observed['source'], 'skill_installation')
        self.assertFalse(self.project.exists(), 'Path selection must not create data or project folders')

    def test_same_named_tasks_in_separate_project_installs_do_not_collide(self):
        second = self.base / 'project-b' / '.claude' / 'skills' / 'desktop-select' / 'scripts' / 'storage_paths.py'
        first_store = storage.default_store(module_path=self.module, home=self.home, env={})
        second_store = storage.default_store(module_path=second, home=self.home, env={})
        self.assertNotEqual(first_store / 'same-task.json', second_store / 'same-task.json')
        self.assertNotEqual(first_store / 'execution-plans', second_store / 'execution-plans')

    def test_invoking_from_other_directory_does_not_select_other_project(self):
        before = self.info(env={})
        previous = Path.cwd()
        try:
            os.chdir(self.base)
            self.assertEqual(self.info(env={}), before)
        finally:
            os.chdir(previous)

    def test_portable_zip_keeps_tasks_inside_unpacked_skill(self):
        skill = self.base / 'download' / 'desktop-select'
        observed = storage.storage_info(module_path=skill / 'scripts' / 'storage_paths.py',
                                        home=self.home, env={})
        self.assertEqual(observed['scope'], 'portable')
        self.assertEqual(Path(observed['task_directory']), skill / '.desktop-select' / 'tasks')
        self.assertFalse(skill.exists())

    def test_default_never_reads_or_changes_former_shared_store(self):
        old = self.base / 'local-app-data' / 'DesktopSelect' / 'tasks'
        old.mkdir(parents=True)
        original = old / 'old-task.json'
        original.write_bytes(b'old recording')
        observed = self.info(env={'LOCALAPPDATA': str(old.parent.parent)})
        self.assertEqual(Path(observed['task_directory']), self.project / '.desktop-select' / 'tasks')
        self.assertEqual(original.read_bytes(), b'old recording')
        self.assertEqual(list(old.iterdir()), [original])

    def test_explicit_absolute_store_is_preserved(self):
        custom = self.base / 'approved-store' / 'tasks'
        observed = self.info(env={'DESKTOP_SELECT_TASK_DIR': str(custom)})
        self.assertEqual(observed, dict(scope='custom', project_root=None,
                                       task_directory=str(custom), source='DESKTOP_SELECT_TASK_DIR'))
        self.assertFalse(custom.exists())

    def test_relative_override_reports_problem_instead_of_using_cwd(self):
        for relative in ('tasks', './tasks', '../shared/tasks'):
            with self.subTest(relative=relative), self.assertRaisesRegex(
                    storage.StorageError, 'task_directory_absolute_required'):
                self.info(env={'DESKTOP_SELECT_TASK_DIR': relative})

    def test_present_empty_or_malformed_override_does_not_fall_back(self):
        for value in ('', '  ', None, ' path ', 'bad\x00path', 'bad\npath'):
            with self.subTest(value=value), self.assertRaises(storage.StorageError) as error:
                self.info(env={'DESKTOP_SELECT_TASK_DIR': value})
            self.assertEqual(error.exception.code, 'invalid_task_directory')

    def test_user_global_install_requires_explicit_store(self):
        module = self.home / '.claude' / 'skills' / 'desktop-select' / 'scripts' / 'storage_paths.py'
        with self.assertRaisesRegex(storage.StorageError, 'project_store_required'):
            storage.default_store(module_path=module, home=self.home, env={'LOCALAPPDATA': str(self.base / 'local')})
        self.assertFalse(self.home.exists())

    def test_user_global_install_accepts_explicit_shared_store(self):
        module = self.home / '.claude' / 'skills' / 'desktop-select' / 'scripts' / 'storage_paths.py'
        shared = self.base / 'local' / 'DesktopSelect' / 'tasks'
        observed = storage.storage_info(module_path=module, home=self.home,
                                        env={'DESKTOP_SELECT_TASK_DIR': str(shared)})
        self.assertEqual((observed['scope'], Path(observed['task_directory'])), ('custom', shared))

    def test_installation_path_is_not_resolved_before_existing_redirect_checks(self):
        with patch.object(Path, 'resolve', side_effect=AssertionError('must preserve reparse point')):
            self.assertEqual(Path(self.info(env={})['task_directory']), self.project / '.desktop-select' / 'tasks')

    @unittest.skipUnless(os.name == 'nt', 'Windows installation names are case-insensitive')
    def test_windows_project_layout_case_is_accepted(self):
        module = self.project / '.CLAUDE' / 'SKILLS' / 'desktop-select' / 'scripts' / 'storage_paths.py'
        observed = storage.storage_info(module_path=module, home=self.home, env={})
        self.assertEqual(observed['scope'], 'project')


if __name__ == '__main__':
    unittest.main()
