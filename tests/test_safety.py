import pytest
from pathlib import Path

from harness.permissions import PermissionManager, PermissionLevel
from harness.tools.registry import EffectClass
from harness.hooks import HookManager
from harness.checkpoints import CheckpointManager
from harness.audit import AuditLedger
from harness.secrets_broker import SecretsBroker
from harness.payments_broker import PaymentsBroker


def test_permission_manager_levels():
    pm_read = PermissionManager(level=PermissionLevel.READ_ONLY)
    # Pure allowed
    allowed, _ = pm_read.evaluate("read_file", EffectClass.PURE, {"path": "a.txt"})
    assert allowed
    # Reversible blocked without confirmation
    allowed, _ = pm_read.evaluate("write_file", EffectClass.REVERSIBLE, {"path": "a.txt"})
    assert not allowed

    # Plan mode blocks reversible even in AUTO tier
    pm_plan = PermissionManager(level=PermissionLevel.AUTO, plan_mode=True)
    allowed, reason = pm_plan.evaluate("write_file", EffectClass.REVERSIBLE, {})
    assert not allowed
    assert "Plan mode active" in reason

    # Rule precedence
    pm_rules = PermissionManager(level=PermissionLevel.DEFAULT)
    pm_rules.add_rule("shell_command:pytest*", "allow")
    allowed, _ = pm_rules.evaluate("shell_command", EffectClass.IRREVERSIBLE, {"command": "pytest tests/"})
    assert allowed


def test_safety_hooks():
    hooks = HookManager()
    # Dangerous rm -rf / blocked
    err = hooks.trigger_pre_tool("shell_command", {"command": "rm -rf /"})
    assert err is not None
    assert "prohibited dangerous command" in err

    # Safe command passes
    err_safe = hooks.trigger_pre_tool("shell_command", {"command": "dir"})
    assert err_safe is None


def test_checkpoints_create_and_restore(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print('v1')", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "zero.txt").write_text("", encoding="utf-8")  # 0-byte file must be preserved!

    cpm = CheckpointManager(ws, storage_dir=tmp_path / "cps")
    cp_id = cpm.create("Initial state")

    # Modify main.py, delete zero.txt, add new_file.py
    (ws / "main.py").write_text("print('v2')", encoding="utf-8")
    (ws / "sub" / "zero.txt").unlink()
    (ws / "new_file.py").write_text("temp", encoding="utf-8")

    # Restore
    assert cpm.restore(cp_id)
    assert (ws / "main.py").read_text(encoding="utf-8") == "print('v1')"
    assert (ws / "sub" / "zero.txt").exists()
    assert (ws / "sub" / "zero.txt").read_text(encoding="utf-8") == ""
    assert not (ws / "new_file.py").exists()


def test_audit_ledger_integrity(tmp_path: Path):
    ledger_path = tmp_path / "audit.jsonl"
    ledger = AuditLedger(ledger_path)

    ledger.record("agent", "read_file", {"path": "a.txt"})
    ledger.record("agent", "write_file", {"path": "b.txt"})
    valid, err = ledger.verify_integrity()
    assert valid
    assert err is None

    # Simulate tampering by editing the log file
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    tampered = lines[0].replace("a.txt", "tampered.txt")
    ledger_path.write_text("\n".join([tampered, lines[1]]) + "\n", encoding="utf-8")

    tampered_ledger = AuditLedger(ledger_path)
    valid, err = tampered_ledger.verify_integrity()
    assert not valid
    assert "Tampered" in err or "broken" in err


def test_secrets_broker():
    broker = SecretsBroker({"API_KEY": "supersecret123"})
    # Secret masked
    text = "Error connecting with key supersecret123"
    assert broker.mask_secrets(text) == "Error connecting with key [REDACTED:API_KEY]"

    # Resolve token
    expanded = broker.resolve_tokens("Bearer $SECRET:API_KEY")
    assert expanded == "Bearer supersecret123"


def test_payments_broker_and_waiver(tmp_path: Path):
    ledger = AuditLedger(tmp_path / "audit.jsonl")
    pb = PaymentsBroker(max_limit_usd=100.0, audit_ledger=ledger)

    # Cannot transact before waiver
    with pytest.raises(PermissionError, match="waiver"):
        pb.authorize_transaction(10.0, "vendor", "test")

    # Accept waiver
    assert not pb.accept_waiver("wrong phrase")
    assert pb.accept_waiver("I ACCEPT FULL RESPONSIBILITY FOR AUTONOMOUS PAYMENTS")

    tx = pb.authorize_transaction(30.0, "vendor", "cloud GPU hour")
    assert tx["status"] == "authorized"
    assert pb.spent_usd == 30.0

    # Exceed limit
    with pytest.raises(PermissionError, match="exceeds remaining"):
        pb.authorize_transaction(80.0, "vendor", "expensive item")

    # Revoke stops transactions and preserves spent record
    pb.revoke()
    assert pb.spent_usd == 30.0
    with pytest.raises(PermissionError, match="revoked"):
        pb.authorize_transaction(5.0, "vendor", "small item")
