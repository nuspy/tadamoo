"""Several processes share one audit ledger without forking its chain, and the person can have it verified from the conversation."""
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import commands
from harness.audit import AuditLedger
from test_commands import make
from test_session import Scripted, call, say

ROOT = Path(__file__).resolve().parent.parent


def test_two_ledgers_on_one_file_take_turns_without_forking_the_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    a, b = AuditLedger(path), AuditLedger(path)
    for i in range(6):
        (a if i % 2 == 0 else b).record("agent", f"step{i}", {"i": i})
    assert [r["details"]["i"] for r in AuditLedger(path).records] == list(range(6))
    assert a.verify_integrity() == (True, None) and b.verify_integrity() == (True, None)      # each sees what the other wrote
    assert [r["index"] for r in a.records] == list(range(6))


def test_a_ledger_that_was_open_before_another_process_wrote_still_verifies_and_continues(tmp_path):
    path = tmp_path / "audit.jsonl"
    old = AuditLedger(path)
    old.record("agent", "first", {})
    AuditLedger(path).record("agent", "second", {})
    assert old.verify_integrity() == (True, None)
    old.record("agent", "third", {})
    assert AuditLedger(path).verify_integrity() == (True, None) and len(AuditLedger(path).records) == 3


WRITER = """
import sys
from harness.audit import AuditLedger
path, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
ledger = AuditLedger(path)
for i in range(n):
    ledger.record(tag, "tick", {"i": i})
"""


def test_processes_writing_at_once_leave_one_whole_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLedger(path).record("agent", "start", {})
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(path), f"p{k}", "25"], cwd=str(ROOT)) for k in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    ledger = AuditLedger(path)
    assert ledger.verify_integrity() == (True, None) and len(ledger.records) == 101
    for k in range(4):
        assert [r["details"]["i"] for r in ledger.records if r["actor"] == f"p{k}"] == list(range(25))        # nobody's order was lost


def test_threads_of_one_process_with_their_own_ledger_objects_do_not_fork_it_either(tmp_path):
    path = tmp_path / "audit.jsonl"

    def work(k):
        ledger = AuditLedger(path)
        for i in range(15):
            ledger.record(f"t{k}", "tick", {"i": i})

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert AuditLedger(path).verify_integrity() == (True, None) and len(AuditLedger(path).records) == 60


def test_verifying_reads_the_file_as_it_is_now_so_an_edit_made_behind_the_object_is_found(tmp_path):
    path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(path)
    for i in range(3):
        ledger.record("agent", "x", {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[1].replace('"i": 1', '"i": 9')] + lines[2:]) + "\n", encoding="utf-8")
    ok, why = ledger.verify_integrity()
    assert not ok and "Tampered" in why


def test_the_audit_command_tells_whether_the_record_is_whole(tmp_path):
    session, ev, built = make(tmp_path, Scripted(call("list_directory", path="."), say("ok")))
    session.send("m1", "ciao")
    ev.wait("done")
    out = commands.run("audit", "", session)
    assert out["ok"] and out["data"]["valid"] and out["data"]["records"] >= 1 and out["data"]["problem"] == ""
    path = built.audit.path
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")                          # the last entry is cut off
    bad = commands.run("audit", "", session)["data"]
    assert not bad["valid"] and "truncated" in bad["problem"]


def test_the_audit_command_is_one_the_server_passes_on_and_the_page_words_in_both_languages():
    from serve.agent_sessions import SLASH_COMMANDS
    web = ROOT / "serve" / "web"
    assert "audit" in SLASH_COMMANDS and 'server("audit"' in (web / "slash.js").read_text(encoding="utf-8")
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    for key in ("sl.r.audit.ok", "sl.r.audit.bad", "sl.d.audit"):
        assert i18n.count(f'"{key}":') == 2, key
