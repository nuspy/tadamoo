"""Logins from the file a browser or a password manager exports (CSV), as vault entries: one per site and user, sent only to that site.

Reads the columns Edge, Chrome, Firefox, Bitwarden, 1Password and KeePass use under their usual names. Nothing is guessed beyond those names.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

URL = ("url", "origin", "login_uri", "website", "web site", "login url", "uri", "site")
USER = ("username", "user name", "login", "login_username", "user", "email", "e-mail")
PASSWORD = ("password", "login_password", "pass")
NAME = ("name", "title", "account", "description")
MAX_ROWS = 5000
MAX_BYTES = 8 << 20


def _pick(row: Dict[str, str], names: Tuple[str, ...]) -> str:
    for n in names:
        if row.get(n):
            return row[n].strip()
    return ""


def host_of(url: str) -> str:
    """The site of an address, as a vault entry names it: lowercase, no `www.`, no port."""
    text = url.strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "https://" + text)
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def slug(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", host).strip("-")[:30] or "site"


def parse_logins(text: str, existing: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """{"entries": [...], "skipped": {...}} from a CSV; an entry already in the vault for the same site, user and password is skipped."""
    if len(text) > MAX_BYTES:
        raise ValueError("the file is larger than 8 MB")
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("the file has no header row")
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
    if not (set(reader.fieldnames) & set(URL)) or not (set(reader.fieldnames) & set(PASSWORD)):
        raise ValueError("the file needs a column for the site (url) and one for the password")
    taken = {e["id"] for e in (existing or [])}
    known = {(e.get("host"), e.get("username"), e.get("password")) for e in (existing or [])}
    entries: List[Dict[str, Any]] = []
    skipped = {"no_site": 0, "no_password": 0, "duplicate": 0}
    for n, row in enumerate(reader):
        if n >= MAX_ROWS:
            raise ValueError(f"the file has more than {MAX_ROWS} rows")
        row = {(k or "").strip().lower(): (v or "") for k, v in row.items() if k is not None}
        host = host_of(_pick(row, URL))
        password, user = _pick(row, PASSWORD), _pick(row, USER)
        if not host or "." not in host and host != "localhost":
            skipped["no_site"] += 1
            continue
        if not password:
            skipped["no_password"] += 1
            continue
        if (host, user, password) in known:
            skipped["duplicate"] += 1
            continue
        known.add((host, user, password))
        base, entry_id, k = slug(host), slug(host), 2
        while entry_id in taken:
            entry_id = f"{base[:26]}-{k}"
            k += 1
        taken.add(entry_id)
        entries.append({"id": entry_id, "kind": "login", "label": (_pick(row, NAME) or host)[:80], "hosts": [host],
                        "fields": {k2: v for k2, v in (("username", user), ("password", password)) if v}, "second_factor": "none"})
    return {"entries": entries, "skipped": skipped}
