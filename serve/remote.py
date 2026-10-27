"""Phones and other clients away from this PC.

Several API keys: each one has a name (the device it was made for), is checked by its hash, and can be revoked on its own. The key itself is kept
encrypted under the device key (the one that wraps the vault), so its QR code can be shown again for a phone that was reinstalled.
A client that is not on this PC may reach only the routes a phone needs (the model, conversations, files, routines, plans), never the vault, the
wallet, the settings or the loading of models. The server listens for such clients on one address only, the PC's Tailscale address, and only after the
person turned that on; Tailscale itself is installed and connected from here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

KEY_PREFIX = "bsk_"
LABEL_MAX = 60
TOUCH_EVERY_S = 60                                       # the last use of a key is written at most this often
KEY_TTL_DAYS = 90                                        # how long a new key lasts unless the person chose otherwise
KEY_TTL_MAX_DAYS = 3650
MODE_ORDER = ("plan", "read_only", "default", "auto")    # from the least to the most a conversation may do without asking
PHONE_MAX_MODE = "default"                               # the most a phone may start a conversation with, unless the person raised it
PHONE_RULES_MAX = 10
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_NET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
# what a client from outside this PC may ask for: the model, the conversations and what they hold, the routines, the plans
REMOTE_PREFIXES = ("/v1/", "/api/agent/", "/api/attachments", "/api/outputs", "/api/routines", "/api/plans", "/api/personal-skills", "/api/files/", "/api/remote/hello", "/api/remote/peers")


def remote_path_ok(path: str) -> bool:
    return path == "/v1" or any(path == p.rstrip("/") or path.startswith(p) for p in REMOTE_PREFIXES)


def in_tailscale(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip in TAILSCALE_NET or ip in TAILSCALE_NET6


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ApiKeys:
    def __init__(self, path: str | Path, key_dir: Optional[str | Path] = None, key: Optional[bytes] = None) -> None:
        """`key_dir` holds the device key that seals the keys (or `key`, the sealing key itself); with neither, only hashes are kept."""
        self.path = Path(path)
        self.key_dir = Path(key_dir) if key_dir else None
        self._seal_key = key
        self._lock = threading.RLock()
        self._rows: List[Dict[str, Any]] = self._load()

    def _sealing_key(self) -> Optional[bytes]:
        if self._seal_key is None and self.key_dir is not None:
            from harness.waiver.keystore import load_or_create_key
            raw = load_or_create_key(self.key_dir / "device.key")
            self._seal_key = hmac.new(raw, b"basalt phone keys", hashlib.sha256).digest()
        return self._seal_key

    def _seal(self, key_id: str, key: str) -> Optional[str]:
        """The key encrypted for the file, or None when no sealing key is available (the key is then shown once, as before)."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            sealing = self._sealing_key()
            if sealing is None:
                return None
            nonce = secrets.token_bytes(12)
            return base64.b64encode(nonce + AESGCM(sealing).encrypt(nonce, key.encode("utf-8"), key_id.encode("utf-8"))).decode("ascii")
        except (ImportError, OSError, ValueError):
            return None

    def _load(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [r for r in data.get("keys", []) if isinstance(r, dict) and isinstance(r.get("hash"), str) and isinstance(r.get("id"), str)]
        except (OSError, ValueError, AttributeError):
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema": "1.0", "keys": self._rows}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def any(self) -> bool:
        with self._lock:
            return bool(self._rows)

    @staticmethod
    def _expires(ttl_days: Any) -> Optional[float]:
        """When a key made now stops working: `ttl_days` from now, none (None) for 0, the default for a missing value."""
        if ttl_days is None or ttl_days == "":
            ttl_days = KEY_TTL_DAYS
        try:
            days = int(ttl_days)
        except (TypeError, ValueError):
            raise ValueError("How long the key lasts is a number of days, or 0 for never.") from None
        if isinstance(ttl_days, bool) or not 0 <= days <= KEY_TTL_MAX_DAYS:
            raise ValueError(f"How long the key lasts is 1 to {KEY_TTL_MAX_DAYS} days, or 0 for never.")
        return time.time() + days * 86400 if days else None

    @staticmethod
    def _view(row: Dict[str, Any]) -> Dict[str, Any]:
        expires = row.get("expires")
        return {**{k: row.get(k) for k in ("id", "label", "created", "last_used")}, "expires": expires,
                "expired": bool(expires and expires <= time.time())}

    def list(self) -> List[Dict[str, Any]]:
        """The keys without what would let one be used: the name, when it was made, when it last came and when it stops working."""
        with self._lock:
            return [{**self._view(r), "recoverable": bool(r.get("sealed"))} for r in self._rows]

    def create(self, label: Any, ttl_days: Any = None) -> Tuple[Dict[str, Any], str]:
        text = " ".join(str(label or "").split())[:LABEL_MAX]
        if not text:
            raise ValueError("Give the key a name, such as the phone it is for.")
        expires = self._expires(ttl_days)
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        row = {"id": secrets.token_hex(4), "label": text, "hash": _digest(key), "created": time.time(), "last_used": None, "expires": expires}
        sealed = self._seal(row["id"], key)
        if sealed:
            row["sealed"] = sealed
        with self._lock:
            self._rows.append(row)
            self._save()
        return {**self._view(row), "recoverable": bool(sealed)}, key

    def renew(self, key_id: Any, ttl_days: Any = None) -> Optional[Dict[str, Any]]:
        """Gives a key a new end from now (or none), expired or not; None when there is no such key."""
        expires = self._expires(ttl_days)
        with self._lock:
            row = next((r for r in self._rows if r["id"] == key_id), None)
            if row is None:
                return None
            row["expires"] = expires
            self._save()
            return {**self._view(row), "recoverable": bool(row.get("sealed"))}

    def reveal(self, key_id: Any) -> Optional[Tuple[str, str]]:
        """The name and the key of a phone, to show its QR code again; None when there is no such key or it was made without being kept."""
        with self._lock:
            row = next((r for r in self._rows if r["id"] == key_id), None)
        if row is None or not row.get("sealed"):
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            blob = base64.b64decode(row["sealed"])
            key = AESGCM(self._sealing_key()).decrypt(blob[:12], blob[12:], row["id"].encode("utf-8")).decode("utf-8")
        except Exception:
            return None
        return (row["label"], key) if _digest(key) == row["hash"] else None

    def verify(self, given: str) -> Optional[Dict[str, Any]]:
        """The key row for a key a client sent, or None. Compared by hash, in constant time."""
        if not given or not given.startswith(KEY_PREFIX) or len(given) > 200:
            return None
        digest = _digest(given)
        with self._lock:
            for row in self._rows:
                if secrets.compare_digest(row["hash"], digest):
                    now = time.time()
                    if row.get("expires") and row["expires"] <= now:
                        return None                                    # a key past its end is not a key
                    if not row.get("last_used") or now - row["last_used"] > TOUCH_EVERY_S:
                        row["last_used"] = now
                        try:
                            self._save()
                        except OSError:
                            pass
                    return {k: row[k] for k in ("id", "label", "created", "last_used")}
        return None

    def expired_label(self, given: str) -> Optional[str]:
        """The name of the phone whose key this is, when it is a key that has run out; None for any other text."""
        if not given or not given.startswith(KEY_PREFIX) or len(given) > 200:
            return None
        digest = _digest(given)
        with self._lock:
            for row in self._rows:
                if secrets.compare_digest(row["hash"], digest) and row.get("expires") and row["expires"] <= time.time():
                    return row["label"]
        return None

    def revoke(self, key_id: Any) -> bool:
        with self._lock:
            kept = [r for r in self._rows if r["id"] != key_id]
            if len(kept) == len(self._rows):
                return False
            self._rows = kept
            self._save()
            return True


# -- Tailscale ----------------------------------------------------------------------------------------

def tailscale_exe() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        p = Path(base) / "Tailscale" / "tailscale.exe"
        if p.is_file():
            return str(p)
    return None


def _run(args: List[str], timeout: float = 8.0) -> Tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags, encoding="utf-8", errors="replace")
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def tailscale_status() -> Dict[str, Any]:
    """Whether Tailscale is installed here, whether it is connected, and the address and name this PC has on it."""
    exe = tailscale_exe()
    if not exe:
        return {"installed": False, "state": "NotInstalled", "ip": "", "dns_name": "", "version": ""}
    code, text = _run([exe, "status", "--json"])
    try:
        data = json.loads(text[text.index("{"):])
    except ValueError:
        return {"installed": True, "state": "Unknown", "ip": "", "dns_name": "", "version": "", "detail": text.strip()[:200]}
    me = data.get("Self") or {}
    ips = [i for i in (data.get("TailscaleIPs") or me.get("TailscaleIPs") or []) if ":" not in i]
    return {"installed": True, "state": data.get("BackendState") or "Unknown", "ip": ips[0] if ips else "",
            "dns_name": str(me.get("DNSName") or "").rstrip("."), "version": str(data.get("Version") or "")}


