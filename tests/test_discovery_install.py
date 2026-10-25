"""Installing what the person chose: fixed commands only, integrity checked, registered as approved, removable. No real install runs."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_discovery import FakeServices, SHA, git_blob_sha  # noqa: E402
from harness.capabilities import CapabilityIndex  # noqa: E402
from harness.discovery import sources as src  # noqa: E402
from harness.discovery.candidates import Candidate, CandidateStore  # noqa: E402
from harness.discovery.installed import InstalledStore  # noqa: E402
from harness.discovery.installer import BOOT, InstallError, Installer, IntegrityError, Run, UnknownCandidate  # noqa: E402
from harness.discovery.scoring import Metrics, score  # noqa: E402

WIN = os.name == "nt"


class Runner:
    """Stands in for npm, uv and pip: records each command and makes the files the real one would."""

    def __init__(self, integrity="sha512-GOOD", wheel=b"wheel-bytes", fail=None):
        self.calls, self.integrity, self.wheel, self.fail = [], integrity, wheel, fail

    def __call__(self, command, cwd, timeout):
        self.calls.append(list(command))
        if self.fail and self.fail(command):
            return Run(1, "", "boom: something broke")
        if "install" in command and command[0].endswith(("npm", "npm.cmd", "npm.exe")) or command[0] == "npm":
            folder = Path(command[command.index("--prefix") + 1])
            package = command[command.index("--prefix") + 2].rsplit("@", 1)[0]
            bins = folder / "node_modules" / ".bin"
            bins.mkdir(parents=True)
            (bins / (package.rsplit("/", 1)[-1] + (".cmd" if WIN else ""))).write_text("x")
            (folder / "package-lock.json").write_text(json.dumps({"packages": {f"node_modules/{package}": {"integrity": self.integrity}}}))
            return Run(0)
        if command[1:3] == ["-m", "pip"] and command[3] == "--version":
            return Run(0, "pip 24")
        if command[1:3] == ["-m", "pip"] and command[3] == "download":
            out = Path(command[command.index("-d") + 1])
            out.mkdir(parents=True)
            (out / "fastpkg-1.2.0-py3-none-any.whl").write_bytes(self.wheel)
            return Run(0)
        if "install" in command and "--target" in command:
            site = Path(command[command.index("--target") + 1])
            info = site / "fastpkg-1.2.0.dist-info"
            info.mkdir(parents=True)
            (info / "entry_points.txt").write_text("[console_scripts]\nfastpkg = fastpkg_mod:main\n")
            (site / "fastpkg_mod.py").write_text("import sys\ndef main():\n    print('hello', sys.argv[1:])\n    return 0\n")
            return Run(0)
        return Run(0)


@pytest.fixture
def env(tmp_path):
    ws, caps = tmp_path / "ws", tmp_path / "caps"
    ws.mkdir()
    index = CapabilityIndex(caps, ws, home=tmp_path / "home", appdata=tmp_path / "none")
    store = CandidateStore()
    runner = Runner()
    inst = Installer(caps, store, index=index, runner=runner, which=lambda n: {"npm": "npm", "uv": None}.get(n))
    return type("Env", (), dict(caps=caps, index=index, store=store, runner=runner, inst=inst, tmp=tmp_path))


def npm_candidate(**kw):
    base = dict(kind="mcp", name="search-mcp", source="registry", route="npm", package="@acme/search-mcp", version="2.0.0", integrity="sha512-GOOD",
                bins={"search-mcp": "build/index.js"}, args=["--mode", "fast"], description="Web search", repo="acme/search-mcp",
                env=[{"name": "SEARCH_API_KEY", "required": True, "secret": True}], metrics=Metrics(downloads_week=100))
    base.update(kw)
    c = Candidate(**base)
    c.score = score(c.metrics)
    return c


def test_an_npm_package_is_installed_with_the_fixed_command_checked_and_approved(env):
    c = env.store.add(npm_candidate(env=[]))
    record = env.inst.install(c.id)
    folder = env.caps / "mcp" / "search-mcp"
    assert env.runner.calls == [["npm", "install", "--prefix", str(folder), "@acme/search-mcp@2.0.0", "--ignore-scripts", "--no-audit", "--no-fund"]]
    shim = folder / "node_modules" / ".bin" / ("search-mcp" + (".cmd" if WIN else ""))
    assert record["command"] == [str(shim), "--mode", "fast"] and "npx" not in json.dumps(record["command"])
    entry = env.index.get("mcp:search-mcp")
    assert entry["origin"] == "basalt" and entry["state"] == "approved" and entry["sha"] == record["sha"]
    assert env.index.target("mcp:search-mcp") == [str(shim), "--mode", "fast"]
    assert [h["id"] for h in env.index.search("web search")] == ["mcp:search-mcp"]
    stored = json.loads((env.caps / "installed.json").read_text(encoding="utf-8"))["items"]["mcp:search-mcp"]
    assert stored["integrity"] == "sha512-GOOD" and stored["env"] == [] and stored["source"] == "registry" and stored["version"] == "2.0.0"


def test_the_variables_a_server_asks_for_are_recorded_by_name_and_vault_entry_never_by_value(env):
    env.inst.install(env.store.add(npm_candidate()).id)
    spec = InstalledStore(env.caps).get("mcp:search-mcp")["env"]
    assert spec == [{"name": "SEARCH_API_KEY", "required": True, "secret": True, "entry": "SEARCH_API_KEY", "value": ""}]
    with pytest.raises(ValueError, match="SEARCH_API_KEY"):
        env.index.target("mcp:search-mcp")                        # no vault: the server is not started without its key


def test_a_different_hash_than_the_registry_published_blocks_and_deletes(env):
    env.runner.integrity = "sha512-TAMPERED"
    c = env.store.add(npm_candidate())
    with pytest.raises(IntegrityError):
        env.inst.install(c.id)
    assert not (env.caps / "mcp" / "search-mcp").exists() and env.inst.records.load() == {}
    assert env.index.get("mcp:search-mcp") is None


def test_a_candidate_without_a_published_hash_is_not_installed(env):
    c = env.store.add(npm_candidate(integrity=""))
    with pytest.raises(IntegrityError):
        env.inst.install(c.id)
    assert not (env.caps / "mcp" / "search-mcp").exists()


def test_a_failed_install_leaves_nothing_behind(env):
    env.runner.fail = lambda cmd: True
    c = env.store.add(npm_candidate())
    with pytest.raises(InstallError, match="boom"):
        env.inst.install(c.id)
    assert not (env.caps / "mcp" / "search-mcp").exists() and env.inst.records.load() == {}


def test_nothing_that_the_process_did_not_keep_in_memory_can_be_installed(env):
    for name in ("c999", "", "--registry=http://evil", "npm install evil", "@acme/search-mcp"):
        with pytest.raises(UnknownCandidate):
            env.inst.install(name)
    stranger = npm_candidate()                                 # built by someone else and never returned by a search
    with pytest.raises(UnknownCandidate):
        env.inst.install(stranger.id or "cabcdef")
    assert env.runner.calls == []


def test_a_candidate_whose_name_could_be_an_option_is_refused_before_any_command_runs(env):
    for bad in (npm_candidate(package="--registry=http://evil"), npm_candidate(version="latest"), npm_candidate(package="a b"),
                npm_candidate(route="pypi", package="-r", version="1")):
        c = env.store.add(bad)
        with pytest.raises(InstallError):
            env.inst.install(c.id)
    assert env.runner.calls == []


def test_the_missing_tools_are_named(env):
    inst = Installer(env.caps, env.store, index=env.index, runner=env.runner, which=lambda n: None)
    c = env.store.add(npm_candidate())
    with pytest.raises(InstallError, match="npm is not installed"):
        inst.install(c.id)
    p = env.store.add(npm_candidate(route="pypi", package="fastpkg", version="1.2.0", name="fastpkg", bins={}))
    bad_pip = Installer(env.caps, env.store, runner=lambda c, d, t: Run(1, "", "no pip"), which=lambda n: None)
    with pytest.raises(InstallError, match="uv nor pip"):
        bad_pip.install(p.id)
    assert env.runner.calls == []
    s = env.store.add(Candidate(kind="skill", name="s", source="github", route="skill", repo="a/b", path="skills/s", commit=SHA, version="aaaaaaa"))
    with pytest.raises(InstallError, match="cannot be downloaded"):
        Installer(env.caps, env.store, runner=env.runner).install(s.id)


def pypi_candidate(wheel=b"wheel-bytes", **kw):
    c = npm_candidate(route="pypi", name="fastpkg", package="fastpkg", version="1.2.0", bins={}, args=["--x"], env=[],
                      digests=[hashlib.sha256(wheel).hexdigest()], **kw)
    return c


def test_a_python_package_is_downloaded_as_a_wheel_checked_and_installed_with_pip(env):
    c = env.store.add(pypi_candidate())
    record = env.inst.install(c.id)
    folder = env.caps / "mcp" / "fastpkg"
    dl = next(c for c in env.runner.calls if "download" in c)
    inst = next(c for c in env.runner.calls if "install" in c)
    assert dl[:4] == [sys.executable, "-m", "pip", "download"] and "--no-deps" in dl and "--only-binary=:all:" in dl and "fastpkg==1.2.0" in dl
    assert inst[:4] == [sys.executable, "-m", "pip", "install"] and "--only-binary=:all:" in inst and inst[-1].endswith(".whl")
    assert record["integrity"] == "wheel sha256 checked" and not (folder / "_wheel").exists()
    assert record["command"][:3] == [sys.executable, "-c", BOOT] and record["command"][-1] == "--x"
    done = subprocess.run(record["command"], capture_output=True, text=True, timeout=30)           # the start command does start the program, with its arguments
    assert done.returncode == 0 and "hello ['--x']" in done.stdout, done.stderr
    assert env.index.get("mcp:fastpkg")["state"] == "approved"


def test_uv_is_used_when_it_exists_with_no_build_step(env):
    inst = Installer(env.caps, env.store, index=env.index, runner=env.runner, which=lambda n: "uv" if n == "uv" else None)
    c = env.store.add(pypi_candidate())
    inst.install(c.id)
    call = env.runner.calls[-1]
    assert call[:3] == ["uv", "pip", "install"] and "--no-build" in call and "--target" in call


def test_a_wheel_that_pypi_does_not_list_is_deleted(env):
    c = env.store.add(pypi_candidate(wheel=b"other-bytes"))
    with pytest.raises(IntegrityError):
        env.inst.install(c.id)
    assert not (env.caps / "mcp" / "fastpkg").exists()


def test_a_remote_server_is_only_registered_by_its_address(env):
    c = env.store.add(Candidate(kind="mcp", name="hosted", source="registry", route="remote", url="https://mcp.example.com/mcp", version="1", transport="streamable-http"))
    record = env.inst.install(c.id)
    assert env.runner.calls == [] and not (env.caps / "mcp").exists() and record["url"] == "https://mcp.example.com/mcp"
    entry = env.index.get("mcp:hosted")
    assert entry["state"] == "approved" and entry.get("remote") and env.index.target("mcp:hosted") == "https://mcp.example.com/mcp"


@pytest.fixture
def skills(tmp_path):
    f = FakeServices()
    w = f.world
    skill_md = b"---\nname: pdf-tables\ndescription: Extract tables from a PDF file\n---\nRead the file, then do it.\n"
    script = b"print('hi')\n"
    w.trees["acme/skills"] = [
        {"path": "skills/pdf/SKILL.md", "type": "blob", "mode": "100644", "sha": git_blob_sha(skill_md), "size": len(skill_md)},
        {"path": "skills/pdf/scripts/x.py", "type": "blob", "mode": "100644", "sha": git_blob_sha(script), "size": len(script)},
        {"path": "skills/pdf/link", "type": "blob", "mode": "120000", "sha": "0" * 40, "size": 3},
        {"path": "skills/other/SKILL.md", "type": "blob", "mode": "100644", "sha": "1" * 40, "size": 3}]
    w.files[("acme/skills", "skills/pdf/SKILL.md")] = skill_md
    w.files[("acme/skills", "skills/pdf/scripts/x.py")] = script
    yield f
    f.close()


def skill_installer(env, services):
    gh = src.GithubClient(src.Http(), services.bases["github"], services.bases["github_raw"])
    return Installer(env.caps, env.store, index=env.index, runner=env.runner, github=gh)


def skill_candidate(**kw):
    return Candidate(**{**dict(kind="skill", name="pdf-tables", source="github", route="skill", repo="acme/skills", path="skills/pdf", commit=SHA, version="aaaaaaa",
                               description="Extract tables", metrics=Metrics(stars=900)), **kw})


def test_a_skill_is_downloaded_folder_only_file_by_file_at_the_pinned_commit(env, skills):
    c = env.store.add(skill_candidate())
    record = skill_installer(env, skills).install(c.id)
    folder = env.caps / "skills" / "pdf-tables"
    assert (folder / "SKILL.md").is_file() and (folder / "scripts" / "x.py").read_bytes() == b"print('hi')\n" and not (folder / "link").exists()
    assert not (folder / "other").exists() and record["id"] == "skill:pdf-tables" and record["commit"] == SHA
    assert json.loads((folder / ".basalt-source.json").read_text())["commit"] == SHA
    entry = env.index.get("skill:pdf-tables")
    assert entry["origin"] == "basalt" and entry["state"] == "approved"
    assert "Read the file" in env.index.body("skill:pdf-tables")
    assert not any(p.startswith("/raw/acme/skills/" + SHA + "/skills/other") for p, _ in skills.world.requests)


def test_a_skill_file_that_does_not_match_its_git_hash_is_refused_and_deleted(env, skills):
    skills.world.files[("acme/skills", "skills/pdf/scripts/x.py")] = b"print('evil')\n"
    c = env.store.add(skill_candidate())
    with pytest.raises(IntegrityError):
        skill_installer(env, skills).install(c.id)
    assert not (env.caps / "skills" / "pdf-tables").exists() and env.inst.records.load() == {}


def test_a_skill_path_that_climbs_out_of_the_folder_is_refused(env, skills):
    skills.world.trees["acme/skills"].append({"path": "skills/pdf/../../evil.txt", "type": "blob", "mode": "100644", "sha": "2" * 40, "size": 3})
    c = env.store.add(skill_candidate())
    with pytest.raises(InstallError):
        skill_installer(env, skills).install(c.id)
    assert not (env.caps / "skills" / "pdf-tables").exists() and not (env.caps / "evil.txt").exists()


def test_removing_deletes_the_folder_the_record_and_the_approval(env):
    c = env.store.add(npm_candidate())
    env.inst.install(c.id)
    assert env.inst.listing()[0]["id"] == "mcp:search-mcp" and env.inst.listing()[0]["keys"] == [{"name": "SEARCH_API_KEY", "entry": "SEARCH_API_KEY"}]
    assert env.inst.remove("mcp:search-mcp") is True
    assert not (env.caps / "mcp" / "search-mcp").exists() and env.inst.listing() == []
    assert "mcp:search-mcp" not in json.loads((env.caps / "approved.json").read_text(encoding="utf-8"))
    assert env.index.get("mcp:search-mcp") is None
    assert env.inst.remove("mcp:search-mcp") is False and env.inst.remove("mcp:user-own") is False
    env.inst.install(env.store.add(npm_candidate()).id)                 # and it can be installed again


def test_a_record_that_points_outside_the_folders_basalt_makes_deletes_nothing(env):
    victim = env.tmp / "precious"
    victim.mkdir()
    (victim / "f.txt").write_text("keep")
    InstalledStore(env.caps).put({"id": "mcp:x", "name": "x", "kind": "mcp", "folder": "../precious", "url": "https://x.example/mcp", "sha": "s"})
    with pytest.raises(InstallError):
        env.inst.remove("mcp:x")
    assert (victim / "f.txt").read_text() == "keep"


def test_a_name_that_is_already_there_is_not_overwritten(env):
    env.inst.install(env.store.add(npm_candidate()).id)
    with pytest.raises(InstallError, match="already installed"):
        env.inst.install(env.store.add(npm_candidate()).id)
    InstalledStore(env.caps).delete("mcp:search-mcp")
    env.index.refresh()
    shutil_target = env.caps / "mcp" / "search-mcp"
    assert shutil_target.exists()
    with pytest.raises(InstallError, match="already there"):
        env.inst.install(env.store.add(npm_candidate()).id)
    assert shutil_target.exists()                                       # the folder that was there is not deleted by the failed attempt


# ---- the keys of a server come from the vault when it starts ---------------------------------------------------------------

def vault_with(tmp_path, entries):
    from harness.vault import Vault
    vault = Vault(tmp_path / "vault")
    for entry_id, value in entries.items():
        vault.put(entry_id, kind="secret", label=entry_id, fields={"value": value})
    return tmp_path / "vault"


def installed_fake_search(env):
    c = env.store.add(npm_candidate(name="search"))
    env.inst.install(c.id)
    store = InstalledStore(env.caps)
    rec = store.get("mcp:search")
    from harness.capabilities import mcp_spec_sha
    command = [sys.executable, str(Path(__file__).resolve().parent / "fake_search_mcp.py")]
    rec.update(command=command, sha=mcp_spec_sha(command[0], command[1:]))
    store.put(rec)
    env.inst.approvals.approve(type("S", (), {"name": "mcp:search", "sha256": rec["sha"]}))
    env.index.refresh()


def test_the_key_is_read_from_the_vault_at_the_start_and_never_written_down(env):
    installed_fake_search(env)
    env.index.vault_dir = vault_with(env.tmp, {"SEARCH_API_KEY": "sekret-value-123"})
    target = env.index.target("mcp:search")
    assert target.env == {"SEARCH_API_KEY": "sekret-value-123"} and list(target)[0] == sys.executable
    for f in env.caps.rglob("*"):
        if f.is_file():
            assert "sekret-value-123" not in f.read_text(encoding="utf-8", errors="ignore"), f
    from harness.mcp_client import MCPClient
    client = MCPClient(target, env={"FAKE_SEARCH_KEY": "sekret-value-123", **target.env})
    client.start()
    try:
        client.initialize()
        assert "key=sekret-value-123" in str(client.call_tool("web_search", {"query": "q"}))
    finally:
        client.close()


def test_a_missing_key_is_said_by_the_entry_name_to_create_and_the_server_is_not_started(env):
    installed_fake_search(env)
    env.index.vault_dir = vault_with(env.tmp, {"OTHER": "x"})
    with pytest.raises(ValueError) as e:
        env.index.target("mcp:search")
    assert "SEARCH_API_KEY" in str(e.value) and "vault" in str(e.value)
    env.index.vault_dir = None
    with pytest.raises(ValueError):
        env.index.target("mcp:search")
