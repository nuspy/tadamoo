"""--remote-admin: a request from another machine that carries the server key is handled as one from this PC; without the option, with a wrong key or with no key it is not."""
import email.message
import unittest
from types import SimpleNamespace

from serve import handler


def request(remote_admin, key, given, address="203.0.113.5"):
    svc = SimpleNamespace(api_key=key, remote_admin=remote_admin, keys=None, retired_keys=())
    cls = handler.make_handler(svc)
    req = cls.__new__(cls)
    req.client_address = (address, 4000)
    req.headers = email.message.Message()
    if given is not None:
        req.headers["Authorization"] = "Bearer " + given
    req._remote_admin()
    return req.client_address[0]


class RemoteAdminTest(unittest.TestCase):
    def test_the_key_makes_a_remote_request_count_as_this_pc_only_when_the_option_is_on(self):
        self.assertEqual(request(True, "k-1", "k-1"), "127.0.0.1")
        self.assertEqual(request(True, "k-1", "wrong"), "203.0.113.5")
        self.assertEqual(request(True, "k-1", None), "203.0.113.5")
        self.assertEqual(request(False, "k-1", "k-1"), "203.0.113.5")

    def test_a_server_with_no_key_never_does_it(self):
        self.assertEqual(request(True, "", "anything"), "203.0.113.5")


if __name__ == "__main__":
    unittest.main()
