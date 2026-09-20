import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkshot.recognition.text_order import order_text_rows


def row(text, x, top, bottom):
    return dict(text=text, box=[[x, top], [x+10, top], [x+10, bottom], [x, bottom]])


class TextOrderTests(unittest.TestCase):
    def test_different_box_heights_same_line_preserve_objects(self):
        left = row('重複不可', 10, 103, 124)
        right = row('レッスン中1回', 90, 99, 125)
        source = [right, left]
        before = copy.deepcopy(source)
        result = order_text_rows(source)
        self.assertIs(result[0], left)
        self.assertIs(result[1], right)
        self.assertEqual(source, before)

    def test_separate_lines_and_shuffled_input(self):
        a, b = row('a', 10, 10, 30), row('b', 50, 12, 29)
        c, d = row('c', 5, 40, 60), row('d', 70, 42, 59)
        for source in ([d, b, c, a], [b, a, d, c]):
            self.assertEqual(order_text_rows(source), [a, b, c, d])

    def test_tall_member_does_not_extend_anchor_to_next_line(self):
        a = row('a', 10, 0, 20)
        tall = row('tall', 50, 8, 45)
        next_line = row('next', 0, 32, 52)
        self.assertEqual(order_text_rows([next_line, tall, a]), [a, tall, next_line])

    def test_empty_and_zero_height(self):
        self.assertEqual(order_text_rows([]), [])
        a, zero = row('a', 20, 0, 20), row('zero', 0, 10, 10)
        self.assertEqual(order_text_rows([zero, a]), [a, zero])


if __name__ == '__main__':
    unittest.main()
