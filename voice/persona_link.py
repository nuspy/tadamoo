"""The personality the avatar follows: its avatar folder and its voice, read from TaDaMoo.

The personality is the one of `brain.persona` in voice.json (Settings > Voice). The service asks TaDaMoo's server for the list of personalities
(`GET /api/personas`, a route the service's token opens) and takes the `avatar` (the name of a folder in the avatars store) and the `voice`
({engine, voice}) of that one. The result is put on the `Config` (`avatar_persona`, `avatar_voice_engine`, `avatar_voice_name`) so that the avatar
and the TTS router follow the personality. Nothing here writes to a personality."""
from __future__ import annotations

import logging

from callbridge.avatar_store import AvatarStore, valid_name
from callbridge.tadamoo_api import ApiError, TadamooApi

log = logging.getLogger("voice.persona_link")


class Link:
    """What the personality says about the avatar: `avatar` is "" when it names none, or none that exists."""

    def __init__(self, slug: str = "", name: str = "", avatar: str = "", voice: dict | None = None, language: str = "", problem: str = "") -> None:
        self.slug, self.name, self.avatar, self.voice, self.language, self.problem = slug, name, avatar, voice or {}, language, problem

    def as_dict(self) -> dict:
        return {"persona": self.slug, "name": self.name, "avatar": self.avatar, "voice": self.voice, "language": self.language, "problem": self.problem}


def resolve(slug: str, api: TadamooApi, store: AvatarStore) -> Link:
    """The link for personality `slug`. A server that does not answer or a personality that is gone gives a Link with `problem` set."""
    if not slug:
        return Link(problem="no personality is chosen in Settings > Voice")
    try:
        listing = api.get("/api/personas")
    except ApiError as e:
        return Link(slug, problem=f"TaDaMoo's personalities could not be read: {e}")
    row = next((p for p in listing.get("personas", []) if isinstance(p, dict) and p.get("slug") == slug), None)
    if row is None:
        return Link(slug, problem="the personality chosen in Settings > Voice does not exist any more")
    avatar = row.get("avatar") if isinstance(row.get("avatar"), str) else ""
    voice = row.get("voice") if isinstance(row.get("voice"), dict) else {}
    link = Link(slug, str(row.get("name") or slug), "", {"engine": str(voice.get("engine") or ""), "voice": str(voice.get("voice") or "")},
                str(row.get("language") or ""))
    if avatar:
        if valid_name(avatar) and avatar in store.names():
            link.avatar = avatar
        else:
            link.problem = f"the avatar {avatar!r} of this personality is not in the avatars folder"
    return link


def apply(config, link: Link) -> None:
    """Put the link on the config: the avatar in use and the voice it speaks with."""
    config.avatar_persona = link.avatar
    config.avatar_voice_engine = link.voice.get("engine", "")
    config.avatar_voice_name = link.voice.get("voice", "")
