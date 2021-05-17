import unittest

from .tools import pairwise, iter_show_end


class TestTools(unittest.TestCase):
    def test_pairwise(self):
        self.assertEqual(list(pairwise(["a", "b"])), [("a", "b")])
        self.assertEqual(list(pairwise(["a", "b", "c"])), [("a", "b"), ("b", "c")])
        self.assertEqual(list(pairwise(["a"])), [])

    def test_iter_show_end(self):
        self.assertEqual(list(iter_show_end(["a"])), [("a", True)])
        self.assertEqual(list(iter_show_end(["a", "b"])), [("a", False), ("b", True)])
