"""A spoken launch must refer to a real affordance on the currently requested row."""
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills' / 'desktop-select' / 'scripts'))
import desktop_select
import row_intent


def painted(*, border=True, triangle=True):
    w,h = 80,40
    pixels = bytearray(b'\xff' * (w*h*3))
    def dot(x,y):
        pixels[(y*w+x)*3:(y*w+x+1)*3] = bytes(3)
    if border:
        for x in range(35,70):
            dot(x,4)
            dot(x,36)
        for y in range(4,37):
            dot(35,y)
            dot(69,y)
    for y in range(10,31):
        extent = round(16 * (1 - abs((y-20)/11))) if triangle else 16
        for x in range(44,44+extent):
            dot(x,y)
    return bytes(pixels),w,h


class RowIntentTests(TestCase):
    def test_play_glyph_requires_shape_and_an_enclosing_control(self):
        self.assertEqual(len(row_intent.play_glyphs(*painted())),1)
        self.assertEqual(row_intent.play_glyphs(*painted(border=False)),[])
        self.assertEqual(row_intent.play_glyphs(*painted(triangle=False)),[])

    def test_host_can_normalize_loose_wording_but_cannot_invert_an_explicit_action(self):
        self.assertEqual(row_intent.parse('첫번째 요소 실행버튼 누르기'),1)
        self.assertEqual(row_intent.parse('두 번째 행을 실행해 줘'),2)
        self.assertEqual(row_intent.parse('1번째 행의 ▶ 버튼을 눌러줘'), 1)
        self.assertEqual(row_intent.parse('두 번째 줄 ▶를 눌러줘'), 2)
        self.assertIsNone(row_intent.parse('1번째 행 버튼을 눌러줘'))
        self.assertIsNone(row_intent.parse('첫 행 말고 둘째만 켜줘'))
        self.assertFalse(row_intent.conflicts_with_launch('거기 맨 위에 거 켜줘'))
        for sentence in ('두 번째 행 실행하지 마세요','첫 번째 행 종료해줘','do not start the first item'):
            self.assertTrue(row_intent.conflicts_with_launch(sentence))

    def test_device_number_describes_the_result_of_opening_the_requested_row(self):
        self.assertEqual(row_intent.parse('MuMu 관리창에서 6번째 행의 실행 버튼을 눌러 04 기기를 열어줘.'), 6)
        self.assertEqual(row_intent.parse('관리 창에서 7번째 행의 실행 버튼을 눌러서 05번 기기를 열어줘.'), 7)
        for text in ('MuMu 관리창에서 6번째 행의 실행 버튼을 눌러 04 기기를 삭제해줘.',
                     '관리창에서 6번째 행 말고 04 기기를 열어줘.',
                     '관리창에서 6번째 행의 실행 버튼을 눌러 04번째 행의 실행 버튼도 눌러줘.',
                     '저장하고 관리창에서 6번째 행의 실행 버튼을 눌러 04 기기를 열어줘.'):
            self.assertIsNone(row_intent.parse(text), text)

    def test_distinct_lists_and_overlapping_rows_cannot_be_merged_into_one_ordinal(self):
        rows = [([0,30,100,20], {'viewport':(0,0,100,90)}),([0,5,100,20],{'viewport':(0,0,100,90)})]
        self.assertEqual(row_intent._at_ordinal(rows,1),rows[1][0])
        for other in (([0,25,100,20],rows[0][1]), ([0,0,100,20],{'viewport':(0,0,100,22)})):
            with self.assertRaises(desktop_select.bridge.BridgeError):
                row_intent._at_ordinal([rows[0],other],1)
