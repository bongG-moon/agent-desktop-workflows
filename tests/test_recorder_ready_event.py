"""The launch receipt belongs to the visible editor and is emitted exactly once."""
import unittest
from unittest.mock import Mock, patch

from test_teach_capture import capture


class RecorderReadyTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
        except ImportError as error:
            self.skipTest(str(error))
        self.tk = tk
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.withdraw()

    def open_review(self, ready):
        with patch.object(self.tk, 'Tk', return_value=self.root):
            return capture.capture(None, title='Example window', initial_draft=[], on_ready=ready)

    def test_only_mapped_root_can_signal_ready_and_remapping_does_not_repeat_it(self):
        root = self.root
        observations = []
        def observed():
            observations.append((root.title(), root.winfo_viewable()))
            root.after_cancel(deadline)
            root.after_idle(remap)

        ready = Mock(side_effect=observed)

        def show():
            panel = root.winfo_children()[0]
            panel.event_generate('<Map>')
            observations.append(('before_show', ready.call_count))
            root.deiconify()

        def remap():
            root.withdraw()
            root.deiconify()
            root.after_idle(root.destroy)

        # The recording editor constructs its controls before entering mainloop.
        # Remapping on a timer started here races the first real Map event.
        deadline = root.after(3000, root.destroy)
        root.after(0, show)
        receipt = self.open_review(ready)

        self.assertEqual(receipt['status'], 'cancelled')
        self.assertEqual(observations, [('before_show', 0), ('Desktop Select 녹화', True)])
        ready.assert_called_once_with()

    def test_closing_before_mapping_sends_no_ready_event(self):
        ready = Mock()
        self.root.after(0, self.root.destroy)
        receipt = self.open_review(ready)
        self.assertEqual(receipt['status'], 'cancelled')
        ready.assert_not_called()
