"""An edited demonstration keeps its original file and never overwrites another edit."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import intent_tasks as intent
import learned_tasks as tasks
import task_edit


def plan(text='여기서 현재 필요한 작업을 해 줘'):
    step = intent.make_step(dict(role='combobox', automation_id='condition'), text)
    return tasks.from_recording('sample', 'Sample.exe', 'Sample', [step])


class EditTests(unittest.TestCase):
    def test_edit_preserves_original_bytes_and_replaces_description(self):
        with tempfile.TemporaryDirectory() as folder:
            original=plan()
            path=tasks.save_task(original, folder)
            old=path.read_bytes()
            edited=plan('실행할 때 받은 {Family} 값으로 선택해 줘')
            result=task_edit.replace_task(edited, intent.plan_digest(original), folder)
            self.assertEqual(Path(result['backup']).read_bytes(), old)
            self.assertEqual(tasks.load_task('sample',folder), edited)
            self.assertEqual(tasks.list_tasks(folder), ['sample'])
            self.assertEqual(edited['steps'][0]['instruction']['parameters'], ['Family'])

    def test_stale_editor_and_concurrent_editor_cannot_overwrite_new_work(self):
        with tempfile.TemporaryDirectory() as folder:
            original=plan()
            path=tasks.save_task(original,folder)
            updated=plan('새 설명')
            task_edit.replace_task(updated,intent.plan_digest(original),folder)
            with self.assertRaisesRegex(tasks.TaskError,'task_changed_during_edit'):
                task_edit.replace_task(plan('오래된 창'),intent.plan_digest(original),folder)
            self.assertEqual(tasks.load_task('sample',folder),updated)
            lock=path.with_suffix('.edit-lock')
            lock.write_text('another editor')
            with self.assertRaisesRegex(tasks.TaskError,'task_already_being_edited'):
                task_edit.replace_task(plan('세 번째 창'),intent.plan_digest(updated),folder)
            self.assertEqual(lock.read_text(),'another editor')

    def test_unrecognized_target_and_free_multiline_description_save_as_editable_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            step=intent.make_step(None,'여기 근처에서 작업해 줘.\n필요한 값은 실행할 때 물어봐.')
            draft=tasks.from_recording('draft','Sample.exe','Sample',[step])
            tasks.save_task(draft,folder)
            loaded=tasks.load_task('draft',folder)
            self.assertIsNone(loaded['steps'][0]['anchor'])
            self.assertEqual(loaded['steps'][0]['instruction']['text'],step['instruction']['text'])
            called=[]
            result=tasks.replay(loaded,'Sample.exe','Sample',{},lambda s:called.append(s))
            self.assertEqual(result['status'],'needs_target')
            self.assertFalse(called)
