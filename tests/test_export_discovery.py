"""Bounded local export discovery and the path returned by fresh workbook verification."""
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import export_evidence as output


def write_workbook(path):
    sheet = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Override PartName="/xl/workbook.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
        archive.writestr('_rels/.rels', f'<Relationships><Relationship Type="{rel}/officeDocument" '
                         'Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', f'<workbook xmlns="{sheet}" xmlns:r="{rel}"><sheets>'
                         '<sheet name="Result" r:id="r1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', f'<Relationships><Relationship Id="r1" '
                         f'Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{sheet}"><sheetData/></worksheet>')


@unittest.skipUnless(os.name == 'nt', 'Windows process image and local drive checks')
class ExportDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.image = self.base / 'vendor' / 'suite' / 'app' / 'version' / 'bin' / 'Workflow.exe'
        self.image.parent.mkdir(parents=True)
        self.image.touch()
        self.profile = self.base / 'profile'
        self.binding = Mock()

    def discover(self, image=None):
        with patch.object(output, '_process_image', return_value=image or self.image), \
             patch.object(Path, 'home', return_value=self.profile), \
             patch.object(os, 'scandir', side_effect=AssertionError('discovery enumerated a directory')):
            return output.discover_directories(self.binding)

    def test_finds_ancestor_sibling_without_scanning_or_following_arbitrary_descendants(self):
        wanted = self.image.parents[3] / 'ExcelData'
        wanted.mkdir()
        (self.image.parent / 'arbitrary' / 'Output').mkdir(parents=True)
        (self.base / 'Exports').mkdir()  # Five levels above the install directory.
        observed = self.discover()
        self.assertEqual(observed['directories'], [str(wanted)])
        self.assertEqual(observed['suggested_output'], dict(kind='directory', path=str(wanted)))
        self.assertEqual(observed['file_verification'], 'pending_execution')
        self.assertFalse(observed['input_dispatched'])
        self.assertLessEqual(observed['candidate_paths_checked'], 49)

    def test_named_user_exports_are_candidates_and_multiple_folders_have_no_default(self):
        app_output = self.image.parent / 'Output'
        user_export = self.profile / 'Documents' / 'Exports'
        app_output.mkdir()
        user_export.mkdir(parents=True)
        (self.profile / 'Downloads').mkdir()
        observed = self.discover()
        self.assertEqual(observed['directories'], [str(app_output), str(user_export)])
        self.assertNotIn('suggested_output', observed)

    def test_candidate_and_parent_junctions_are_excluded(self):
        import _winapi
        destination = self.base / 'other'
        (destination / 'Output').mkdir(parents=True)
        self.profile.mkdir()
        try:
            _winapi.CreateJunction(str(destination), str(self.image.parent / 'ExcelData'))
        except PermissionError:
            self.skipTest('Current token cannot create a temporary NTFS junction')
        _winapi.CreateJunction(str(destination), str(self.profile / 'Documents'))
        observed = self.discover()
        self.assertEqual(observed['directories'], [])
        self.assertNotIn('suggested_output', observed)

    def test_unc_and_mapped_executable_are_rejected_before_path_access(self):
        for image in (r'\\server\share\bin\Workflow.exe', r'R:\bin\Workflow.exe'):
            with self.subTest(image=image), patch.object(ctypes.windll.kernel32, 'GetDriveTypeW', return_value=4), \
                 patch.object(output, '_plain', side_effect=AssertionError('remote filesystem accessed')), \
                 self.assertRaisesRegex(output.TaskError, 'export_local_path_required'):
                self.discover(Path(image))

    def test_process_image_uses_live_pid_and_checks_executable_identity(self):
        self.binding.pid = os.getpid()
        self.binding.exe = Path(sys.executable).name
        self.assertEqual(output._process_image(self.binding), Path(sys.executable))
        self.assertEqual(self.binding.check.call_count, 2)
        self.binding.exe = 'Unrelated.exe'
        with self.assertRaisesRegex(output.TaskError, 'export_executable_changed'):
            output._process_image(self.binding)

    def test_only_stable_valid_unique_new_file_supplies_exact_path(self):
        folder = self.base / 'ExcelData'
        folder.mkdir()
        saved = folder / 'existing.xlsx'
        write_workbook(saved)
        spec = dict(kind='directory', path=str(folder))
        before = output.baseline(spec)
        moved = saved.rename(folder / 'renamed.xlsx')
        self.assertFalse(output.wait(spec, before, timeout=.04, pause=.002)[0])
        self.assertIsNone(before.verified_path)
        moved.rename(saved)
        sent = folder / 'today.xlsx'
        write_workbook(sent)
        self.assertEqual(output.wait(spec, before, timeout=.4, pause=.002), (True, 'export_workbook_verified'))
        self.assertEqual(before.verified_path, str(sent))
        write_workbook(folder / 'unrelated.xlsx')
        self.assertEqual(output.wait(spec, before), (False, 'multiple_new_workbooks'))
        self.assertIsNone(before.verified_path)

    def test_old_workbook_moved_from_another_folder_is_not_fresh(self):
        folder = self.base / 'ExcelData'
        folder.mkdir()
        old = self.base / 'old.xlsx'
        write_workbook(old)
        spec = dict(kind='directory', path=str(folder))
        with patch.object(output.time, 'time_ns', return_value=output.time.time_ns() + 30_000_000_000):
            before = output.baseline(spec)
        old.rename(folder / old.name)
        self.assertFalse(output.wait(spec, before, timeout=.04, pause=.002)[0])
        self.assertIsNone(before.verified_path)

    def test_different_new_paths_on_successive_polls_remain_ambiguous(self):
        first, second = self.base / 'first.xlsx', self.base / 'second.xlsx'
        with patch.object(output, 'inventory', side_effect=[{first: 1}, {}, {second: 2}]), \
             patch.object(output, 'workbook') as parse:
            self.assertEqual(output.wait(dict(kind='directory'), {}, pause=0), (False, 'multiple_new_workbooks'))
        parse.assert_not_called()


if __name__ == '__main__':
    unittest.main()
