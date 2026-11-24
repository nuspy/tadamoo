"""The server's start lines: the drafting the model runs, and an idle sleep said once."""
import unittest

from serve.config import shared_line, with_spec


class StartLines(unittest.TestCase):
    def test_spec_is_the_engine_s_window(self):
        self.assertEqual(with_spec(["ctx=1048576", "pool=12"], 3), ["ctx=1048576", "pool=12", "spec=3"])
        self.assertEqual(with_spec(["spec=7", "pool=8"], 0), ["pool=8", "spec=0"])    # the flag as given leaves
        self.assertEqual(with_spec(["pool=8"], None), ["pool=8"])                     # an engine with no answer

    def test_a_shared_sleep_is_said_once(self):
        self.assertEqual(shared_line(["reserve=500MiB"], [300, 300]), ("reserve=500MiB sleep=300s", True))
        self.assertEqual(shared_line(["reserve=500MiB"], [300, 60]), ("reserve=500MiB", False))
        self.assertEqual(shared_line([], [None, None]), ("", False))
        self.assertEqual(shared_line([], [300]), ("", False))                        # one model says it itself


if __name__ == "__main__":
    unittest.main()
