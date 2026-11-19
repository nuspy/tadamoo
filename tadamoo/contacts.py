"""Contacts: who the agent is dealing with when it reaches outside (a phone call today - tadamoo/integrations/telephony_tools.py; email
and other channels later), organized in groups with default permissions a single contact's own entries can override. Global data, shared
by every project and conversation: userdata.path("contacts.json"), the same place profiles.json and routines.json live.

A contact belongs to at most one group. A permission type not mentioned anywhere for a contact is denied - there is no such thing as an
implicit grant. An unknown caller who only claims to belong to a known organization is not a contact and so has no permissions at all:
only a saved contact, matched by its own phone number or its own voice, ever gets any.

A contact can also be identified by voice (tadamoo/speech/voiceid.py), enrolled only when the owner is there to say so: a live
confirmation on a call or local microphone session, or a guided local recording. One contact - the owner themselves - can be marked
`is_self`, since the voice tests need to recognise the owner too, and a contact is the only identity record this file knows."""
from __future__ import annotations

import json
import re
import secrets
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional

from serve.util.files import write_json
from tadamoo.speech import voiceid

NAME_MAX = 120
LABEL_MAX = 160
NOTE_MAX = 2000
MAX_PHONES = 10
MAX_EMAILS = 10
VOICE_CHANNELS = ("mic", "phone")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactError(ValueError):
    pass


def _new_id() -> str:
    return secrets.token_hex(4)


def digits(number: Any) -> str:
    """A phone number as only its digits and a leading +, so matching does not care about spaces or dashes."""
    return re.sub(r"[^\d+]", "", str(number or "").strip())


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def default_file() -> Dict[str, Any]:
    return {"schema": "1.0", "permission_categories": [], "permission_types": [], "groups": [], "contacts": []}


