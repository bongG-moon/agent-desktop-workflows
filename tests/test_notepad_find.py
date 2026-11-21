import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'skills' / 'desktop-select' / 'scripts'), str(REPO / 'scripts')]

import notepad_find as find


def overlay_frame(scale, *, dark=True, down=1, up=-1, clutter=False, shifted=0):
    window = [517, 246, round(813*scale), round(76*scale)]
    strip = find._strip(window, scale)
    width, height = strip[2:]
    y_center = window[1]+window[3]//2-strip[1]
    x_centers = [window[0]+window[2]-round(offset*scale)-strip[0] for offset in (155, 115)]
    result = bytearray()
    for y in range(height):
        for x in range(width):
            value = (49 if x < width/2 else 39) if dark else (229 if x < width/2 else 241)
            for index, direction in enumerate((down, up)):
                if direction is None:
                    continue
                dx = (x-x_centers[index]-(shifted if index == 0 else 0))/scale
                dy = (y-y_center+scale)/scale
                shaft = abs(dx) <= .5 and abs(dy) <= 7.5
                head = abs(dx) <= 7 and abs(direction*dy-(7.5-abs(dx))) <= .55
                debris = clutter and index == 0 and abs(dx) >= 5 and direction*dy < -1
                if shaft or head or debris:
                    value = 255 if dark else 5
            result.extend((value, value, value, 0))
    return bytes(result), strip, window, scale


class NotepadFindTests(unittest.TestCase):
    def test_pair_at_multiple_dpis_including_light_and_dark_hover(self):
        for scale in (1, 1.25, 1.5, 2, 3):
            for dark in (False, True):
                with self.subTest(scale=scale, dark=dark):
                    pixels, strip, overlay, _ = overlay_frame(scale, dark=dark)
                    match = find._find_arrows(pixels, strip, overlay, scale)
                    self.assertIsNotNone(match)
                    self.assertAlmostEqual(match[0], overlay[0]+overlay[2]-155*scale, delta=1)

    def test_rejects_wrong_direction_missing_peer_clutter_and_shift(self):
        for changes in ({'down': -1}, {'up': None}, {'up': 1}, {'clutter': True}, {'shifted': 8}):
            with self.subTest(changes=changes):
                self.assertIsNone(find._find_arrows(*overlay_frame(2, **changes)))

    def test_clicked_point_must_be_inside_proved_button(self):
        proof = {'box': [110, 200, 37, 37]}
        self.assertTrue(find.contains_click(proof, 110, 200))
        self.assertTrue(find.contains_click(proof, 146, 236))
        self.assertFalse(find.contains_click(proof, 147, 200))
        self.assertFalse(find.contains_click(proof, 109, 218))


if __name__ == '__main__':
    unittest.main()
