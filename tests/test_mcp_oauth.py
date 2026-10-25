"""Signing in to a remote MCP server: discovery, registration, PKCE, tokens in the credential store, refresh, and what is refused."""
import base64
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.llm.credentials import FileCredentials
from harness.mcp_oauth import AuthRequired, McpOAuth, OAuthError, OAuthStore, canonical, parse_challenge

REDIRECT = "http://127.0.0.1:7232/api/mcp/auth/callback"


class World:
    """An MCP server, its authorization server and everything between, on one local port."""

    def __init__(self):
        self.challenge = None
        self.registered = []
        self.tokens = {"AT1": "RT1"}
        self.requests = []
        self.n = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                p = outer.port
                outer.requests.append(("GET", self.path))
                if self.path == "/.well-known/oauth-protected-resource/mcp":
                    return self._send(200, {"resource": f"http://127.0.0.1:{p}/mcp", "authorization_servers": [f"http://127.0.0.1:{p}/as"]})
                if self.path == "/.well-known/oauth-authorization-server/as":
                    return self._send(200, {"issuer": f"http://127.0.0.1:{p}/as", "authorization_endpoint": f"http://127.0.0.1:{p}/as/authorize",
                                            "token_endpoint": f"http://127.0.0.1:{p}/as/token", "registration_endpoint": f"http://127.0.0.1:{p}/as/register",
                                            "code_challenge_methods_supported": ["S256"]})
                self._send(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append(("POST", self.path))
                p = outer.port
                if self.path == "/mcp":
                    token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                    if token in outer.tokens:
                        return self._send(200, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
                    return self._send(401, {}, {"WWW-Authenticate": f'Bearer resource_metadata="http://127.0.0.1:{p}/.well-known/oauth-protected-resource/mcp", scope="mcp.read"'})
                if self.path == "/as/register":
                    reg = json.loads(body)
                    outer.registered.append(reg)
                    return self._send(201, {"client_id": "cid-1", **{k: reg[k] for k in ("redirect_uris",)}})
                if self.path == "/as/token":
                    f = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
                    outer.last_token_request = f
                    if f.get("grant_type") == "authorization_code":
                        if f.get("code") != "good-code":
                            return self._send(400, {"error": "invalid_grant"})
                        digest = base64.urlsafe_b64encode(hashlib.sha256(f["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                        if digest != outer.challenge:
                            return self._send(400, {"error": "invalid_grant", "error_description": "PKCE does not match"})
                        return self._send(200, {"access_token": "AT1", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "RT1", "scope": "mcp.read"})
                    if f.get("grant_type") == "refresh_token":
                        current = [rt for rt in outer.tokens.values()]
                        if f.get("refresh_token") not in current:
                            return self._send(400, {"error": "invalid_grant"})
                        outer.n += 1
                        outer.tokens = {f"AT{outer.n + 1}": f"RT{outer.n + 1}"}
                        return self._send(200, {"access_token": f"AT{outer.n + 1}", "token_type": "bearer", "expires_in": 3600, "refresh_token": f"RT{outer.n + 1}"})
                self._send(404, {})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def challenge_header(self):
        return f'Bearer resource_metadata="http://127.0.0.1:{self.port}/.well-known/oauth-protected-resource/mcp", scope="mcp.read"'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()


@pytest.fixture
def parts(tmp_path, world):
    creds = FileCredentials(tmp_path / "c.json")
    store = OAuthStore(tmp_path / "oauth.json", creds)
    clock = Clock()
    return McpOAuth(world.url, store, REDIRECT, clock=clock), store, clock, creds, tmp_path


def sign_in(oauth, world, code="good-code"):
    started = oauth.begin(world.challenge_header())
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query).items()}
    world.challenge = q["code_challenge"]
    oauth.complete(code, started["state"])
    return q


def test_the_challenge_and_the_address_are_read_the_way_the_specification_says():
    h = 'Bearer realm="x", resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource", scope="a b", error=invalid_token'
    assert parse_challenge(h) == {"realm": "x", "resource_metadata": "https://mcp.example.com/.well-known/oauth-protected-resource", "scope": "a b", "error": "invalid_token"}
    assert parse_challenge("Basic realm=x") == {} and parse_challenge("") == {}
    assert canonical("HTTPS://MCP.Example.com:443/mcp/#frag") == "https://mcp.example.com/mcp" and canonical("https://a.com/") == "https://a.com"
    assert canonical("http://127.0.0.1:7000/x?y=1") == "http://127.0.0.1:7000/x"


def test_signing_in_asks_for_everything_the_specification_requires(parts, world):
    oauth, store, clock, creds, tmp = parts
    q = sign_in(oauth, world)
    assert q["response_type"] == "code" and q["client_id"] == "cid-1" and q["redirect_uri"] == REDIRECT
    assert q["code_challenge_method"] == "S256" and q["resource"] == world.url and q["scope"] == "mcp.read" and len(q["state"]) >= 20
    assert world.registered == [{"client_name": "Basalt", "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
                                 "response_types": ["code"], "token_endpoint_auth_method": "none"}]
    assert world.last_token_request["resource"] == world.url and world.last_token_request["client_id"] == "cid-1"
    assert oauth.signed_in() and oauth.access_token() == "AT1"


def test_the_tokens_are_in_the_credential_store_and_not_in_the_file(parts, world):
    oauth, store, clock, creds, tmp = parts
    sign_in(oauth, world)
    text = (tmp / "oauth.json").read_text(encoding="utf-8")
    assert "AT1" not in text and "RT1" not in text and "cid-1" in text and store.get(world.url)["token_endpoint"].endswith("/as/token")
    assert "AT1" in (tmp / "c.json").read_text(encoding="utf-8")


def test_the_client_is_registered_once_and_a_second_sign_in_reuses_it(parts, world):
    oauth, store, clock, creds, tmp = parts
    sign_in(oauth, world)
    sign_in(oauth, world)
    assert len(world.registered) == 1


def test_a_code_without_its_state_or_a_second_time_or_too_late_is_refused(parts, world):
    oauth, store, clock, creds, tmp = parts
    started = oauth.begin(world.challenge_header())
    world.challenge = "irrelevant"
    with pytest.raises(OAuthError, match="not started here"):
        oauth.complete("good-code", "made-up-state")
    clock.now += 700
    with pytest.raises(OAuthError, match="too long"):
        oauth.complete("good-code", started["state"])
    started = oauth.begin(world.challenge_header())
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(started["authorization_url"]).query).items()}
    world.challenge = q["code_challenge"]
    oauth.complete("good-code", started["state"])
    with pytest.raises(OAuthError, match="not started here"):
        oauth.complete("good-code", started["state"])                          # a state works once


def test_a_wrong_code_or_a_broken_pkce_is_the_servers_refusal_in_its_words(parts, world):
    oauth, store, clock, creds, tmp = parts
    with pytest.raises(OAuthError, match="invalid_grant"):
        sign_in(oauth, world, code="bad-code")
    started = oauth.begin(world.challenge_header())
    world.challenge = "someone-elses-challenge"
    with pytest.raises(OAuthError, match="PKCE does not match"):
        oauth.complete("good-code", started["state"])
    assert not oauth.signed_in()


def test_a_token_that_runs_out_is_renewed_and_the_new_refresh_token_replaces_the_old(parts, world):
    oauth, store, clock, creds, tmp = parts
    sign_in(oauth, world)
    clock.now += 3600 - 30                                  # inside the margin
    assert oauth.access_token() == "AT2"
    assert store.secret(world.url, "refresh") == "RT2" and oauth.access_token() == "AT2"
    clock.now += 4000
    assert oauth.access_token() == "AT3"


def test_a_refresh_the_server_refuses_means_signing_in_again_and_forgets_the_tokens(parts, world):
    oauth, store, clock, creds, tmp = parts
    sign_in(oauth, world)
    world.tokens = {"AT-other": "RT-other"}                 # the server no longer knows our refresh token
    clock.now += 4000
    with pytest.raises(AuthRequired):
        oauth.access_token()
    assert not oauth.signed_in() and store.get(world.url)["client_id"] == "cid-1"       # the registration is kept


def test_a_server_never_signed_in_to_has_no_token_and_signing_out_forgets(parts, world):
    oauth, store, clock, creds, tmp = parts
    assert oauth.access_token() is None and not oauth.signed_in()
    sign_in(oauth, world)
    oauth.sign_out()
    assert oauth.access_token() is None and "AT1" not in (tmp / "c.json").read_text(encoding="utf-8")


def test_a_long_token_is_kept_in_pieces_and_comes_back_whole(parts, world):
    oauth, store, clock, creds, tmp = parts
    long = "x" * 5000 + "end"
    store.secret(world.url, "access", long)
    assert store.secret(world.url, "access") == long and len(creds.read(f"mcp/{store.key(world.url)}/access")) < 10
    store.secret(world.url, "access", "short")
    assert store.secret(world.url, "access") == "short"
    store.secret(world.url, "access", None)
    assert store.secret(world.url, "access") == "" and "x" * 50 not in (tmp / "c.json").read_text(encoding="utf-8")


# -- what comes from a server that is not trusted ------------------------------------------------------------------------------


def stub(routes):
    def http(method, url, headers, data, timeout):
        for key, (status, body) in routes.items():
            if url == key:
                return status, {}, json.dumps(body).encode()
        return 404, {}, b"{}"
    return http


def remote(tmp_path, routes, resource="https://mcp.example.com/mcp"):
    return McpOAuth(resource, OAuthStore(tmp_path / "o.json", FileCredentials(tmp_path / "c.json")), REDIRECT, http=stub(routes))


PRM = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"


def good_meta(issuer="https://auth.example.com", **over):
    return {"issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
            "registration_endpoint": issuer + "/register", "code_challenge_methods_supported": ["S256"], **over}


def test_a_server_that_sends_us_to_a_private_address_is_not_followed(tmp_path):
    for bad in ("https://10.0.0.5", "https://192.168.1.9/as", "https://169.254.169.254", "https://localhost", "https://nas.local", "http://auth.example.com"):
        routes = {PRM: (200, {"resource": "https://mcp.example.com/mcp", "authorization_servers": [bad]})}
        with pytest.raises(OAuthError, match="private network|not an https"):
            remote(tmp_path, routes).discover()
    routes = {PRM: (200, {"resource": "https://mcp.example.com/mcp", "authorization_servers": ["https://auth.example.com"]}),
              "https://auth.example.com/.well-known/oauth-authorization-server": (200, good_meta(token_endpoint="https://10.1.1.1/token"))}
    with pytest.raises(OAuthError, match="private network"):
        remote(tmp_path, routes).discover()


def test_metadata_for_another_resource_or_issuer_or_without_s256_is_refused(tmp_path):
    prm_ok = {"resource": "https://mcp.example.com/mcp", "authorization_servers": ["https://auth.example.com"]}
    with pytest.raises(OAuthError, match="another address"):
        remote(tmp_path, {PRM: (200, {**prm_ok, "resource": "https://evil.example.com/mcp"})}).discover()
    well = "https://auth.example.com/.well-known/oauth-authorization-server"
    with pytest.raises(OAuthError, match="another issuer"):
        remote(tmp_path, {PRM: (200, prm_ok), well: (200, good_meta("https://other.example.com"))}).discover()
    with pytest.raises(OAuthError, match="S256"):
        remote(tmp_path, {PRM: (200, prm_ok), well: (200, good_meta(code_challenge_methods_supported=["plain"]))}).discover()
    found = remote(tmp_path, {PRM: (200, prm_ok), well: (200, good_meta())}).discover()
    assert found["token_endpoint"] == "https://auth.example.com/token" and found["registration_endpoint"] == "https://auth.example.com/register"


def test_discovery_tries_the_openid_documents_when_the_first_is_missing(tmp_path):
    prm_ok = {"resource": "https://mcp.example.com/mcp", "authorization_servers": ["https://auth.example.com/tenant"]}
    routes = {PRM: (200, prm_ok), "https://auth.example.com/tenant/.well-known/openid-configuration": (200, good_meta("https://auth.example.com/tenant"))}
    assert remote(tmp_path, routes).discover()["issuer"] == "https://auth.example.com/tenant"


def test_a_plain_http_server_is_refused_unless_it_is_on_this_pc(tmp_path):
    with pytest.raises(OAuthError, match="https"):
        McpOAuth("http://mcp.example.com/mcp", OAuthStore(tmp_path / "o.json", FileCredentials(tmp_path / "c.json")), REDIRECT)
    McpOAuth("http://127.0.0.1:9/mcp", OAuthStore(tmp_path / "o.json", FileCredentials(tmp_path / "c.json")), REDIRECT)


def test_a_server_that_does_not_register_clients_says_so(tmp_path):
    meta = good_meta()
    del meta["registration_endpoint"]
    routes = {PRM: (200, {"resource": "https://mcp.example.com/mcp", "authorization_servers": ["https://auth.example.com"]}),
              "https://auth.example.com/.well-known/oauth-authorization-server": (200, meta)}
    with pytest.raises(OAuthError, match="no client id"):
        remote(tmp_path, routes).begin()


# -- the client that uses it ---------------------------------------------------------------------------------------------------


def test_the_http_client_sends_the_token_asks_to_sign_in_without_one_and_renews_a_token_that_ran_out(parts, world):
    from harness.mcp_client import MCPAuthRequired, MCPHttpClient
    oauth, store, clock, creds, tmp = parts
    client = MCPHttpClient(world.url, auth=oauth)
    with pytest.raises(MCPAuthRequired) as e:
        client.notify("ping")                                                          # not signed in: the server says 401
    assert "sign in" in str(e.value) and "resource_metadata" in e.value.www_authenticate
    sign_in(oauth, world)
    client.notify("ping")                                                               # the token goes along, the server accepts it
    world.tokens = {"AT-new": "RT1"}                                                    # the server dropped our access token, kept the refresh
    client.notify("ping")                                                               # 401, refreshed once, accepted
    assert store.secret(world.url, "access").startswith("AT")
    world.tokens = {"AT-x": "RT-x"}                                                     # nothing of ours is known any more
    with pytest.raises(MCPAuthRequired):
        client.notify("ping")


def test_a_static_token_and_no_auth_still_work_as_before(world):
    from harness.mcp_client import MCPAuthRequired, MCPHttpClient
    world.tokens = {"fixed": "x"}
    MCPHttpClient(world.url, token="fixed").notify("ping")
    with pytest.raises(MCPAuthRequired):
        MCPHttpClient(world.url, token="wrong").notify("ping")
