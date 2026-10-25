"""The vault can be kept apart from this PC's key: a backup under a passphrase opens on any PC."""
import base64
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.vault import Vault, VaultError

KEY, OTHER = bytes(range(32)), bytes(range(1, 33))


def filled(tmp, key=KEY):
    v = Vault(tmp, key=key)
    v.put("trip", kind="card", fields={"holder": "Mario Rossi", "number": "4242424242424242", "expiry": "12/30", "cvc": "123"}, hosts=["shop.example"],
          card_kind="real", currency="eur", guard=True)
    v.put("gh", kind="login", fields={"username": "mario", "password": "S3cr3tPass!", "totp_secret": "JBSWY3DPEHPK3PXP"}, hosts=["github.com"], second_factor="automatic")
    return v


def test_a_backup_opens_on_another_pc_with_another_key():
    with TemporaryDirectory() as a, TemporaryDirectory() as b:
        blob = filled(a).export_backup("a long enough passphrase")
        assert b"4242" not in blob and b"Rossi" not in blob
        new = Vault(b, key=OTHER)
        made = new.import_backup(blob, "a long enough passphrase")
        assert sorted(made["added"]) == ["gh", "trip"] and made["refused"] == []
        assert new.reveal("trip", "number") == "4242424242424242" and new.reveal("gh", "password") == "S3cr3tPass!"
        d = new.describe("trip")
        assert d["hosts"] == ["shop.example"] and d["guard"] is True and d["card_kind"] == "real" and new.describe("gh")["second_factor"] == "automatic"


def test_a_wrong_passphrase_or_a_changed_backup_opens_nothing():
    with TemporaryDirectory() as a, TemporaryDirectory() as b:
        blob = filled(a).export_backup("right")
        new = Vault(b, key=OTHER)
        with pytest.raises(VaultError, match="passphrase is wrong"):
            new.import_backup(blob, "wrong")
        changed = bytearray(blob)
        changed[-1] ^= 1
        with pytest.raises(VaultError, match="passphrase is wrong"):
            new.import_backup(bytes(changed), "right")
        with pytest.raises(VaultError, match="not a backup"):
            new.import_backup(b"nothing", "right")
        with pytest.raises(VaultError, match="needs a passphrase"):
            filled(a).export_backup("")
        assert new.entries() == []


def test_restoring_replaces_an_entry_with_the_same_name():
    with TemporaryDirectory() as a:
        v = filled(a)
        blob = v.export_backup("p")
        v.put("gh", kind="login", fields={"password": "changed-later"})
        v.import_backup(blob, "p")
        assert v.reveal("gh", "password") == "S3cr3tPass!"


def test_the_wallet_gives_and_takes_the_backup_as_a_file(tmp_path):
    from harness.browser import BrowserPrefs
    from harness.payments_broker import PaymentsBroker
    from serve.wallet_api import WalletApi
    v = filled(tmp_path / "v")
    api = WalletApi(lambda: v, lambda: PaymentsBroker(tmp_path / "p"), BrowserPrefs(tmp_path / "b.json"), tmp_path / "profile")
    out = api.backup({"passphrase": "pw"})
    assert out["name"].endswith(".bvb") and base64.b64decode(out["file"]).startswith(b"BVB1")
    other = Vault(tmp_path / "w", key=OTHER)
    api2 = WalletApi(lambda: other, lambda: PaymentsBroker(tmp_path / "p2"), BrowserPrefs(tmp_path / "b2.json"), tmp_path / "profile2")
    assert api2.restore({"file": out["file"], "passphrase": "pw"})["added"] == 2
