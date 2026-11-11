"""A demonstrated visual row keeps its own meaning through the recorder review."""
from copy import deepcopy
import multiprocessing as mp
from pathlib import Path
import sys
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/desktop-select/scripts'))
import desktop_select
import row_teach
import teach_capture


class RowTeachTests(unittest.TestCase):
    def test_hung_reader_times_out_and_its_process_is_reclaimed(self):
        job = row_teach.SampleJob.__new__(row_teach.SampleJob)
        ctx = mp.get_context('spawn')
        job.receiver, sender = ctx.Pipe(duplex=False)
        job.process = ctx.Process(target=time.sleep, args=(25,))
        job.process.start()
        pid = job.process.pid
        job.started = time.monotonic() - 30
        try:
            with self.assertRaisesRegex(RuntimeError, 'row_sample_timeout'):
                job.poll()
        finally:
            job.close()
            sender.close()
        self.assertNotIn(pid, [p.pid for p in mp.active_children()])

    def test_drag_is_row_relative_without_silently_cropping_a_neighbor(self):
        self.assertEqual(row_teach.selection_box((78, 22), (57, 3), (110, 32)), [57, 3, 21, 19])
        with self.assertRaises(ValueError):
            row_teach.selection_box((78, 22), (57, 34), (110, 32))
        with self.assertRaises(ValueError):
            row_teach.selection_box((78, 22), (77, 20), (110, 32))

    def test_row_action_review_names_stay_bound_to_the_teaching(self):
        spec = {'key': 'first example'}
        step = row_teach.raw_row_step('  첫 행 시작 후 준비 표시 확인  ', spec)
        different = deepcopy(step)
        different['row']['key'] = 'different example'
        edits = teach_capture.StepEdits()
        edits.store(step, '첫 장비', '')
        with self.assertRaisesRegex(ValueError, 'recorded_target_changed'):
            edits.apply([different])
        self.assertEqual(edits.apply([step])[0]['alias'], '첫 장비')
        self.assertEqual(teach_capture.default_text(step), '첫 행 시작 후 준비 표시 확인')
        self.assertIn('같은 행', teach_capture.instruction_view(step, None))
        with self.assertRaises(ValueError):
            row_teach.raw_row_step(' ', spec)
