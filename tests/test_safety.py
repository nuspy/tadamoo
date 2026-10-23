import pytest
from pathlib import Path

from harness.permissions import PermissionManager, PermissionLevel
from harness.tools.registry import EffectClass
from harness.hooks import HookManager
from harness.checkpoints import CheckpointManager
from harness.audit import AuditLedger
from harness.secrets_broker import SecretsBroker


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
    expanded = broker.resolve_tokens("Bearer {{secret:API_KEY}}")
    assert expanded == "Bearer supersecret123"
    assert broker.references("a {{secret:API_KEY}} b {{secret:OTHER}}") == ["API_KEY", "OTHER"]


def test_secrets_broker_never_reads_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-only-value")
    monkeypatch.setenv("ALLOWED_TOKEN", "allowed-value")
    broker = SecretsBroker()
    assert broker.get_secret("AWS_SECRET_ACCESS_KEY") is None
    assert broker.resolve_tokens("{{secret:AWS_SECRET_ACCESS_KEY}}") == "{{secret:AWS_SECRET_ACCESS_KEY}}"

    scoped = SecretsBroker.from_environment(["ALLOWED_TOKEN", "MISSING"])
    assert scoped.names() == ["ALLOWED_TOKEN"]
    assert scoped.get_secret("AWS_SECRET_ACCESS_KEY") is None


def test_secrets_broker_masks_longest_value_first():
    broker = SecretsBroker({"SHORT": "abcd", "LONG": "abcdefgh"})
    assert broker.mask_secrets("x abcdefgh y") == "x [REDACTED:LONG] y"


def test_secret_destinations_by_host():
    from harness.secrets_broker import SecretDestinationError, url_hosts
    broker = SecretsBroker({"GH": "ghp_value1234", "LOCAL": "local-value1234"}, allowed_hosts={"GH": ["API.github.com"]})
    assert url_hosts("curl https://a.example:8443/x?y=http://b.example http://c.example") == ["a.example", "c.example"]

    broker.check_destinations("curl https://api.github.com/user -H {{secret:GH}}", ["api.github.com"])
    with pytest.raises(SecretDestinationError, match="not to evil.example"):
        broker.check_destinations("x {{secret:GH}}", ["api.github.com", "evil.example"])
    with pytest.raises(SecretDestinationError, match="local use only"):
        broker.check_destinations("x {{secret:LOCAL}}", ["x.example"])
    with pytest.raises(SecretDestinationError):
        broker.check_destinations("x {{secret:LOCAL}}", [], ["curl"])
    broker.check_destinations("python build.py {{secret:LOCAL}}", [])           # local use is fine
    broker.check_destinations("curl http://anything.example", ["anything.example"])   # no secret in it

    assert broker.resolve_tokens("a {{secret:GH}}", destinations=["api.github.com"]) == "a ghp_value1234"
    with pytest.raises(SecretDestinationError):
        broker.resolve_tokens("a {{secret:GH}}", destinations=["evil.example"])