class ContactStore:
    """Loaded whole and saved whole: at the scale this holds (contacts and permission types, not conversations), that is simpler and
    safer than a partial update, and every write is atomic (serve/util/files.py write_json)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # -- loading and saving -------------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            return default_file()
        out = default_file()
        for key in out:
            if key != "schema" and isinstance(data.get(key), list):
                out[key] = data[key]
        return out

    def save(self, data: Dict[str, Any]) -> None:
        write_json(self.path, data)

    # -- permission categories ----------------------------------------------------------------------

    def add_category(self, label: str) -> Dict[str, Any]:
        label = _text(label, LABEL_MAX)
        if not label:
            raise ContactError("A category needs a label.")
        data = self.load()
        row = {"id": _new_id(), "label": label}
        data["permission_categories"].append(row)
        self.save(data)
        return row

    def rename_category(self, category_id: str, label: str) -> None:
        label = _text(label, LABEL_MAX)
        if not label:
            raise ContactError("A category needs a label.")
        data = self.load()
        row = next((c for c in data["permission_categories"] if c["id"] == category_id), None)
        if row is None:
            raise ContactError("That category is not there any more.")
        row["label"] = label
        self.save(data)

    def remove_category(self, category_id: str) -> None:
        data = self.load()
        used = sum(1 for t in data["permission_types"] if t["category"] == category_id)
        if used:
            raise ContactError(f"{used} permission type(s) are still in this category; move or remove them first.")
        before = len(data["permission_categories"])
        data["permission_categories"] = [c for c in data["permission_categories"] if c["id"] != category_id]
        if len(data["permission_categories"]) == before:
            raise ContactError("That category is not there any more.")
        self.save(data)

    # -- permission types -----------------------------------------------------------------------------

    def add_type(self, category_id: str, label: str) -> Dict[str, Any]:
        label = _text(label, LABEL_MAX)
        if not label:
            raise ContactError("A permission type needs a label.")
        data = self.load()
        if not any(c["id"] == category_id for c in data["permission_categories"]):
            raise ContactError("That category is not there any more.")
        row = {"id": _new_id(), "category": category_id, "label": label}
        data["permission_types"].append(row)
        self.save(data)
        return row

    def update_type(self, type_id: str, category_id: Optional[str] = None, label: Optional[str] = None) -> None:
        data = self.load()
        row = next((t for t in data["permission_types"] if t["id"] == type_id), None)
        if row is None:
            raise ContactError("That permission type is not there any more.")
        if category_id is not None:
            if not any(c["id"] == category_id for c in data["permission_categories"]):
                raise ContactError("That category is not there any more.")
            row["category"] = category_id
        if label is not None:
            label = _text(label, LABEL_MAX)
            if not label:
                raise ContactError("A permission type needs a label.")
            row["label"] = label
        self.save(data)

    def remove_type(self, type_id: str) -> None:
        """Removes the type and, with it, every grant or denial of it a group or a contact held - a removed type leaves nothing
        dangling, the same way a removed setting is simply gone rather than kept as a stale reference (AGENTS.md: no shims)."""
        data = self.load()
        before = len(data["permission_types"])
        data["permission_types"] = [t for t in data["permission_types"] if t["id"] != type_id]
        if len(data["permission_types"]) == before:
            raise ContactError("That permission type is not there any more.")
        for g in data["groups"]:
            g["permissions"].pop(type_id, None)
        for c in data["contacts"]:
            c["overrides"].pop(type_id, None)
        self.save(data)

    def reorder_types(self, ids_in_order: List[str]) -> None:
        data = self.load()
        current = {t["id"] for t in data["permission_types"]}
        if set(ids_in_order) != current or len(ids_in_order) != len(current):
            raise ContactError("The new order must name every existing permission type exactly once.")
        by_id = {t["id"]: t for t in data["permission_types"]}
        data["permission_types"] = [by_id[i] for i in ids_in_order]
        self.save(data)

    # -- groups ---------------------------------------------------------------------------------------

    def _check_permissions(self, data: Dict[str, Any], permissions: Any) -> Dict[str, bool]:
        if permissions is None:
            return {}
        if not isinstance(permissions, dict):
            raise ContactError("permissions: an object of permission type id to true or false.")
        known = {t["id"] for t in data["permission_types"]}
        out: Dict[str, bool] = {}
        for key, value in permissions.items():
            if key not in known:
                raise ContactError(f"Unknown permission type {key!r}.")
            if not isinstance(value, bool):
                raise ContactError(f"permissions[{key!r}]: true or false.")
            out[key] = value
        return out

    def add_group(self, name: str, permissions: Any = None) -> Dict[str, Any]:
        name = _text(name, NAME_MAX)
        if not name:
            raise ContactError("A group needs a name.")
        data = self.load()
        row = {"id": _new_id(), "name": name, "permissions": self._check_permissions(data, permissions)}
        data["groups"].append(row)
        self.save(data)
        return row

    def update_group(self, group_id: str, name: Optional[str] = None, permissions: Any = None) -> None:
        data = self.load()
        row = next((g for g in data["groups"] if g["id"] == group_id), None)
        if row is None:
            raise ContactError("That group is not there any more.")
        if name is not None:
            name = _text(name, NAME_MAX)
            if not name:
                raise ContactError("A group needs a name.")
            row["name"] = name
        if permissions is not None:
            row["permissions"] = self._check_permissions(data, permissions)
        self.save(data)

    def remove_group(self, group_id: str) -> None:
        """Removes the group; its contacts are not removed, only set loose (group_id cleared) - a contact is never deleted as a side
        effect of something else going away."""
        data = self.load()
        before = len(data["groups"])
        data["groups"] = [g for g in data["groups"] if g["id"] != group_id]
        if len(data["groups"]) == before:
            raise ContactError("That group is not there any more.")
        for c in data["contacts"]:
            if c["group_id"] == group_id:
                c["group_id"] = ""
        self.save(data)

    # -- contacts -------------------------------------------------------------------------------------

    def _check_phones(self, phones: Any) -> List[Dict[str, str]]:
        if not isinstance(phones, list) or len(phones) > MAX_PHONES:
            raise ContactError(f"phones: a list of at most {MAX_PHONES} entries.")
        out = []
        for p in phones:
            number = digits((p or {}).get("number") if isinstance(p, dict) else p)
            if not re.fullmatch(r"\+?\d{4,20}", number):
                raise ContactError(f"phones: {number!r} is not a usable phone number.")
            out.append({"number": number, "label": _text((p or {}).get("label") if isinstance(p, dict) else "", LABEL_MAX)})
        return out

    def _check_emails(self, emails: Any) -> List[Dict[str, str]]:
        if not isinstance(emails, list) or len(emails) > MAX_EMAILS:
            raise ContactError(f"emails: a list of at most {MAX_EMAILS} entries.")
        out = []
        for e in emails:
            address = _text((e or {}).get("address") if isinstance(e, dict) else e, LABEL_MAX)
            if not _EMAIL_RE.match(address):
                raise ContactError(f"emails: {address!r} is not a usable address.")
            out.append({"address": address, "label": _text((e or {}).get("label") if isinstance(e, dict) else "", LABEL_MAX)})
        return out

    def add_contact(self, name: str, organization: str = "", role: str = "", group_id: str = "", phones: Any = None,
                     emails: Any = None, sharing_notes: str = "", relationship_notes: str = "", overrides: Any = None,
                     is_self: bool = False) -> Dict[str, Any]:
        name = _text(name, NAME_MAX)
        if not name:
            raise ContactError("A contact needs a name.")
        data = self.load()
        if group_id and not any(g["id"] == group_id for g in data["groups"]):
            raise ContactError("That group is not there any more.")
        if is_self and any(c.get("is_self") for c in data["contacts"]):
            raise ContactError("There is already a contact marked as yourself; remove that mark before adding another.")
        row = {"id": _new_id(), "name": name, "organization": _text(organization, NAME_MAX), "role": _text(role, NAME_MAX),
               "group_id": group_id, "phones": self._check_phones(phones or []), "emails": self._check_emails(emails or []),
               "sharing_notes": _text(sharing_notes, NOTE_MAX), "relationship_notes": _text(relationship_notes, NOTE_MAX),
               "overrides": self._check_permissions(data, overrides), "is_self": bool(is_self), "voice": None}
        data["contacts"].append(row)
        self.save(data)
        return row

    def self_contact(self, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        data = data if data is not None else self.load()
        return next((c for c in data["contacts"] if c.get("is_self")), None)

    def update_contact(self, contact_id: str, **fields: Any) -> None:
        data = self.load()
        row = next((c for c in data["contacts"] if c["id"] == contact_id), None)
        if row is None:
            raise ContactError("That contact is not there any more.")
        if "name" in fields:
            name = _text(fields["name"], NAME_MAX)
            if not name:
                raise ContactError("A contact needs a name.")
            row["name"] = name
        for key in ("organization", "role", "sharing_notes", "relationship_notes"):
            if key in fields:
                row[key] = _text(fields[key], NOTE_MAX if key.endswith("_notes") else NAME_MAX)
        if "group_id" in fields:
            group_id = fields["group_id"] or ""
            if group_id and not any(g["id"] == group_id for g in data["groups"]):
                raise ContactError("That group is not there any more.")
            row["group_id"] = group_id
        if "phones" in fields:
            row["phones"] = self._check_phones(fields["phones"])
        if "emails" in fields:
            row["emails"] = self._check_emails(fields["emails"])
        if "overrides" in fields:
            row["overrides"] = self._check_permissions(data, fields["overrides"])
        if "is_self" in fields:
            is_self = bool(fields["is_self"])
            if is_self and any(c.get("is_self") and c["id"] != contact_id for c in data["contacts"]):
                raise ContactError("There is already a contact marked as yourself; remove that mark before adding another.")
            row["is_self"] = is_self
        self.save(data)

    def remove_contact(self, contact_id: str) -> None:
        data = self.load()
        before = len(data["contacts"])
        data["contacts"] = [c for c in data["contacts"] if c["id"] != contact_id]
        if len(data["contacts"]) == before:
            raise ContactError("That contact is not there any more.")
        self.save(data)

    # -- what matters to a caller ------------------------------------------------------------------------

    def effective_permissions(self, contact: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
        """Every known permission type for this contact: its own override when it has one, else its group's default, else denied.
        Nothing is ever granted by implication."""
        data = data if data is not None else self.load()
        group = next((g for g in data["groups"] if g["id"] == contact.get("group_id")), None) if contact.get("group_id") else None
        base = group["permissions"] if group else {}
        overrides = contact.get("overrides") or {}
        return {t["id"]: overrides[t["id"]] if t["id"] in overrides else base.get(t["id"], False) for t in data["permission_types"]}

    def match_phone(self, number: Any) -> Optional[Dict[str, Any]]:
        """The one contact whose own phone number matches, or None - a number not saved here is not a contact, whatever the caller
        claims to be."""
        want = digits(number)
        if not want:
            return None
        data = self.load()
        for c in data["contacts"]:
            if any(digits(p["number"]) == want for p in c["phones"]):
                return c
        return None

    # -- voice identification (tadamoo/speech/voiceid.py) ----------------------------------------------

    def enroll_voice(self, contact_id: str, wavs: List[bytes], channel: str) -> Dict[str, Any]:
        """Adds one or more readings of `contact_id`'s voice to `channel` ("mic" or "phone") - readings accumulate, they never
        replace what is already there, the same way voiceprint.build() gathers several takes. A "mic" reading also seeds a rough
        "phone" reading of its own (voiceid.phone_proxy), so the phone channel has a starting point before any real call is heard."""
        if channel not in VOICE_CHANNELS:
            raise ContactError(f"channel must be one of {VOICE_CHANNELS}.")
        if not wavs:
            raise ContactError("At least one recording is needed.")
        data = self.load()
        row = next((c for c in data["contacts"] if c["id"] == contact_id), None)
        if row is None:
            raise ContactError("That contact is not there any more.")
        voice = row.get("voice") or {"embeddings": {c: [] for c in VOICE_CHANNELS}, "f0_samples": []}
        for wav in wavs:
            reading = voiceid.read(wav)
            voice["embeddings"][channel].append(reading["embedding"])
            voice["f0_samples"].append(reading["f0_hz"])
            if channel == "mic":
                voice["embeddings"]["phone"].append(voiceid.read(voiceid.phone_proxy(wav))["embedding"])
        voiced = [f for f in voice["f0_samples"] if f]
        voice["f0_hz"] = round(statistics.median(voiced), 1) if voiced else 0.0
        row["voice"] = voice
        self.save(data)
        return voice

    def match_voice(self, embedding: List[float], f0_hz: float) -> Optional[Dict[str, Any]]:
        """Who this voice most likely is, or None - see voiceid.identify() for how the search is ordered and when it gives up."""
        data = self.load()
        found = voiceid.identify(embedding, f0_hz, data["contacts"])
        if found is None:
            return None
        contact = next((c for c in data["contacts"] if c["id"] == found.contact_id), None)
        if contact is None:
            return None
        return {"contact": contact, "score": found.score, "channel": found.channel, "confident": found.confident}
