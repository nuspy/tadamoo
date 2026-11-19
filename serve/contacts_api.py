"""The Contacts tab.

    GET /api/contacts                                            everything: categories, types, groups, contacts (each with its effective permissions)
    POST /api/contacts/categories {action: add|rename|remove, id?, label?}
    POST /api/contacts/types      {action: add|update|remove|reorder, id?, category_id?, label?, order?}
    POST /api/contacts/groups     {action: add|update|remove, id?, name?, permissions?}
    POST /api/contacts/items      {action: add|update|remove, id?, name?, organization?, role?, group_id?, phones?, emails?,
                                    sharing_notes?, relationship_notes?, overrides?, is_self?}
    POST /api/contacts/voice      {action: enroll, id, channel: mic|phone, wavs: [base64 PCM16 mono 16kHz wav, ...]}
"""
from __future__ import annotations

import base64
from typing import Any, Dict

from tadamoo.contacts import ContactError, ContactStore

CONTACT_FIELDS = ("name", "organization", "role", "group_id", "phones", "emails", "sharing_notes", "relationship_notes", "overrides",
                   "is_self")


def _voice_summary(voice: Any) -> Dict[str, Any]:
    """How many readings each channel has - never the raw embeddings, which have no use to the GUI and no business leaving the server."""
    if not voice:
        return {"enrolled": False, "readings": {}, "f0_hz": 0.0}
    readings = {ch: len(v) for ch, v in (voice.get("embeddings") or {}).items()}
    return {"enrolled": any(readings.values()), "readings": readings, "f0_hz": voice.get("f0_hz", 0.0)}


class ContactsError(ValueError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class ContactsApi:
    def __init__(self, store: ContactStore) -> None:
        self.store = store

    def view(self) -> Dict[str, Any]:
        data = self.store.load()
        contacts = [{**c, "voice": _voice_summary(c.get("voice")), "effective": self.store.effective_permissions(c, data)}
                    for c in data["contacts"]]
        return {"permission_categories": data["permission_categories"], "permission_types": data["permission_types"],
                "groups": data["groups"], "contacts": contacts}

    @staticmethod
    def _call(fn, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ContactError as e:
            raise ContactsError(str(e)) from None

    def change_categories(self, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action")
        if action == "add":
            self._call(self.store.add_category, req.get("label"))
        elif action == "rename":
            self._call(self.store.rename_category, str(req.get("id") or ""), req.get("label"))
        elif action == "remove":
            self._call(self.store.remove_category, str(req.get("id") or ""))
        else:
            raise ContactsError("action: add, rename or remove", "action")
        return self.view()

    def change_types(self, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action")
        if action == "add":
            self._call(self.store.add_type, str(req.get("category_id") or ""), req.get("label"))
        elif action == "update":
            self._call(self.store.update_type, str(req.get("id") or ""), req.get("category_id"), req.get("label"))
        elif action == "remove":
            self._call(self.store.remove_type, str(req.get("id") or ""))
        elif action == "reorder":
            order = req.get("order")
            if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
                raise ContactsError("order: a list of every permission type id, each once", "order")
            self._call(self.store.reorder_types, order)
        else:
            raise ContactsError("action: add, update, remove or reorder", "action")
        return self.view()

    def change_groups(self, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action")
        if action == "add":
            self._call(self.store.add_group, req.get("name"), req.get("permissions"))
        elif action == "update":
            self._call(self.store.update_group, str(req.get("id") or ""), req.get("name"), req.get("permissions"))
        elif action == "remove":
            self._call(self.store.remove_group, str(req.get("id") or ""))
        else:
            raise ContactsError("action: add, update or remove", "action")
        return self.view()

    def change_contacts(self, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action")
        if action == "add":
            self._call(self.store.add_contact, req.get("name"), req.get("organization") or "", req.get("role") or "",
                       req.get("group_id") or "", req.get("phones"), req.get("emails"), req.get("sharing_notes") or "",
                       req.get("relationship_notes") or "", req.get("overrides"), bool(req.get("is_self")))
        elif action == "update":
            fields = {k: req[k] for k in CONTACT_FIELDS if k in req}
            self._call(self.store.update_contact, str(req.get("id") or ""), **fields)
        elif action == "remove":
            self._call(self.store.remove_contact, str(req.get("id") or ""))
        else:
            raise ContactsError("action: add, update or remove", "action")
        return self.view()

    def change_voice(self, req: Dict[str, Any]) -> Dict[str, Any]:
        action = req.get("action")
        if action != "enroll":
            raise ContactsError("action: enroll", "action")
        wavs = req.get("wavs")
        if not isinstance(wavs, list) or not wavs or not all(isinstance(w, str) for w in wavs):
            raise ContactsError("wavs: a list of at least one base64-encoded wav recording", "wavs")
        try:
            decoded = [base64.b64decode(w) for w in wavs]
        except (ValueError, TypeError):
            raise ContactsError("wavs: not valid base64", "wavs") from None
        self._call(self.store.enroll_voice, str(req.get("id") or ""), decoded, str(req.get("channel") or ""))
        return self.view()
