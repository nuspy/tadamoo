"""Logins from a browser's or a password manager's export: one entry per site and user, each sent only to its own site."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.vault import Vault
from harness.vault_import import host_of, parse_logins

CHROME = """name,url,username,password,note
Example,https://www.example.com/login,mario@example.com,pw-one-1234,
Bank,https://bank.example.org:8443/signin,mrossi,pw-two-5678,
No site,,someone,pw-three,
No password,https://nopass.example.net,someone,,
Local,http://localhost:3000/,dev,pw-local,
"""


def test_the_columns_of_a_browser_export_become_logins_for_their_own_sites():
    parsed = parse_logins(CHROME)
    got = {e["id"]: e for e in parsed["entries"]}
    assert set(got) == {"example-com", "bank-example-org", "localhost"}
    assert got["example-com"]["hosts"] == ["example.com"] and got["example-com"]["fields"] == {"username": "mario@example.com", "password": "pw-one-1234"}
    assert got["bank-example-org"]["hosts"] == ["bank.example.org"] and got["bank-example-org"]["label"] == "Bank"
    assert parsed["skipped"] == {"no_site": 1, "no_password": 1, "duplicate": 0}


def test_other_exports_and_separators_are_read():
    firefox = "url;username;password\nhttps://a.example.com;u1;p1\n"
    bitwarden = "folder,favorite,type,name,login_uri,login_username,login_password\n,,login,Site B,https://b.example.com,u2,p2\n"
    assert parse_logins(firefox)["entries"][0]["fields"] == {"username": "u1", "password": "p1"}
    b = parse_logins(bitwarden)["entries"][0]
    assert b["hosts"] == ["b.example.com"] and b["label"] == "Site B" and b["fields"]["username"] == "u2"


def test_a_file_without_the_needed_columns_is_refused_in_words():
    for bad in ("", "name,note\nx,y\n", "url,note\nhttps://a.example,x\n"):
        with pytest.raises(ValueError):
            parse_logins(bad)


def test_two_users_of_one_site_get_two_entries_and_a_repeat_is_skipped():
    text = "url,username,password\nhttps://a.example.com,u1,p1\nhttps://a.example.com,u2,p2\nhttps://a.example.com,u1,p1\n"
    parsed = parse_logins(text)
    assert [e["id"] for e in parsed["entries"]] == ["a-example-com", "a-example-com-2"] and parsed["skipped"]["duplicate"] == 1


def test_importing_twice_adds_nothing_the_second_time_and_the_value_is_not_in_the_listing():
    with TemporaryDirectory() as d:
        v = Vault(d, key=bytes(range(32)))
        first = v.put_many(parse_logins(CHROME, v.login_facts())["entries"])
        assert len(first["added"]) == 3 and first["refused"] == []
        again = parse_logins(CHROME, v.login_facts())
        assert again["entries"] == [] and again["skipped"]["duplicate"] == 3
        assert "pw-one" not in str(v.entries())
        assert v.resolve("{{vault:example-com.password}}", hosts=["example.com"]) == "pw-one-1234"


def test_host_of_reads_what_people_paste():
    assert host_of("https://www.Example.com:8080/a?b=1") == "example.com" and host_of("example.com/login") == "example.com" and host_of("") == ""
