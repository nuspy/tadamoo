import pytest
from pathlib import Path

from harness.tools.search import load_providers, search_web, browse_url
from harness.sandbox import ProcessSandbox


def test_search_providers_config():
    providers = load_providers()
    assert len(providers) >= 3
    ids = [p["id"] for p in providers]
    assert "tavily" in ids
    assert "exa" in ids
    assert "firecrawl" in ids


def test_search_and_browse():
    results = search_web("Basalt Blackwell sm_120", provider="local_mock")
    assert len(results) > 0
    assert results[0]["untrusted"] is True

    page = browse_url("https://example.com/docs")
    assert page["url"] == "https://example.com/docs"
    assert page["untrusted"] is True


def test_sandbox_default_deny(tmp_path: Path):
    sb = ProcessSandbox(tmp_path)
    res = sb.run("reg query HKLM\\Software")
    assert res["blocked"] is True
    assert "blocked by sandbox" in res["stderr"]

    res_schtasks = sb.run("schtasks.exe /?")
    assert res_schtasks["blocked"] is True


def test_sandbox_safe_execution(tmp_path: Path):
    sb = ProcessSandbox(tmp_path)
    # Safe echo or python command
    res = sb.run("python -c \"print('sandbox_ok')\"")
    assert res["blocked"] is False
    assert res["exit_code"] == 0
    assert "sandbox_ok" in res["stdout"]
