"""Basalt's registry of providers: keys apart from the file, consent for the cloud, a copy of another registry, daily caps."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.llm.credentials import FileCredentials
from harness.llm.store import Ledger, SecureRegistry, is_local


@pytest.fixture
def reg(tmp_path):
    return SecureRegistry(tmp_path / "llm_providers.json", credentials=FileCredentials(tmp_path / "creds.json"))


def test_a_key_is_kept_in_the_credential_store_and_never_in_the_registry_file(reg, tmp_path):
    prov = reg.add({"type": "openai", "api_key": "sk-secret-123456", "model": "gpt-x"})
    text = (tmp_path / "llm_providers.json").read_text(encoding="utf-8")
    assert "sk-secret-123456" not in text and "key_ref" in text
    assert reg.load()["providers"][0]["api_key"] == "sk-secret-123456"               # it comes back for the one who calls
    assert reg.public()["providers"][0]["has_key"] is True and "sk-secret" not in json.dumps(reg.public())
    reg.update(prov["id"], {"api_key": "sk-rotated-999999"})
    assert "sk-rotated" not in (tmp_path / "llm_providers.json").read_text(encoding="utf-8")
    assert reg.endpoint_for("chat") is None                                            # not assigned yet
    reg.assign("chat", prov["id"])
    assert reg.endpoint_for("chat", carica=False)[1] == "sk-rotated-999999"
    ref = f"{prov['id']}/api_key"
    assert reg.credentials.read(ref) == "sk-rotated-999999"
    reg.remove(prov["id"])
    assert reg.credentials.read(ref) == ""                                             # the secret went with the provider


def test_the_oauth_tokens_are_kept_apart_too(reg, tmp_path):
    prov = reg.add({"type": "openai"})
    data = reg.load()
    data["providers"][0]["oauth"] = {"access_token": "tok-aaa", "refresh_token": "tok-bbb", "account": "me@example.com"}
    reg.save(data)
    text = (tmp_path / "llm_providers.json").read_text(encoding="utf-8")
    assert "tok-aaa" not in text and "tok-bbb" not in text and "me@example.com" in text
    assert reg.load()["providers"][0]["oauth"]["refresh_token"] == "tok-bbb"


def test_personal_data_does_not_go_to_a_cloud_provider_without_a_yes_for_that_function(reg):
    cloud = reg.add({"type": "openai", "api_key": "k" * 20})["id"]
    ok, why = reg.check("executor", cloud)
    assert not ok and "outside this PC" in why and "Executor" in why
    with pytest.raises(ValueError, match="say yes"):
        reg.assign_checked("executor", cloud)
    assert reg.assign_checked("relevance", cloud)["relevance"] == cloud                 # not personal data: fine
    reg.set_consent("executor", True)
    assert reg.assign_checked("executor", cloud)["executor"] == cloud
    reg.set_consent("executor", False)
    assert not reg.check("executor", cloud)[0]                                         # the yes can be taken back
    local = reg.add({"type": "lmstudio"})["id"]
    assert reg.check("executor", local)[0] and reg.check("executor", None)[0]


def test_what_a_provider_cannot_do_is_said_before_it_is_chosen(reg):
    jev = reg.add({"type": "jev", "api_key": "k" * 20})["id"]
    cli = reg.add({"type": "claude_cli"})["id"]
    reg.set_consent("router", True)
    assert "cannot hold a conversation" in reg.check("chat", jev)[1]
    assert "cannot hold a conversation" in reg.check("executor", cli)[1]               # a command-line agent has no tool loop
    assert "say yes" in reg.check("compaction", cli)[1]                                  # text work is fine for it, once the person agrees
    reg.set_consent("compaction", True)
    assert reg.check("compaction", cli)[0]
    assert reg.check("router", jev)[0] and reg.check("relevance", jev)[0]               # Jev judges
    assert not reg.check("router", "missing")[0]


def test_a_failing_cloud_route_falls_back_only_to_a_route_that_is_as_local_or_more(reg):
    cloud = reg.add({"type": "openai", "api_key": "k" * 20})["id"]
    local = reg.add({"type": "basalt"})["id"]
    reg.set_consent("chat", True)
    reg.assign_checked("chat", cloud)
    assert reg.fallback_chain("chat") == [cloud, None]                                  # then the loaded model
    reg.assign_checked("chat", local)
    assert reg.fallback_chain("chat") == [local]
    assert reg.fallback_chain("eval") == [None]


def test_another_registry_is_copied_once_and_nothing_stays_linked(reg, tmp_path):
    other = tmp_path / "shared" / "llm_providers.json"
    other.parent.mkdir()
    other.write_text(json.dumps({"providers": [{"id": "zai", "type": "zai_coding", "name": "Z.AI", "api_key": "zz-key-123456", "model": "glm"},
                                               {"id": "weird", "type": "no_such_type"}],
                                 "assignments": {"chat": "zai", "copilot": "zai"}}), encoding="utf-8")
    assert reg.import_shared(other) == {"imported": 1, "already": False}
    assert reg.import_shared(other) == {"imported": 0, "already": True}                # once
    loaded = reg.load()
    assert [p["id"] for p in loaded["providers"]] == ["zai"] and loaded["assignments"] == {"chat": "zai"}   # a consumer Basalt has not is left out
    assert "zz-key" not in (tmp_path / "llm_providers.json").read_text(encoding="utf-8")               # the copied key went to the store
    other.write_text("{}", encoding="utf-8")
    assert reg.load()["providers"][0]["api_key"] == "zz-key-123456"                    # the source can change or go


def test_the_daily_cap_counts_tokens_and_starts_again_tomorrow(reg, tmp_path):
    now = [1_800_000_000.0]
    ledger = Ledger(tmp_path / "usage.json", clock=lambda: now[0])
    pid = reg.add({"type": "openai", "api_key": "k" * 20})["id"]
    reg.set_cap(pid, 1000)
    prov = reg.load()["providers"][0]
    assert ledger.over_cap(prov) is None
    ledger.record(pid, 600)
    ledger.record(pid, 400)
    assert "1000 tokens" in ledger.over_cap(prov) and ledger.used(pid) == 1000
    now[0] += 86400
    assert ledger.over_cap(prov) is None
    reg.set_cap(pid, 0)
    assert "daily_token_cap" not in reg.load()["providers"][0]


def test_what_is_on_this_pc_is_known():
    assert is_local(None) and is_local({"type": "lmstudio"}) and is_local({"type": "basalt"})
    assert is_local({"type": "openai_compat", "base_url": "http://127.0.0.1:9000/v1"})
    assert not is_local({"type": "openai"}) and not is_local({"type": "openai_compat", "base_url": "https://llm.example.com/v1"})