def parse_peers(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The other devices on the Tailscale network, from `tailscale status --json`: name, address, system and whether it is on now."""
    out = []
    for peer in (data.get("Peer") or {}).values():
        ips = [i for i in (peer.get("TailscaleIPs") or []) if ":" not in i]
        if not ips:
            continue
        out.append({"name": str(peer.get("HostName") or peer.get("DNSName") or ips[0]), "dns_name": str(peer.get("DNSName") or "").rstrip("."), "ip": ips[0],
                    "os": str(peer.get("OS") or ""), "online": bool(peer.get("Online"))})
    return sorted(out, key=lambda p: (not p["online"], p["name"].lower()))


def tailscale_peers() -> List[Dict[str, Any]]:
    exe = tailscale_exe()
    if not exe:
        return []
    code, text = _run([exe, "status", "--json"])
    try:
        return parse_peers(json.loads(text[text.index("{"):]))
    except ValueError:
        return []


class Installer:
    """Installs Tailscale with winget, in the background, and keeps what it said for the page to show."""

    def __init__(self) -> None:
        self.state, self.output = "idle", ""
        self._lock = threading.Lock()

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self.state == "running":
                return self.view()
            winget = shutil.which("winget")
            if not winget:
                self.state, self.output = "failed", "winget is not available on this PC. Install Tailscale from https://tailscale.com/download"
                return self.view()
            self.state, self.output = "running", ""
            threading.Thread(target=self._run, args=(winget,), daemon=True).start()
            return self.view()

    def _run(self, winget: str) -> None:
        code, text = _run([winget, "install", "--id", "Tailscale.Tailscale", "-e", "--silent", "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
        with self._lock:
            self.output = text.strip()[-800:]
            self.state = "done" if code == 0 or tailscale_exe() else "failed"

    def view(self) -> Dict[str, Any]:
        return {"state": self.state, "output": self.output}


_AUTH_URL = re.compile(r"https://login\.tailscale\.com/\S+")
_UP: Dict[str, Any] = {"proc": None}


def tailscale_up(timeout: float = 12.0) -> Dict[str, Any]:
    """Connects this PC to Tailscale: when it has to sign in, the address to open for that is returned (the process stays until the sign-in is done)."""
    exe = tailscale_exe()
    if not exe:
        return {"ok": False, "url": "", "message": "Tailscale is not installed."}
    old = _UP.get("proc")
    if old is not None and old.poll() is None:
        old.kill()
    proc = subprocess.Popen([exe, "up"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _UP["proc"] = proc
    seen, url, end = [], "", time.monotonic() + timeout
    buf: List[str] = []

    def pump() -> None:
        for line in proc.stdout:                          # type: ignore[union-attr]
            buf.append(line)

    threading.Thread(target=pump, daemon=True).start()
    while time.monotonic() < end and proc.poll() is None:
        found = _AUTH_URL.search("".join(buf))
        if found:
            url = found.group(0)
            break
        time.sleep(0.2)
    text = "".join(buf).strip()[-400:]
    return {"ok": proc.poll() in (None, 0), "url": url, "message": text}


# -- the listener on the Tailscale address -----------------------------------------------------------

class RemoteAccess:
    """Whether phones may reach this server, and the second listener that makes it so. The listener is bound to the PC's Tailscale address and to
    nothing else, so the network only sees it through Tailscale; every request needs one of the keys."""

    def __init__(self, state_path: str | Path, port: int, start_listener: Callable[[str, int], Any]) -> None:
        self.path = Path(state_path)
        self.port = port
        self._start_listener = start_listener
        self._httpd: Any = None
        self._lock = threading.RLock()
        self.error = ""
        self.enabled = False
        self.phone_chooses = True                              # a phone may pick its own mode and rules; off: the limits below are the PC's
        self.max_mode = PHONE_MAX_MODE                         # the most a phone may start or switch a conversation to, when it may not choose
        self.rules: List[str] = []                             # the allow rules a conversation started from a phone gets: the person's, never the phone's
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.enabled = bool(saved.get("enabled"))
            self.phone_chooses = bool(saved.get("phone_chooses", True))
            if saved.get("max_mode") in MODE_ORDER:
                self.max_mode = saved["max_mode"]
            if isinstance(saved.get("rules"), list):
                self.rules = [r for r in saved["rules"] if isinstance(r, str)][:PHONE_RULES_MAX]
        except (OSError, ValueError, AttributeError):
            pass

    @property
    def listening(self) -> bool:
        return self._httpd is not None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": self.enabled, "phone_chooses": self.phone_chooses, "max_mode": self.max_mode, "rules": self.rules}), encoding="utf-8")

    def set_limits(self, max_mode: Any, rules: Any, phone_chooses: Any = None) -> None:
        """Whether a phone chooses for itself; when it does not, what a conversation started from a phone may do without asking: its most
        permissive mode, and the allow rules it gets."""
        if max_mode not in MODE_ORDER:
            raise ValueError("The most a phone may start is one of: " + ", ".join(MODE_ORDER) + ".")
        lines = [str(r).strip() for r in rules] if isinstance(rules, list) else []
        if len(lines) > PHONE_RULES_MAX or any(not r or len(r) > 200 or "\n" in r for r in lines):
            raise ValueError(f"Rules are lines of text, at most {PHONE_RULES_MAX}.")
        with self._lock:
            self.max_mode, self.rules = max_mode, lines
            if phone_chooses is not None:
                self.phone_chooses = bool(phone_chooses)
            self._save()

    def mode_allowed(self, mode: Any) -> bool:
        return mode in MODE_ORDER and (self.phone_chooses or MODE_ORDER.index(mode) <= MODE_ORDER.index(self.max_mode))

    def apply(self) -> None:
        """Brings the listener in line with the setting: on when it is enabled and Tailscale has an address, off otherwise."""
        with self._lock:
            self.error = ""
            if not self.enabled:
                self._stop()
                return
            ip = tailscale_status().get("ip") or ""
            if not ip:
                self._stop()
                self.error = "Tailscale is not connected on this PC, so there is no address to listen on."
                return
            if self._httpd is not None and self._httpd.server_address[0] == ip:
                return
            self._stop()
            try:
                self._httpd = self._start_listener(ip, self.port)
            except OSError as exc:
                self._httpd = None
                self.error = f"Could not listen on {ip}:{self.port}: {exc}"

    def _stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except OSError:
                pass

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = bool(on)
            self._save()
            self.apply()

    def address(self) -> str:
        return f"{self._httpd.server_address[0]}:{self.port}" if self._httpd is not None else ""

    def close(self) -> None:
        with self._lock:
            self._stop()
