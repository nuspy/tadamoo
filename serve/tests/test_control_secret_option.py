"""The control secret is off unless the person asks for it with --control-secret."""
import unittest

from serve.config import build_parser


class ControlSecretOption(unittest.TestCase):
    def test_it_is_off_by_default_and_on_when_asked(self):
        self.assertFalse(build_parser().parse_args(["--model", "m.basalt"]).control_secret)
        self.assertTrue(build_parser().parse_args(["--model", "m.basalt", "--control-secret"]).control_secret)


if __name__ == "__main__":
    unittest.main()
