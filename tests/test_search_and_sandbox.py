import pytest
from pathlib import Path

from harness.tools.search import (ProviderUnavailable, clear_backends, load_providers,
                                  register_backend, search_web)
from harness.sandbox import ProcessSandbox


def test_search_providers_config():
    providers = load_providers()
    assert len(providers) >= 3
    ids = [p["id"] for p in providers]
    assert "tavily" in ids
    assert "exa" in ids


@pytest.fixture(autouse=True)
def _no_backends():
    clear_backends()
    yield
    clear_backends()


def test_unconnected_provider_is_an_error_not_fake_data(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    with pytest.raises(ProviderUnavailable, match="not connected"):
        search_web("Basalt Blackwell", provider="tavily")
    with pytest.raises(ProviderUnavailable, match="unknown provider"):
        search_web("x", provider="nope")


def test_connected_backend_results_are_marked_untrusted(monkeypatch):
    register_backend("local_mock", search=lambda q, n: [{"title": q, "snippet": "s"}])
    results = search_web("Basalt Blackwell sm_120", provider="local_mock")
    assert results[0]["untrusted"] is True and results[0]["source"] == "local_mock"

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    register_backend("exa", search=lambda q, n: [])
    with pytest.raises(ProviderUnavailable, match="EXA_API_KEY"):
        search_web("x", provider="exa")


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
