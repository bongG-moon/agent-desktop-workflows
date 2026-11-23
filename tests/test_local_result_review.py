import gc
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'skills/desktop-select/scripts'))
import local_result_review as local


def tearDownModule():
    # Finalize closed Tk objects on their owning thread before later tests
    # start the asynchronous window picker.
    gc.collect()


class LocalReviewTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk display unavailable: {error}')
        self.root.withdraw()
        def close():
            try:
                self.root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)/'sample.png'
        image = tk.PhotoImage(master=self.root,width=160,height=120)
        image.write(str(path),format='png')
        self.review = local.ResultReview(self.root,path,'Code',[20,10,120,80],expected='PART-42')
        self.root.update_idletasks()

    @staticmethod
    def event(x, y):
        return SimpleNamespace(x=x, y=y)

    def pick(self, point):
        review = self.review
        review.press(self.event(7,5))
        review.release(self.event(48,5))
        review.press(self.event(*point))
        review.release(self.event(*point))

    def test_crop_reports_original_image_coordinates_and_exact_displayed_text(self):
        self.pick((18,32))
        self.review.value.set('PART-42')
        self.review.confirm.invoke()
        self.assertEqual(self.review.result,dict(header=[27,15,68],point=[38,42],value='PART-42'))

    def test_other_column_header_and_similar_text_cannot_confirm(self):
        for point in ((65,20),(10,4),(10,5)):
            self.pick(point)
            self.review.value.set('PART-42')
            self.assertTrue(self.review.confirm.instate(['disabled']))
        self.pick((18,32))
        self.review.value.set('PART-4Z')
        self.review.accept()
        self.assertIsNone(self.review.result)
        self.review.refresh()
        self.assertEqual(self.review.result,dict(refresh=True))

    def test_row_review_accepts_one_point_and_explicit_confirmation_without_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rows.png'
            image = tk.PhotoImage(master=self.root, width=160, height=120)
            image.write(str(path), format='png')
            review = local.ResultReview(self.root, path, None, [20,10,120,80], row_policy='single')
            self.assertIsNone(review.entry)
            review.press(self.event(18,32))
            review.release(self.event(18,32))
            self.assertTrue(review.confirm.instate(['disabled']))
            review.confirmed.set(True)
            review.update_button()
            review.accept()
            self.assertEqual(review.result,dict(point=[38,42],row_policy='single',confirmed=True))

    def test_row_review_resets_confirmation_after_point_change_and_requires_table_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'rows.png'
            image = tk.PhotoImage(master=self.root, width=160, height=120)
            image.write(str(path), format='png')
            review = local.ResultReview(self.root, path, None, [20,10,120,80], row_policy='first')
            review.press(self.event(18,32))
            review.release(self.event(18,32))
            review.confirmed.set(True)
            review.update_button()
            review.press(self.event(22,35))
            review.release(self.event(22,35))
            self.assertFalse(review.confirmed.get())
            self.assertTrue(review.confirm.instate(['disabled']))
            with self.assertRaisesRegex(local.ResultReviewError, 'result_row_review_scope_required'):
                local.ResultReview(self.root, path, None, None, row_policy='first')

    def test_resized_scrolled_image_keeps_original_source_coordinates(self):
        root = tk.Tk()
        root.withdraw()
        def close():
            try:
                root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'large.png'
            image = tk.PhotoImage(master=root, width=1600, height=1200)
            image.write(str(path), format='png')
            review = local.ResultReview(root, path, 'Code', [200,100,1200,900], 'PART-42')
            review.canvas.configure(width=320, height=180)
            # The hidden dialog retains its initial allocated geometry. Prevent
            # grid stretching so this test really exercises a small viewport,
            # including when another test enabled process-wide DPI awareness.
            review.canvas.grid_configure(sticky='nw')
            root.update_idletasks()
            self.assertLess(review.canvas.winfo_width(), 1200)
            self.assertLess(review.canvas.winfo_height(), 900)
            review.canvas.xview_moveto(.2)
            review.canvas.yview_moveto(.3)
            left = int(review.canvas.canvasx(12))
            top = int(review.canvas.canvasy(15))
            right = int(review.canvas.canvasx(72))
            point = [int(review.canvas.canvasx(32)), int(review.canvas.canvasy(55))]
            self.assertGreater(left, 12)
            self.assertGreater(top, 15)
            review.press(self.event(12,15))
            review.release(self.event(72,15))
            review.press(self.event(32,55))
            review.release(self.event(32,55))
            review.value.set('PART-42')
            review.accept()
            self.assertEqual(review.result, dict(
                header=[200+left,100+top,200+right],
                point=[200+point[0],100+point[1]], value='PART-42'))

    def test_clipped_edge_crop_preserves_source_header_and_cell_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'partial.png'
            image = tk.PhotoImage(master=self.root, width=1296, height=839)
            image.write(str(path), format='png')
            review = local.ResultReview(self.root, path, 'Code', [10,197,1286,562], 'PART-42',
                                        frame_size=[1296,839])
            review.press(self.event(20,10))
            review.release(self.event(90,10))
            review.press(self.event(45,35))
            review.release(self.event(45,35))
            review.value.set('PART-42')
            review.accept()
            self.assertEqual(review.result, dict(header=[30,207,100],point=[55,232],value='PART-42'))

    def test_image_size_mismatch_and_unclipped_crop_keep_precise_geometry_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'partial.png'
            image = tk.PhotoImage(master=self.root, width=160, height=120)
            image.write(str(path), format='png')
            with self.assertRaises(local.ResultReviewError) as error:
                local.ResultReview(self.root, path, 'Code', [20,10,120,80], frame_size=[161,120])
            self.assertEqual(error.exception.code, 'result_capture_size_mismatch')
            self.assertEqual(error.exception.geometry, dict(capture_size=[161,120], image_size=[160,120]))
            with self.assertRaises(local.ResultReviewError) as error:
                local.ResultReview(self.root, path, 'Code', [20,10,150,80])
            self.assertEqual(error.exception.code, 'result_table_outside_frame')
            self.assertEqual(error.exception.geometry['crop_relative'], [20,10,150,80])
