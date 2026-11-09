"""The control server's token: the exact Bearer value opens the routes, any other value (a prefix, a longer value, another case) does
not, and /health stays open. The comparison is constant-time (hmac.compare_digest)."""
from unittest import mock

from callbridge import control_server
from callbridge.control_server import create_app
from telephony_fakes import bridge_stub, config

TOKEN = "t0ken-2"


def test_only_the_exact_token_opens_the_routes(tmp_path):
    client = create_app(config(tmp_path, control_auth_token=TOKEN), bridge=bridge_stub(), ptt=None).test_client()
    with mock.patch.object(control_server.hmac, "compare_digest", wraps=control_server.hmac.compare_digest) as compared:
        assert client.get("/call/approvals", headers={"Authorization": "Bearer " + TOKEN}).status_code == 200
        assert compared.called
    for wrong in ("", "Bearer ", "Bearer t0ken", "bearer " + TOKEN, "Bearer " + TOKEN + "x", "Bearer \xff"):
        assert client.get("/call/approvals", headers={"Authorization": wrong} if wrong else {}).status_code == 401, wrong
    assert client.get("/health").status_code == 200
