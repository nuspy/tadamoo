"""How freely the code the model writes runs: the person's choice, and what each level does to a command and to a program."""
import os
import sys

import pytest

from harness import exec_level
from harness.sandbox import ProcessSandbox
from harness.secrets_broker import SecretsBroker
from harness.tools.execute import find_runtimes, run_code
from harness.tools.shell import check_timeout, execute_shell_command
from harness.vault import Vault

RUNTIMES = find_runtimes()
PRINT_VAR = "import os\nprint(os.environ.get('BASALT_TEST_OUTER', 'absent'))"


@pytest.fixture(autouse=True)
def back_to_default():
    yield
    exec_level.configure(None)


def level(name):
    exec_level.configure(lambda: name)


def test_the_choice_is_free_until_the_person_changes_it_and_is_kept_in_one_file(tmp_path):
    prefs = exec_level.Prefs(tmp_path / "x" / "exec-level.json")
    assert prefs.read() == "free"
    assert prefs.write("admin") == "admin" and exec_level.Prefs(prefs.path).read() == "admin"
    with pytest.raises(ValueError, match="level"):
        prefs.write("reckless")
    prefs.path.write_text("not json", encoding="utf-8")
    assert prefs.read() == "free"                                                  # a damaged file is the default, not a failure
    prefs.path.write_text('{"level": "nonsense"}', encoding="utf-8")
    assert exec_level.Prefs(prefs.path).read() == "free"


def test_a_process_without_a_configured_source_stays_contained_and_a_file_is_read_again(tmp_path):
    assert exec_level.current() == "contained"
    exec_level.configure(tmp_path / "exec-level.json")
    assert exec_level.current() == "free"
    exec_level.Prefs(tmp_path / "exec-level.json").write("contained")
    assert exec_level.contained()


def test_the_timeout_of_a_command_has_no_ceiling_when_it_runs_free():
    assert check_timeout(5000) == (600.0, "the timeout was lowered to the maximum, 600 seconds") and check_timeout(None) == (30.0, "")
    level("free")
    assert check_timeout(5000) == (5000.0, "") and check_timeout(None) == (None, "")
    assert check_timeout(0)[0] is None                                              # a nonsense number is still refused


def test_the_deny_list_stops_a_command_only_when_it_is_contained(tmp_path):
    sandbox = ProcessSandbox(tmp_path)
    assert sandbox.run("reg query HKCU")["blocked"] is True
    level("free")
    assert sandbox.run("reg query HKCU")["blocked"] is False


def test_a_command_gets_the_whole_environment_only_when_it_runs_free(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_TEST_OUTER", "from-the-outside")
    show = f'"{sys.executable}" -c "import os;print(os.environ.get(\'BASALT_TEST_OUTER\',\'absent\'))"'
    assert execute_shell_command(show, tmp_path, timeout=30)["stdout"].strip() == "absent"
    level("free")
    assert execute_shell_command(show, tmp_path)["stdout"].strip() == "from-the-outside"


@pytest.mark.skipif("python" not in RUNTIMES, reason="no Python runtime")
def test_a_program_runs_free_with_the_environment_and_the_vault_values_ready_and_without_a_time_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("BASALT_TEST_OUTER", "from-the-outside")
    vault = Vault(tmp_path / "v", key=bytes(range(32)))
    vault.put("mail", kind="login", label="mail", fields={"username": "me", "password": "pw-1234"}, hosts=["mail.example.com"])
    broker = SecretsBroker({"API": "token-xyz"})
    broker.vault = vault
    assert broker.environment()["BASALT_VAULT_MAIL_PASSWORD"] == "pw-1234" and broker.environment()["BASALT_SECRET_API"] == "token-xyz"
    code = "import os\nprint(os.environ.get('BASALT_TEST_OUTER','absent'), os.environ.get('BASALT_VAULT_MAIL_PASSWORD','none'))"
    contained = run_code("python", code, tmp_path, RUNTIMES, broker=broker)
    assert contained["stdout"].strip() == "absent none"
    level("free")
    free = run_code("python", code, tmp_path, RUNTIMES, broker=broker)
    assert free["success"] and free["stdout"].strip() == "from-the-outside pw-1234" and not free["timed_out"]


@pytest.mark.skipif("python" not in RUNTIMES, reason="no Python runtime")
def test_a_program_that_names_the_protected_places_is_refused_only_when_contained(tmp_path):
    secret_folder = tmp_path / "wallet"
    secret_folder.mkdir()
    broker = SecretsBroker()
    broker.protect(secret_folder)
    code = f"print('hello')  # {secret_folder}"
    assert run_code("python", code, tmp_path, RUNTIMES, broker=broker).get("blocked") is True
    level("free")
    ran = run_code("python", code, tmp_path, RUNTIMES, broker=broker)
    assert not ran.get("blocked") and ran["stdout"].strip() == "hello"


def test_a_free_timeout_that_the_model_sets_is_still_honoured(tmp_path):
    level("free")
    started = __import__("time").monotonic()
    res = ProcessSandbox(tmp_path).run(f'"{sys.executable}" -c "import time;time.sleep(30)"', timeout=1.5)
    assert res["timed_out"] is True and __import__("time").monotonic() - started < 15


def test_the_wallet_api_shows_and_changes_the_level(tmp_path):
    from harness.browser import BrowserPrefs
    from harness.payments_broker import PaymentsBroker
    from serve.wallet_api import WalletApi, WalletError
    api = WalletApi(lambda: Vault(tmp_path / "vault"), lambda: PaymentsBroker(tmp_path / "payments"), BrowserPrefs(tmp_path / "browser.json"), tmp_path / "profile",
                    exec_prefs=exec_level.Prefs(tmp_path / "exec-level.json"))
    shown = api.overview()["exec_level"]
    assert shown["level"] == "free" and shown["levels"] == ["contained", "free", "admin"] and isinstance(shown["elevated"], bool)
    assert api.set_exec_level({"level": "contained"})["exec_level"]["level"] == "contained"
    assert exec_level.Prefs(tmp_path / "exec-level.json").read() == "contained"             # what a conversation process reads
    with pytest.raises(WalletError, match="level is one of"):
        api.set_exec_level({"level": "reckless"})
