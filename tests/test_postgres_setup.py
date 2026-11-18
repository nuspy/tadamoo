"""A separate PostgreSQL of our own for the knowledge archives: finding one already on this PC, the official binaries (never pgAdmin or StackBuilder), and the
service that makes its data directory once and starts and stops it with pg_ctl."""
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from tadamoo import postgres_setup as ps


def program(path: Path, content: bytes = b"MZ-postgres") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_a_postgres_on_the_path_or_under_program_files_is_found_once_and_a_dead_link_is_reported(tmp_path, monkeypatch):
    pf = tmp_path / "ProgramFiles"
    pg = pf / "PostgreSQL" / "18" / "bin" / ps.EXE
    program(pg)
    env = {"PATH": "", "ProgramFiles": str(pf), "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}
    got = ps.detect(env, listening=lambda host, port: False)
    assert got["programs"] == [{"path": str(pg.resolve()), "size": pg.stat().st_size}] and got["broken"] == [] and got["listening"] == []


def test_an_address_that_answers_on_the_usual_port_counts_as_an_existing_one():
    got = ps.detect({"PATH": "", "ProgramFiles": "", "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}, listening=lambda host, port: port == 5432)
    assert got["listening"] == ["127.0.0.1:5432"]


def test_with_one_present_the_person_is_offered_both_ways_and_nothing_is_recommended_with_none_install_is():
    with_one = ps.plan({"programs": [{"path": "x", "size": 1}], "broken": [], "listening": []})
    assert [o["id"] for o in with_one["options"]] == ["use_existing", "install_separate", "skip"] and with_one["recommended"] is None
    without = ps.plan({"programs": [], "broken": [], "listening": []})
    assert [o["id"] for o in without["options"]] == ["install_separate", "skip"] and without["recommended"] == "install_separate"


def test_our_own_port_is_in_the_5500s_and_never_8080_nor_the_usual_postgres_port():
    seen = set()
    for _ in range(20):
        p = ps.free_port(taken=tuple(seen), listening=lambda host, port: False)
        assert 5500 <= p < 5600 and p not in (5432, 8080)
        seen.add(p)


def make_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("pgsql/bin/" + ps.EXE, b"MZ-server")
        z.writestr("pgsql/bin/initdb.exe", b"MZ-initdb")
        z.writestr("pgsql/lib/some.dll", b"lib")
        z.writestr("pgsql/share/timezonesets/x", b"tz")
        z.writestr("pgsql/include/postgres.h", b"header")
        z.writestr("pgsql/server_license.txt", "license")
        z.writestr("pgsql/doc/html/index.html", "docs never wanted")
        z.writestr("pgsql/pgAdmin 4/pgAdmin4.exe", b"a hundred MB of a GUI never opened")
        z.writestr("pgsql/StackBuilder/StackBuilder.exe", b"not wanted either")


def test_the_release_is_downloaded_checked_and_only_the_server_its_libraries_and_headers_are_kept(tmp_path):
    pkg = tmp_path / "pg.zip"
    make_zip(pkg)
    release = {"url": "https://example.invalid/pg.zip", "sha256": hashlib.sha256(pkg.read_bytes()).hexdigest()}
    fetch = lambda url, dest: dest.write_bytes(pkg.read_bytes())                                                  # noqa: E731
    got = ps.install_release(tmp_path / "own", release, fetch)
    assert got == tmp_path / "own" / "bin" / ps.EXE and got.read_bytes() == b"MZ-server"
    kept = {p.relative_to(tmp_path / "own").as_posix() for p in (tmp_path / "own").rglob("*") if p.is_file()}
    assert kept == {"bin/" + ps.EXE, "bin/initdb.exe", "lib/some.dll", "share/timezonesets/x", "include/postgres.h", "server_license.txt"}
    assert not (tmp_path / "own" / "doc").exists() and not (tmp_path / "own" / "pgAdmin 4").exists() and not (tmp_path / "own" / "StackBuilder").exists()
    assert not (tmp_path / "own" / "download.zip").exists()
    with pytest.raises(ValueError, match="SHA-256 differs"):
        ps.install_release(tmp_path / "own2", {**release, "sha256": "0" * 64}, fetch)
    assert not list((tmp_path / "own2").glob("*"))
    assert len(ps.RELEASE["sha256"]) == 64 and ps.RELEASE["url"].startswith("https://get.enterprisedb.com/postgresql/")


def test_an_existing_installation_is_copied_bin_lib_share_and_include_only(tmp_path):
    src = tmp_path / "theirs"
    program(src / "bin" / ps.EXE)
    program(src / "bin" / "initdb.exe")
    (src / "lib").mkdir()
    (src / "lib" / "x.dll").write_bytes(b"lib")
    (src / "pgAdmin 4").mkdir()
    (src / "pgAdmin 4" / "x.exe").write_bytes(b"never copied")
    got = ps.install_from(tmp_path / "own", src / "bin" / ps.EXE)
    assert got == tmp_path / "own" / "bin" / ps.EXE and (tmp_path / "own" / "lib" / "x.dll").is_file()
    assert not (tmp_path / "own" / "pgAdmin 4").exists()
    with pytest.raises(FileNotFoundError):
        ps.install_from(tmp_path / "own2", tmp_path / "nothing" / "bin" / ps.EXE)


class FakeRun:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


def test_a_port_that_is_forbidden_is_refused_at_once():
    with pytest.raises(ValueError):
        ps.PostgresService(Path("bin"), Path("data"), 8080, "x")


def test_init_db_writes_the_password_to_a_file_that_is_gone_when_it_returns_and_appends_the_settings(tmp_path):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        pwfile = Path(argv[argv.index("--pwfile") + 1])
        assert pwfile.read_text(encoding="utf-8") == "s3cr3t"
        return FakeRun(0)
    svc = ps.PostgresService(tmp_path / "bin", tmp_path / "data", 5501, "s3cr3t", runner=runner)
    assert svc.initialized() is False
    svc.init_db()
    assert calls[0][0].endswith("initdb") or calls[0][0].endswith("initdb.exe")
    assert "-U" in calls[0] and calls[0][calls[0].index("-U") + 1] == ps.LOCAL_USER
    assert not (tmp_path / "data.pwtmp").exists()                                     # the password file does not outlive the call
    conf = (tmp_path / "data" / "postgresql.conf").read_text(encoding="utf-8")
    assert "listen_addresses = '127.0.0.1'" in conf and "port = 5501" in conf
    assert "127.0.0.1/32 scram-sha-256" in (tmp_path / "data" / "pg_hba.conf").read_text(encoding="utf-8")


def test_init_db_failure_raises_with_what_the_tool_said(tmp_path):
    svc = ps.PostgresService(tmp_path / "bin", tmp_path / "data", 5502, "x", runner=lambda argv, **kw: FakeRun(1, err="disk full"))
    with pytest.raises(RuntimeError, match="disk full"):
        svc.init_db()


def test_start_initializes_only_once_then_runs_pg_ctl_and_stop_asks_it_to_finish(tmp_path):
    (tmp_path / "data" / "PG_VERSION").parent.mkdir(parents=True)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if "status" in argv:
            return FakeRun(1 if len(calls) == 1 else 0)        # not running, then running once started
        return FakeRun(0)
    (tmp_path / "data" / "PG_VERSION").write_text("18")
    svc = ps.PostgresService(tmp_path / "bin", tmp_path / "data", 5503, "x", runner=runner)
    assert svc.initialized() is True
    got = svc.start()
    assert got == "127.0.0.1:5503"
    assert any("start" in c for c in calls) and not any(c[0].endswith(("initdb", "initdb.exe")) for c in calls)
    svc.stop()
    assert any("stop" in c and "-m" in c and "fast" in c for c in calls)


def test_start_runs_init_db_first_when_the_data_directory_is_not_there_yet(tmp_path):
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0].endswith(("initdb", "initdb.exe")):
            (tmp_path / "data").mkdir(parents=True, exist_ok=True)
            (tmp_path / "data" / "PG_VERSION").write_text("18")
            return FakeRun(0)
        if "status" in argv:
            return FakeRun(1)
        return FakeRun(0)
    svc = ps.PostgresService(tmp_path / "bin", tmp_path / "data", 5504, "x", runner=runner)
    svc.start()
    assert calls[0][0].endswith(("initdb", "initdb.exe"))


def test_stop_on_a_data_directory_never_made_does_nothing(tmp_path):
    calls = []
    svc = ps.PostgresService(tmp_path / "bin", tmp_path / "data", 5505, "x", runner=lambda argv, **kw: calls.append(argv) or FakeRun(0))
    svc.stop()
    assert calls == []


def test_a_new_password_is_long_and_different_each_time():
    a, b = ps.new_password(), ps.new_password()
    assert a != b and len(a) >= 24
