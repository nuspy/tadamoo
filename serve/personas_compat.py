"""callAPIcall's `/personas` routes, answered from TaDaMoo's own personalities (`serve/personas_api.py`): list, one, create, change, delete.

The fields callAPIcall's manifest had are the personality's: `name`, `voice {engine, voice}`, `personality`, `system_prompt`. TaDaMoo's other
non-memory fields (`avatar`, `language`, `knowledge`, `learn`) are read and written with the same names. Memory is the person's: the short
profile of what the personality knows about them, its notes and what it learned never appear in an answer and cannot be written here.
`knowledge_project` (a wikibridge project) has no equivalent and is refused when it is not empty.

In an answer `name` is the personality's slug, the identifier the other routes take (callAPIcall's folder name), and `display_name` is its name.
In a body `name` is the display name. A path accepts the slug or the display name (spaces or underscores)."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, unquote

from serve.personas_api import PersonasApi
from tadamoo import personas as pe

BODY_FIELDS = ("name", "voice", "personality", "system_prompt", "avatar", "language", "knowledge", "learn")
NO_PROJECT = "knowledge_project has no equivalent: archives are linked from the Knowledge tab, or with `knowledge` (a list of archive ids)."


class Refusal(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def item(persona: dict) -> dict:
    """What an answer says of a personality: no memory of any kind."""
    return {"name": persona["slug"], "display_name": persona["name"], "voice": persona["voice"], "avatar": persona["avatar"], "personality": persona["personality"],
            "system_prompt": persona["system_prompt"], "language": persona["language"], "knowledge": list(persona["knowledge"]), "learn": persona["learn"]}


def _body(raw: bytes) -> dict:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise Refusal(400, "bad_request", "send a JSON object") from None
    if not isinstance(body, dict):
        raise Refusal(400, "bad_request", "send a JSON object")
    if "profile" in body or "memory" in body:
        raise Refusal(400, "bad_request", "the memory of a personality is the person's: it is not read or written here")
    if "knowledge_project" in body:
        if body["knowledge_project"] not in ("", None):
            raise Refusal(400, "bad_request", NO_PROJECT)
        body = {k: v for k, v in body.items() if k != "knowledge_project"}
    unknown = sorted(str(k) for k in body if k not in BODY_FIELDS)
    if unknown:
        raise Refusal(400, "bad_request", "unknown field: " + ", ".join(unknown[:5]))
    return body


def _resolve(api: PersonasApi, raw_name: str) -> str:
    """The slug a path names: the slug itself, else the personality whose name is this one (spaces and underscores alike)."""
    name = unquote(raw_name).strip()
    if api.store.exists(name):
        return name
    wanted = name.replace("_", " ").lower()
    for persona in api.store.list():
        if persona["name"].lower() == wanted or persona["name"].lower().replace("_", " ") == wanted:
            return persona["slug"]
    raise Refusal(404, "not_found", "personality not found")


def respond(api: PersonasApi, method: str, path: str, query: str, raw: bytes) -> tuple[int, dict]:
    """(status, body) for one request on `/personas` or `/personas/<name>`; the caller has already checked who may use it."""
    try:
        return _respond(api, method, path.rstrip("/"), query, raw)
    except Refusal as e:
        return e.status, {"error": e.message, "code": e.code}
    except pe.PersonaError as e:
        return (404 if e.code == "not_found" else 400), {"error": str(e), "code": e.code}


def _respond(api: PersonasApi, method: str, path: str, query: str, raw: bytes) -> tuple[int, dict]:
    if path == "/personas":
        if method == "GET":
            return 200, {"personas": [item(p) for p in api.store.list()]}
        body = _body(raw)
        slug = pe.slug_of(str(body.get("name") or ""))
        if slug and api.store.exists(slug):
            raise Refusal(409, "exists", f"a personality '{body['name']}' already exists")
        made = api.create(body)["persona"]
        return 201, {"status": "ok", "name": made["slug"], "display_name": made["name"], "persona": item(made)}
    slug = _resolve(api, path[len("/personas/"):])
    if method == "GET":
        return 200, {"persona": item(api.store.get(slug))}
    if method in ("PUT", "PATCH"):
        return 200, {"status": "ok", "persona": item(api.update(slug, _body(raw))["persona"])}
    confirmed = (parse_qs(query).get("confirm") or [""])[0].lower() in ("1", "true", "yes")
    if not confirmed:
        notes = api.store.memory(slug).count()
        raise Refusal(400, "confirmation_needed", f"deleting a personality deletes its memory too ({notes} notes): send ?confirm=true")
    api.delete(slug, {"confirm": True})
    return 200, {"status": "ok"}
