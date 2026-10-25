"""The online search for a missing capability: the score, the settings, the sources and the candidates they give, against local stand-ins."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_discovery import FakeServices, SHA  # noqa: E402
from harness.discovery import sources as src  # noqa: E402
from harness.discovery.candidates import Candidate, CandidateStore  # noqa: E402
from harness.discovery.finder import Finder, keywords  # noqa: E402
from harness.discovery.scoring import DEFAULT_WEIGHTS, Metrics, lookalike, score  # noqa: E402
from harness.discovery.settings import DiscoverySettings  # noqa: E402

NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def good(**kw):
    base = dict(downloads_week=20_000, last_release=datetime(2026, 10, 10, tzinfo=timezone.utc), first_release=datetime(2024, 1, 1, tzinfo=timezone.utc),
                releases_12m=8, stars=1500, forks=150, open_issues=10, license="MIT", has_repo=True, repo_checked=True, publisher_verified=True, name="x-mcp")
    base.update(kw)
    return Metrics(**base)


# ---- the score ------------------------------------------------------------------------------------------------------

def test_a_used_and_kept_up_package_scores_high_and_an_abandoned_one_low():
    well = score(good(), now=NOW)
    old = score(good(downloads_week=5, last_release=datetime(2023, 1, 1, tzinfo=timezone.utc), releases_12m=0, stars=2, forks=0, open_issues=4,
                     publisher_verified=None), now=NOW)
    assert well.total >= 85 and old.total <= 35 and well.total > old.total
    codes = {w.code for w in old.warnings}
    assert {"stale", "few_downloads"} <= codes and "stale" not in {w.code for w in well.warnings}


def test_more_downloads_stars_and_fresher_releases_never_lower_the_score():
    for field, low, high in (("downloads_week", 10, 100_000), ("stars", 1, 5000), ("releases_12m", 0, 6), ("forks", 0, 400)):
        assert score(good(**{field: low}), now=NOW).total <= score(good(**{field: high}), now=NOW).total, field
    assert score(good(last_release=datetime(2024, 1, 1, tzinfo=timezone.utc)), now=NOW).total < score(good(), now=NOW).total


def test_an_archived_repository_scores_zero_and_says_so():
    s = score(good(archived=True), now=NOW)
    assert s.total == 0 and "archived" in {w.code for w in s.warnings}


def test_the_weights_are_the_persons_and_change_the_result():
    m = good(downloads_week=3, stars=20_000, forks=900, last_release=datetime(2026, 10, 20, tzinfo=timezone.utc))
    by_stars = score(m, {**{k: 0 for k in DEFAULT_WEIGHTS}, "stars": 100}, NOW).total
    by_downloads = score(m, {**{k: 0 for k in DEFAULT_WEIGHTS}, "downloads": 100}, NOW).total
    assert by_stars == 100 and by_downloads < 15


def test_what_a_source_does_not_say_is_left_out_and_what_is_absent_counts_as_nothing():
    only = score(Metrics(downloads_week=100_000, last_release=datetime(2026, 10, 20, tzinfo=timezone.utc), remote=True, has_repo=False, name="svc"), now=NOW)
    parts = {p["key"]: p for p in only.parts}
    assert parts["publisher"]["share"] is None and parts["ratings"]["share"] is None and parts["stars"]["share"] == 0.0
    assert abs(sum(p["points"] for p in only.parts if p["points"] is not None) - only.total) < 1.0
    assert "no_repo" in {w.code for w in only.warnings} and "remote" in {w.code for w in only.warnings}


def test_ratings_count_where_a_source_has_them():
    assert score(good(rating=5.0, rating_count=50), now=NOW).total >= score(good(rating=1.0, rating_count=50), now=NOW).total
    assert score(good(rating=1.0, rating_count=50), now=NOW).total < score(good(), now=NOW).total + 1


def test_warnings_say_keys_install_scripts_and_a_name_close_to_a_famous_one():
    s = score(good(env_required=["API_KEY", "TOKEN"], install_scripts=True, runs_program=True, name="@modelcontextprotocol/server-filesytem"), now=NOW)
    by = {w.code: w for w in s.warnings}
    assert {"needs_keys", "install_scripts", "runs_program", "lookalike"} <= set(by)
    assert "API_KEY, TOKEN" in by["needs_keys"].text() and "server-filesystem" in by["lookalike"].text()
    assert lookalike("@modelcontextprotocol/server-filesystem") is None and lookalike("tavily-mcp") is None
    assert lookalike("@evil/server-filesystem") and lookalike("tavily-mcpp") and lookalike("a-totally-different-thing") is None


def test_the_unauthenticated_github_limit_is_a_warning_not_a_zero():
    s = score(good(repo_checked=False, repo_error="GitHub refused more requests", stars=None, forks=None, open_issues=None), now=NOW)
    assert "github_unchecked" in {w.code for w in s.warnings}
    assert s.total >= 60                                                 # the stars it could not count are left out, not counted as none


# ---- the settings -----------------------------------------------------------------------------------------------------

def test_settings_default_validate_and_never_write_the_base_urls(tmp_path):
    st = DiscoverySettings(tmp_path)
    d = st.load()
    assert d["auto_install"] is False and d["min_score"] == 50 and all(d["sources"].values()) and d["weights"] == {k: float(v) for k, v in DEFAULT_WEIGHTS.items()}
    st.save({"min_score": 70, "weights": {"stars": 0}, "sources": {"pypi": False}, "marketplaces": ["acme/market"], "auto_install": True})
    d = st.load()
    assert d["min_score"] == 70 and d["weights"]["stars"] == 0 and d["sources"]["pypi"] is False and d["marketplaces"] == ["acme/market"] and d["auto_install"] is True
    for bad in ({"min_score": 101}, {"min_score": True}, {"weights": {"nope": 1}}, {"weights": {"stars": -1}}, {"sources": {"x": True}}, {"marketplaces": ["not a repo"]},
                {"auto_install": "yes"}, {"weights": {k: 0 for k in DEFAULT_WEIGHTS}}):
        with pytest.raises(ValueError):
            st.save(bad)
    (tmp_path / "discovery.json").write_text(json.dumps({"base_urls": {"npm": "http://127.0.0.1:1"}, "min_score": 20}), encoding="utf-8")
    assert st.load()["base_urls"] == {"npm": "http://127.0.0.1:1"}
    st.save({"min_score": 30})
    assert json.loads((tmp_path / "discovery.json").read_text(encoding="utf-8"))["base_urls"] == {"npm": "http://127.0.0.1:1"}
    (tmp_path / "discovery.json").write_text("{not json", encoding="utf-8")
    assert st.load()["min_score"] == 50


# ---- the sources --------------------------------------------------------------------------------------------------------

@pytest.fixture
def fake():
    f = FakeServices()
    yield f
    f.close()


def test_a_request_declares_itself_and_carries_no_credential(fake):
    http = src.Http()
    src.RegistryClient(http, fake.bases["registry"]).search("search")
    _, headers = fake.world.requests[-1]
    assert headers["user-agent"].startswith("basalt-capability-search/") and "authorization" not in headers and "cookie" not in headers


def test_a_redirect_to_another_host_is_not_followed(fake):
    fake.world.redirect_registry_to = "http://localhost:%d/elsewhere" % fake.port          # same machine, but another host name
    with pytest.raises(src.SourceError) as e:
        src.RegistryClient(src.Http(), fake.bases["registry"]).search("search")
    assert e.value.code == "redirect"
    assert not any(p.startswith("/elsewhere") for p, _ in fake.world.requests)


def test_an_answer_over_the_cap_is_refused(fake):
    fake.world.big_registry = True
    with pytest.raises(src.SourceError) as e:
        src.RegistryClient(src.Http(max_bytes=100_000), fake.bases["registry"]).search("search")
    assert e.value.code == "too_large"


def test_the_github_limit_has_a_clear_message_and_the_token_goes_only_to_github(fake):
    w = fake.world
    w.add_repo("acme/x")
    gh = src.GithubClient(src.Http(), fake.bases["github"], fake.bases["github_raw"], lambda: "tok123")
    assert gh.repo("acme", "x")["stars"] == 100
    assert fake.world.requests[-1][1]["authorization"] == "Bearer tok123"
    w.github_limited = True
    with pytest.raises(src.RateLimited) as e:
        gh.repo("acme", "x")
    assert "Even with the token" in str(e.value)
    with pytest.raises(src.RateLimited) as e2:
        src.GithubClient(src.Http(), fake.bases["github"], fake.bases["github_raw"]).repo("acme", "x")
    assert "vault" in str(e2.value) and "github" in str(e2.value).lower() and "resets at" in str(e2.value)
    anon = src.GithubClient(src.Http(), fake.bases["github"], fake.bases["github_raw"])
    w.github_limited = False
    anon.repo("acme", "x")
    assert "authorization" not in fake.world.requests[-1][1]
    src.NpmClient(src.Http(), fake.bases["npm"], fake.bases["npm_downloads"]).search("x")
    assert "authorization" not in fake.world.requests[-1][1]


def test_github_addresses_in_the_forms_packages_use():
    for url in ("https://github.com/a/b", "git+https://github.com/a/b.git", "git@github.com:a/b.git", "github:a/b", "https://github.com/a/b/tree/main/src/x"):
        assert src.parse_github_url(url) == ("a", "b"), url
    assert src.parse_github_url("https://gitlab.com/a/b") is None and src.parse_github_url(None) is None


# ---- the candidates -----------------------------------------------------------------------------------------------------

def world(fake):
    w = fake.world
    w.add_repo("acme/search-mcp", stars=2000, forks=200, issues=5)
    w.add_npm("@acme/search-mcp", "2.0.0", weekly=40_000, repo="acme/search-mcp", description="Web search for agents")
    w.add_registry("io.github.acme/search-mcp", "Web search tools", repo_url="https://github.com/acme/search-mcp", version="2.0.0",
                   packages=[{"registryType": "npm", "identifier": "@acme/search-mcp", "version": "2.0.0", "transport": {"type": "stdio"},
                              "environmentVariables": [{"name": "SEARCH_API_KEY", "isRequired": True, "isSecret": True}]}])
    w.add_npm("tiny-search-mcp", "0.0.3", weekly=12, date="2023-02-01T00:00:00Z", history=["2023-01-01T00:00:00Z", "2023-02-01T00:00:00Z"], description="search things")
    w.add_registry("com.example/search", "A hosted search service", remotes=[{"type": "streamable-http", "url": "https://mcp.example.com/mcp"}])
    w.add_registry("com.example/search-keyed", "Needs a header key", remotes=[{"type": "streamable-http", "url": "https://k.example.com/mcp",
                                                                              "headers": [{"name": "Authorization", "isRequired": True, "isSecret": True}]}])
    w.add_registry("io.github.zed/search-args", "Needs an argument", packages=[{"registryType": "npm", "identifier": "search-args", "version": "1.0.0",
                                                                                "packageArguments": [{"type": "positional", "isRequired": True}]}])
    return w


def finder_for(fake, tmp_path, **kw):
    return Finder(DiscoverySettings(tmp_path), CandidateStore(), bases=fake.bases, now=lambda: NOW, **kw)


def test_keywords_drop_the_words_that_carry_nothing():
    assert keywords("I need to search the web with an MCP server") == ["search", "web"]
    assert keywords("a an the") == []


def test_the_search_ranks_what_the_sources_give_and_merges_the_same_package(fake, tmp_path):
    world(fake)
    res = finder_for(fake, tmp_path).search("web search")
    names = [c.package or c.url for c in res.candidates]
    assert names[0] == "@acme/search-mcp" and names.count("@acme/search-mcp") == 1
    top = res.candidates[0]
    assert top.source == "registry" and top.route == "npm" and top.integrity == "sha512-GOOD" and top.bins and top.metrics.stars == 2000
    assert top.keys() == ["SEARCH_API_KEY"] and top.score.total >= 70 and top.id.startswith("c")
    assert "needs_keys" in {w.code for w in top.score.warnings} and top.metrics.publisher_verified is True
    tiny = next(c for c in res.candidates if c.package == "tiny-search-mcp")
    assert tiny.score.total < top.score.total and {"stale", "few_downloads", "no_repo"} <= {w.code for w in tiny.score.warnings}
    assert any(c.route == "remote" and c.url == "https://mcp.example.com/mcp" for c in res.candidates)
    assert all("keyed" not in (c.url or "") and c.package != "search-args" for c in res.candidates) and res.skipped >= 2
    assert len(res.candidates) <= 5


def test_a_source_turned_off_is_not_asked_and_its_candidates_are_not_offered(fake, tmp_path):
    world(fake)
    DiscoverySettings(tmp_path).save({"sources": {"npm": False, "github": False, "skills": False}})
    fake.world.requests.clear()
    res = finder_for(fake, tmp_path).search("web search")
    asked = {p.split("/")[1] for p, _ in fake.world.requests}
    assert "npm" not in asked and "npmdl" not in asked and "gh" not in asked and "registry" in asked
    assert [c.route for c in res.candidates] == ["remote"]


def test_when_github_is_limited_the_candidates_come_with_a_warning(fake, tmp_path):
    world(fake)
    fake.world.github_limited = True
    res = finder_for(fake, tmp_path).search("web search")
    top = res.candidates[0]
    assert top.metrics.repo_checked is False and "github_unchecked" in {w.code for w in top.score.warnings}
    assert top.metrics.stars is None


def test_the_need_with_no_usable_word_asks_nothing(fake, tmp_path):
    fake.world.requests.clear()
    res = finder_for(fake, tmp_path).search("an the of")
    assert res.candidates == [] and "need" in res.errors and fake.world.requests == []


def test_skills_are_found_in_repositories_with_skill_folders_and_pinned_to_a_commit(fake, tmp_path):
    w = fake.world
    w.add_repo("acme/skills", stars=900, forks=80, topics=["agent-skills"])
    w.repo_search["topic:agent-skills"] = [w.repos["acme/skills"]]
    w.trees["acme/skills"] = [{"path": "skills/pdf/SKILL.md", "type": "blob", "mode": "100644", "sha": "s1", "size": 90},
                              {"path": "skills/pdf/scripts/extract.py", "type": "blob", "mode": "100644", "sha": "s2", "size": 10},
                              {"path": "skills/docx/SKILL.md", "type": "blob", "mode": "100644", "sha": "s3", "size": 50}]
    w.files[("acme/skills", "skills/pdf/SKILL.md")] = b"---\nname: pdf-tables\ndescription: Extract tables from a PDF file\n---\nDo it.\n"
    w.files[("acme/skills", "skills/docx/SKILL.md")] = b"---\nname: docx-writer\ndescription: Write Word documents\n---\nDo it.\n"
    res = finder_for(fake, tmp_path).search("extract tables from pdf")
    sk = [c for c in res.candidates if c.kind == "skill"]
    assert [c.name for c in sk] == ["pdf-tables"]
    assert sk[0].commit == SHA and sk[0].path == "skills/pdf" and sk[0].route == "skill" and sk[0].metrics.stars == 900
    assert "skill_files" in {x.code for x in sk[0].score.warnings}                # it carries a script
    assert sk[0].check() is None


def test_a_marketplace_the_person_lists_is_read_for_plugins_that_match(fake, tmp_path):
    w = fake.world
    w.add_repo("acme/market", stars=50)
    w.files[("acme/market", ".claude-plugin/marketplace.json")] = json.dumps({"plugins": [
        {"name": "pdf-kit", "description": "PDF helpers", "source": "./plugins/pdf-kit"}, {"name": "other", "description": "x", "source": "./plugins/other"}]}).encode()
    w.trees["acme/market"] = [{"path": "plugins/pdf-kit/skills/pdf/SKILL.md", "type": "blob", "mode": "100644", "sha": "m1", "size": 40},
                              {"path": "plugins/other/skills/zzz/SKILL.md", "type": "blob", "mode": "100644", "sha": "m2", "size": 40}]
    w.files[("acme/market", "plugins/pdf-kit/skills/pdf/SKILL.md")] = b"---\nname: pdf-reader\ndescription: Read PDF files\n---\nx\n"
    DiscoverySettings(tmp_path).save({"marketplaces": ["acme/market"], "sources": {"registry": False, "npm": False}})
    res = finder_for(fake, tmp_path).search("read pdf")
    assert [(c.name, c.source) for c in res.candidates] == [("pdf-reader", "marketplace")]


def test_a_candidate_whose_fields_could_become_an_option_or_a_path_is_not_usable():
    def c(**kw):
        return Candidate(**{**dict(kind="mcp", name="ok", source="registry", route="npm", package="pkg", version="1.0.0"), **kw})
    assert c().check() is None
    for bad in (c(package="--registry=http://evil"), c(package="pkg;rm -rf /"), c(version="latest"), c(version="1.0.0 --foo"), c(name="../x"),
                c(args=["x" * 500]), c(env=[{"name": "A B"}]), c(route="pypi", package="-e", version="1.0"),
                c(route="skill", repo="a/b", commit="main", path="x"), c(route="skill", repo="a/b", commit=SHA, path="../x"),
                c(route="skill", repo="a/b", commit=SHA, path="/etc"), c(route="remote", url="file:///etc/passwd")):
        assert bad.check(), bad
