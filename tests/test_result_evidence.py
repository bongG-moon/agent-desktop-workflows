"""Fresh query transitions, protected local outputs and workbook evidence at the final click."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import export_evidence as output
import learned_tasks as tasks
import result_menu as result


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.menu = dict(target={'role':'table','automation_id':'grid'}, point=[.5,.5], path=['Export','Sheet'])
        self.trigger = {'role':'button','automation_id':'go'}
        self.ready = dict(kind='text', target={'role':'statictext','automation_id':'flag'}, expected='Ready')
        self.spec = dict(menu=self.menu, trigger=self.trigger, ready=self.ready, output=None)

    def test_cannot_learn_unverified_trigger_or_another_surfaces_visual_condition(self):
        for rule in (None, dict(kind='text',target=self.trigger,expected='Run'),
                     dict(kind='visual',target=dict(role='group',name='Other'),size=[10,10],
                          box=[0,0,8,8],sample='')):
            with self.subTest(rule=rule), self.assertRaises(tasks.TaskError):
                result.validate_spec(dict(self.spec, ready=rule))

    def test_busy_then_two_ready_reads_required_if_already_ready(self):
        read = Mock(side_effect=[True, True, False, True, False, True, True])
        self.assertTrue(result._wait_for_ready(read, needs_transition=True, pause=0))
        self.assertEqual(7, read.call_count)
        self.assertFalse(result._wait_for_ready(lambda:True, needs_transition=True, timeout=0))

    def test_uncertain_query_cannot_send_right_click(self):
        receipt = dict(status='uncertain', input_dispatched=None, diagnostic='provider_failed')
        with patch.object(result,'_matches',return_value=False), patch.object(result,'_query',return_value=receipt), \
             patch.object(result.context_menu,'execute') as menu:
            answer = result.execute(None,self.spec,lambda:None,'App')
            menu.assert_not_called()
            self.assertEqual('uncertain', answer['status'])
            self.assertIsNone(answer['query_dispatched'])

    def test_menu_only_workflow_never_claims_file_verified(self):
        with patch.object(result.context_menu,'execute',return_value=dict(status='delivered_unverified',
                          input_dispatched=True,leaf_dispatched=True,menu_path_completed=True)):
            answer = result.execute(None,dict(self.spec,trigger=None,ready=None),lambda:None,'App')
            self.assertEqual('delivered_unverified',answer['status'])
            self.assertTrue(answer['menu_path_completed'])
            self.assertNotEqual('matched',answer['effect_verification'])

    def test_newly_created_status_is_allowed_but_ambiguous_baseline_blocks_query(self):
        click = dict(input_dispatched=True, status='delivered_unverified', diagnostic='sent')
        with patch.object(result,'_matches',side_effect=[tasks.TaskError('result_indicator_missing'),True,True]), \
             patch.object(result,'_query',return_value=click) as query, patch.object(result.time,'sleep'), \
             patch.object(result.context_menu,'execute',return_value=dict(status='delivered_unverified',
                          input_dispatched=True,leaf_dispatched=True,menu_path_completed=True)) as menu:
            answer = result.execute(None,self.spec,lambda:None,'App')
            self.assertTrue(answer['query_verified'])
            query.assert_called_once()
            menu.assert_called_once()
        with patch.object(result,'_matches',side_effect=tasks.TaskError('result_indicator_ambiguous')), \
             patch.object(result,'_query') as query:
            self.assertFalse(result.execute(None,self.spec,lambda:None,'App')['input_dispatched'])
            query.assert_not_called()

    def test_rejects_remote_relative_device_and_ambiguous_paths(self):
        for path in (r'\\host\share\out.xlsx','out.xlsx',r'C:\tmp\out:stream.xlsx',
                     r'C:\tmp\..\out.xlsx',r'\\?\C:\tmp\out.xlsx',r'C:\tmp\CON.xlsx',r'C:\tmp\*.xlsx'):
            with self.subTest(path=path), self.assertRaises(tasks.TaskError):
                output.validate(dict(kind='file',path=path))

    def test_directory_rejects_two_new_outputs_even_when_one_was_ready_first(self):
        a,b = Path('a.xlsx'),Path('b.xlsx')
        with patch.object(output,'inventory',side_effect=[{a:1},{a:1,b:2}]), patch.object(output,'workbook') as read:
            self.assertEqual((False,'multiple_new_workbooks'),output.wait(dict(kind='directory'),{},pause=0))
            read.assert_not_called()

    def test_file_predating_leaf_or_new_invalid_zip_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'done.xlsx'
            spec = dict(kind='file',path=str(p))
            p.write_bytes(b'Excel placeholder')
            with patch.object(output,'workbook') as read:
                self.assertFalse(output.wait(spec,output.inventory(spec),timeout=0)[0])
                read.assert_not_called()
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('[Content_Types].xml','<Types/>')
            self.assertFalse(output.workbook(p))

    def test_timestamp_change_alone_does_not_verify_existing_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'prior.xlsx'
            p.write_bytes(b'existing content')
            spec = dict(kind='file',path=str(p))
            before = output.baseline(spec)
            os.utime(p, ns=(p.stat().st_atime_ns,p.stat().st_mtime_ns+2_000_000_000))
            with patch.object(output,'workbook',return_value=True):
                self.assertFalse(output.wait(spec,before,timeout=.05,pause=.002)[0])


if __name__ == '__main__':
    unittest.main()
