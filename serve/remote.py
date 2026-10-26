"""Phones and other clients away from this PC.

Several API keys: each one has a name (the device it was made for), is kept only as a hash, shown once when it is made, and can be revoked on its own.
A client that is not on this PC may reach only the routes a phone needs (the model, conversations, files, routines, plans), never the vault, the
wallet, the settings or the loading of models. The server listens for such clients on one address only, the PC's Tailscale address, and only after the
person turned that on; Tailscale itself is installed and connected from here.
"""
from __future__ import annotations

import hashlib
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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._rows: List[Dict[str, Any]] = self._load()

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

    def list(self) -> List[Dict[str, Any]]:
        """The keys without what would let one be used: the name, when it was made and when it last came."""
        with self._lock:
            return [{k: r.get(k) for k in ("id", "label", "created", "last_used")} for r in self._rows]

    def create(self, label: Any) -> Tuple[Dict[str, Any], str]:
        text = " ".join(str(label or "").split())[:LABEL_MAX]
        if not text:
            raise ValueError("Give the key a name, such as the phone it is for.")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        row = {"id": secrets.token_hex(4), "label": text, "hash": _digest(key), "created": time.time(), "last_used": None}
        with self._lock:
            self._rows.append(row)
            self._save()
        return {k: row[k] for k in ("id", "label", "created", "last_used")}, key

    def verify(self, given: str) -> Optional[Dict[str, Any]]:
        """The key row for a key a client sent, or None. Compared by hash, in constant time."""
        if not given or not given.startswith(KEY_PREFIX) or len(given) > 200:
            return None
        digest = _digest(given)
        with self._lock:
            for row in self._rows:
                if secrets.compare_digest(row["hash"], digest):
                    now = time.time()
                    if not row.get("last_used") or now - row["last_used"] > TOUCH_EVERY_S:
                        row["last_used"] = now
                        try:
                            self._save()
                        except OSError:
                            pass
                    return {k: row[k] for k in ("id", "label", "created", "last_used")}
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
        try:
            self.enabled = bool(json.loads(self.path.read_text(encoding="utf-8")).get("enabled"))
        except (OSError, ValueError, AttributeError):
            self.enabled = False

    @property
    def listening(self) -> bool:
        return self._httpd is not None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"enabled": self.enabled}), encoding="utf-8")

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
