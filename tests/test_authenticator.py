"""Reading the accounts of an authenticator app (Google Authenticator's export link, an otpauth link, a bare secret) and the codes of RFC 6238 for every algorithm the vault makes."""
import base64
from urllib.parse import quote

import pytest

from tadamoo import authenticator as au
from tadamoo.vault import VaultError, totp_code

SEEDS = {"SHA1": b"12345678901234567890", "SHA256": b"12345678901234567890123456789012", "SHA512": b"1234567890123456789012345678901234567890123456789012345678901234"}
VECTORS = {59: {"SHA1": "94287082", "SHA256": "46119246", "SHA512": "90693936"}, 1111111109: {"SHA1": "07081804", "SHA256": "68084774", "SHA512": "25091201"},
           1234567890: {"SHA1": "89005924", "SHA256": "91819424", "SHA512": "93441116"}}


def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field_bytes(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def field_int(number, value):
    return varint(number << 3) + varint(value)


def account_blob(secret, name, issuer, algorithm=1, digits=1, kind=2):
    return field_bytes(1, secret) + field_bytes(2, name.encode()) + field_bytes(3, issuer.encode()) + field_int(4, algorithm) + field_int(5, digits) + field_int(6, kind)


def export_link(accounts, size=1, index=0, batch_id=7):
    payload = b"".join(field_bytes(1, a) for a in accounts) + field_int(2, 1) + field_int(3, size) + field_int(4, index) + field_int(5, batch_id)
    return "otpauth-migration://offline?data=" + quote(base64.b64encode(payload).decode())


@pytest.mark.parametrize("at", sorted(VECTORS))
@pytest.mark.parametrize("algorithm", ["SHA1", "SHA256", "SHA512"])
def test_the_codes_of_rfc_6238_for_every_algorithm(at, algorithm):
    assert totp_code(au.base32(SEEDS[algorithm]), at, 30, 8, algorithm) == VECTORS[at][algorithm]


def test_an_algorithm_the_vault_does_not_make_is_refused():
    with pytest.raises(VaultError):
        totp_code("JBSWY3DPEHPK3PXP", 0, 30, 6, "MD5")


def test_an_export_link_gives_every_account_with_what_it_says():
    link = export_link([account_blob(b"Hello!\xde\xad\xbe\xef", "alice@example.com", "Example"), account_blob(b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a", "Bank", "", algorithm=2, digits=2)])
    got = au.read(link).accounts
    assert [(a.issuer, a.name, a.algorithm, a.digits, a.kind) for a in got] == [("Example", "alice@example.com", "SHA1", 6, "totp"), ("", "Bank", "SHA256", 8, "totp")]
    assert got[0].secret == au.base32(b"Hello!\xde\xad\xbe\xef") and got[0].label == "Example: alice@example.com"
    assert totp_code(got[0].secret, 59) == totp_code(au.base32(b"Hello!\xde\xad\xbe\xef"), 59)


def test_a_name_with_the_issuer_in_front_is_split_and_hotp_is_marked():
    got = au.read(export_link([account_blob(b"abcdefghij", "GitHub:bob", "", kind=1)])).accounts[0]
    assert (got.issuer, got.name, got.kind) == ("GitHub", "bob", "hotp")


def test_a_damaged_or_foreign_link_says_what_is_wrong():
    for bad, why in (("https://example.com", "not an authenticator"), ("otpauth-migration://offline", "no data"), ("otpauth-migration://offline?data=!!!", "base64"),
                     (export_link([account_blob(b"", "x", "y")]), "no secret"), (export_link([account_blob(b"abc", "x", "y", algorithm=4)]), "algorithm")):
        with pytest.raises(au.AuthenticatorError, match=why):
            au.read(bad)
    cut = export_link([account_blob(b"abcdefghij", "x", "y")])[:len("otpauth-migration://offline?data=") + 18]          # cut inside the first account
    with pytest.raises(au.AuthenticatorError):
        au.read(cut)


def test_the_batches_of_one_export_are_joined_in_order_and_a_missing_one_is_said():
    one = au.read(export_link([account_blob(b"aaaaaaaaaa", "a", "A")], size=2, index=1))
    zero = au.read(export_link([account_blob(b"bbbbbbbbbb", "b", "B")], size=2, index=0))
    assert au.merge_batches([one]) is None
    assert [a.name for a in au.merge_batches([one, zero]).accounts] == ["b", "a"]
    other = au.read(export_link([account_blob(b"cccccccccc", "c", "C")], size=2, index=0, batch_id=9))
    with pytest.raises(au.AuthenticatorError, match="different exports"):
        au.merge_batches([one, other])


def test_an_otpauth_link_and_a_bare_secret():
    got = au.read("otpauth://totp/Acme%20Co:carol@acme.test?secret=jbsw y3dp-ehpk3pxp&issuer=Acme%20Co&algorithm=SHA256&digits=8&period=60").accounts[0]
    assert (got.issuer, got.name, got.algorithm, got.digits, got.period, got.secret) == ("Acme Co", "carol@acme.test", "SHA256", 8, 60, "JBSWY3DPEHPK3PXP")
    bare = au.read("jbsw y3dp ehpk 3pxp").accounts[0]
    assert (bare.secret, bare.algorithm, bare.digits, bare.period) == ("JBSWY3DPEHPK3PXP", "SHA1", 6, 30)
    for bad in ("otpauth://totp/x?secret=not base32!", "otpauth://totp/x?secret=JBSWY3DP&algorithm=MD5", "otpauth://totp/x?secret=JBSWY3DP&digits=5", "otpauth://totp/x?secret=JBSWY3DP&period=1", "otpauth://totp/x", "11111"):
        with pytest.raises(au.AuthenticatorError):
            au.read(bad)
