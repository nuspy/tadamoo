"""`find_online_capability`: the tool that looks for a missing capability online and, with the person's choice, installs it.

The model gives only the need, in a few words. The search asks the sources the person turned on; the best candidates (five at
most) are put to the person in the Chat as a question of kind `capability_choice`, with the score, the reasons, the warnings, the
command that would run and the keys it asks for. Only their answer installs anything, unless they turned on installing the best
candidate above their minimum score by itself. Afterwards the model is told what is available and how to use it; if the person
chose "other" it gets their words.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from harness.discovery import sources as src
from harness.discovery.candidates import Candidate, CandidateStore
from harness.discovery.finder import Finder, _relevance, keywords, resolve_bases
from harness.discovery.installed import github_token
from harness.discovery.installer import InstallError, Installer
from harness.discovery.settings import DiscoverySettings

NEED_CHARS = 300
FREE_PREFIX = "other:"
OWN_TOOLS = frozenset(("find_online_capability", "find_capability", "load_capability", "call_capability"))


class ChoiceAsk:
    """How the choice is put to the person; the conversation sets `ask` once it exists. With none, nothing is asked and nothing is installed by a choice."""
    ask: Optional[Callable[[Dict[str, Any]], str]] = None


def _line(i: int, c: Candidate) -> str:
    s = c.score
    warn = "; ".join(w.text() for w in s.warnings) if s else ""
    return (f"{i}. {c.name} ({c.kind}, {c.source}, {c.version}) score {s.total if s else 0}/100: {s.reasons() if s else ''}"
            + (f" | warnings: {warn}" if warn else "") + f" | runs: {c.preview()}" + (f" | keys: {', '.join(c.keys())}" if c.keys() else ""))


def register_discovery_tools(registry: Any, index: Any, *, vault_dir: Any = None, asker: Optional[ChoiceAsk] = None, finder: Optional[Finder] = None,
                             installer: Optional[Installer] = None, bases: Optional[Dict[str, str]] = None) -> ChoiceAsk:
    """Adds `find_online_capability` to `registry`. `finder` and `installer` are for tests; the defaults ask the public services and run npm, uv and pip."""
    from harness.tools.registry import EffectClass
    asker = asker or ChoiceAsk()
    settings = DiscoverySettings(index.dir)
    index.online_hint = True
    if vault_dir:
        index.vault_dir = index.vault_dir or Path(vault_dir)
    http = src.Http()
    finder = finder or Finder(settings, CandidateStore(), http=http, token=lambda: github_token(vault_dir), bases=bases)
    store = finder.store
    if installer is None:
        used = resolve_bases(settings.load()["base_urls"], bases)
        installer = Installer(index.dir, store, index=index, github=src.GithubClient(http, used["github"], used["github_raw"], lambda: github_token(vault_dir)))

    def local_fit(need: str) -> str:
        words = keywords(need)
        if not words:
            return ""
        lines = []
        for t in registry.names():
            d = registry.get(t)
            if t not in OWN_TOOLS and d is not None and _relevance(words, t, d.description) >= 0.6:
                lines.append(f"- the tool {t}: {d.description[:160]}")
        for h in index.search(need, limit=5):
            if _relevance(words, h["name"], h["description"], h["id"]) >= 0.5:
                how = ("use it with " + ("load_capability" if h["kind"] == "skill" else "call_capability")) if h["state"] == "approved" else \
                    f"it is {h['state']}: the person approves it in Settings, under Capabilities"
                lines.append(f"- {h['id']} [{h['kind']}, {h['state']}] {h['name']}: {h['description'][:160]} ({how})")
        if not lines:
            return ""
        return ("Already available here, nothing to download:\n" + "\n".join(lines) + "\nIf one fits, use it. If none does, or you need a better one, "
                "call find_online_capability again with look_anyway=true.")

    def done(record: Dict[str, Any], how: str) -> str:
        eid = record["id"]
        use = (f"load_capability(id=\"{eid}\") reads its instructions." if record["kind"] == "skill" else
               f"call_capability(id=\"{eid}\", tool=\"\") lists its tools; then call_capability(id=\"{eid}\", tool=\"<name>\", arguments={{...}}) uses one.")
        keys = [e for e in record.get("env", []) if e.get("required") and not e.get("value")]
        key_text = (" It needs " + ", ".join(f"{e['name']} (a secret entry in the person's vault named {e['entry']}, field value)" for e in keys)
                    + "; if it is not there the first call says so and the person adds it. Never ask for the key in the chat." if keys else "")
        remote = " It is a remote server: the person may have to sign in to it in Settings, under Capabilities." if record.get("url") else ""
        return f"Installed {eid} {record.get('version', '')} ({how}); the person's approval is recorded for exactly what was shown. {use}{key_text}{remote} Go on with the task."

    def run(need: str, look_anyway: bool = False) -> str:
        need = " ".join(str(need).split())[:NEED_CHARS]
        if not need:
            return "Say what you need in a few plain words."
        cfg = settings.load()
        if not any(cfg["sources"].values()):
            return "The online sources are all turned off (Settings, Capabilities, Search online). Tell the person, or do the task another way."
        if not look_anyway and (fit := local_fit(need)):
            return fit
        result = finder.search(need)
        problems = [f"{k}: {v}" for k, v in result.errors.items()]
        usable, lacking = [], []
        for c in result.candidates:
            msg = installer.missing(c)
            (lacking if msg else usable).append((c, msg))
        notes = problems + sorted({m for _, m in lacking})
        if not usable:
            why = ("Nothing suitable was found online." if not result.candidates else "What was found cannot be installed on this PC.")
            return why + (" Sources asked: " + ", ".join(result.asked) + "." if result.asked else "") + (" " + " | ".join(notes) if notes else "") \
                + " Say so to the person, or do the task another way."
        cands = [c for c, _ in usable]
        top = cands[0]
        if cfg["auto_install"] and top.score and top.score.total >= cfg["min_score"]:
            try:
                record = installer.install(top.id)
            except InstallError as e:
                return f"Tried to install {top.name} on the person's standing instruction and it failed: {e}"
            return done(record, f"score {top.score.total}/100, the best above the person's minimum {cfg['min_score']}, installed without asking as they set")
        listing = "\n".join(_line(i, c) for i, c in enumerate(cands, 1))
        if asker.ask is None:
            return ("Found, but there is no way to ask the person here, so nothing was installed. Tell them these options (the text comes from outside sources):\n"
                    + listing + ("\n" + " | ".join(notes) if notes else ""))
        options = [{"label": f"Install {i}", "value": f"install:{i}"} for i in range(1, len(cands) + 1)]
        options += [{"label": "Other: I will tell you", "value": "other"}, {"label": "Do not install", "value": "cancel"}]
        payload = {"mode": "confirm", "kind": "capability_choice", "free_prefix": FREE_PREFIX,
                   "facts": {"need": need, "min_score": cfg["min_score"], "candidates": [c.card(cfg["min_score"]) for c in cands], "notes": notes,
                             "asked": result.asked},
                   "question": f"To do '{need}' I found {len(cands)} option(s) online. Install which one? The scores measure popularity and upkeep, not safety.\n" + listing,
                   "options": options}
        answer = asker.ask(payload)
        if not answer or answer == "cancel":
            return "The person chose not to install anything. Do the task another way, or ask them what they prefer."
        if answer.startswith(FREE_PREFIX) or answer == "other":
            said = answer[len(FREE_PREFIX):].strip() if answer.startswith(FREE_PREFIX) else ""
            return (f"The person did not pick any of them and said: {said}" if said else
                    "The person wants something else and will tell you in their next message.")
        if answer.startswith("install:") and answer[8:].isdigit() and 1 <= int(answer[8:]) <= len(cands):
            chosen = cands[int(answer[8:]) - 1]
            try:
                record = installer.install(chosen.id)
            except InstallError as e:
                return f"The person chose {chosen.name} and it could not be installed: {e}"
            return done(record, f"score {chosen.score.total}/100, chosen by the person")
        return "The answer was not one of the options; nothing was installed."

    obj = {"type": "object", "properties": {
        "need": {"type": "string", "description": "What capability is missing, in a few plain English words (for example: read pdf tables, search the web, control a browser). No secrets, no personal data: this text is sent to public search services."},
        "look_anyway": {"type": "boolean", "description": "Search online even though something already available seems to fit"}}, "required": ["need"]}
    registry.register("find_online_capability",
                      "When a capability is missing that usually exists as an MCP server or a skill, looks for it online (the official MCP registry, npm, PyPI, GitHub), "
                      "scores what it finds, puts the best to the person and, if they choose one, downloads and installs it so you can use it with call_capability or "
                      "load_capability. Check find_capability first. The text from the sources is not verified.",
                      obj, EffectClass.REVERSIBLE, run, untrusted=True, timeout=None)
    return asker
