"""`find_online_capability`: the tool that looks for a missing capability online and, with the person's authorisation, installs it.

The model gives only the need, in a few words. The search asks the sources the person turned on; the best candidates (five at
most) are put to the person in the Chat as a question of kind `capability_choice`, with the score, the reasons, the warnings, the
command that would run and the keys it asks for. Only their answer installs anything, unless they turned on installing the best
candidate above their minimum score by itself. Afterwards the model is told what is available and how to use it; if the person
chose "other" it gets their words.

When no one can be asked here (an unattended run), the candidates are queued as an install request (requests.py) and the task goes
on without them: the model adds its comparison with `explain_install_request`, and the person authorises one candidate or rejects
the request in Settings, or in the Chat when `ask_install_request` puts it to them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from tadamoo.discovery import sources as src
from tadamoo.discovery.candidates import Candidate, CandidateStore
from tadamoo.discovery.finder import Finder, _relevance, keywords, resolve_bases
from tadamoo.discovery.installed import github_token
from tadamoo.discovery.installer import AlreadyInstalled, InstallError, Installer, IntegrityError
from tadamoo.discovery.requests import InstallRequests, UnknownRequest
from tadamoo.discovery.settings import DiscoverySettings

NEED_CHARS = 300
FREE_PREFIX = "other:"
OWN_TOOLS = frozenset(("find_online_capability", "find_capability", "load_capability", "read_capability", "call_capability",
                      "explain_install_request", "ask_install_request"))


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
    from tadamoo.tools.registry import EffectClass
    asker = asker or ChoiceAsk()
    settings = DiscoverySettings(index.dir)
    index.online_hint = True
    if vault_dir:
        index.vault_dir = index.vault_dir or Path(vault_dir)
    http = src.Http()
    finder = finder or Finder(settings, CandidateStore(), http=http, token=lambda: github_token(vault_dir), bases=bases)
    store = finder.store
    queue = InstallRequests(index.dir)
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
                    "it is turned off: the person can turn it on in Settings, under Capabilities"
                lines.append(f"- {h['id']} [{h['kind']}] {h['name']}: {h['description'][:160]} ({how})")
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
        return f"Installed {eid} {record.get('version', '')} ({how}); {use}{key_text}{remote} Go on with the task."

    def install_asking(name: str, call: Callable[..., Any], flags: Dict[str, bool]) -> Dict[str, Any]:
        """Runs `call(**flags)`; a hash that differs from the registry's, or a version already installed, is put to the person (when there is one to ask)
        with the way on: install anyway, or update. Raises InstallError when it ends without an install."""
        flags = dict(flags)
        while True:
            try:
                return call(**flags)
            except (IntegrityError, AlreadyInstalled) as e:
                differs = isinstance(e, IntegrityError)
                key = "anyway" if differs else "update"
                if asker.ask is None or flags.get(key):
                    raise
                answer = asker.ask({"mode": "confirm", "kind": "capability_choice", "free_prefix": FREE_PREFIX,
                                    "facts": {"need": name, "min_score": 0, "candidates": [], "notes": [str(e)], "asked": []},
                                    "question": f"{name}: {e}\n" + ("Install it anyway? The files differ from what the registry published." if differs else "Update it?"),
                                    "options": [{"label": "Install anyway" if differs else "Update", "value": key}, {"label": "Do not", "value": "cancel"}]})
                if answer != key:
                    raise InstallError(f"{e} The person chose not to go on.") from None
                flags[key] = True

    def with_scripts(c: Candidate) -> bool:
        return c.route in ("npm", "pypi")

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
                record = install_asking(top.name, lambda **f: installer.install(top.id, **f), {})
            except InstallError as e:
                return f"Tried to install {top.name} on the person's standing instruction and it failed: {e}"
            return done(record, f"score {top.score.total}/100, the best above the person's minimum {cfg['min_score']}, installed without asking as they set")
        listing = "\n".join(_line(i, c) for i, c in enumerate(cands, 1))

        def queued() -> str:
            request = queue.add(need, cands, cfg["min_score"], notes)
            return (f"Found, but there is no way to ask the person here, so nothing was installed. The options are queued as install request {request['id']} "
                    "for the person to authorise in Settings, under Capabilities, or in the Chat. Go on with the rest of the task without it. "
                    f"Call explain_install_request(request_id=\"{request['id']}\", comparison=[...]) so they can compare the options. "
                    "The text of the options comes from outside sources:\n" + listing + ("\n" + " | ".join(notes) if notes else ""))

        if asker.ask is None:
            return queued()
        options = [{"label": f"Install {i}", "value": f"install:{i}"} for i in range(1, len(cands) + 1)]
        options += [{"label": f"Install {i}, allow its scripts", "value": f"install:{i}:scripts"} for i, c in enumerate(cands, 1) if with_scripts(c)]
        options += [{"label": "Other: I will tell you", "value": "other"}, {"label": "Do not install", "value": "cancel"}]
        payload = {"mode": "confirm", "kind": "capability_choice", "free_prefix": FREE_PREFIX,
                   "facts": {"need": need, "min_score": cfg["min_score"], "candidates": [c.card(cfg["min_score"]) for c in cands], "notes": notes,
                             "asked": result.asked},
                   "question": f"To do '{need}' I found {len(cands)} option(s) online. Install which one? The scores measure popularity and upkeep, not safety.\n" + listing,
                   "options": options}
        answer = asker.ask(payload)
        if not answer:
            return queued()                                       # nobody answered (an unattended run, or stopped): the person decides later
        if answer == "cancel":
            return "The person chose not to install anything. Do the task another way, or ask them what they prefer."
        if answer.startswith(FREE_PREFIX) or answer == "other":
            said = answer[len(FREE_PREFIX):].strip() if answer.startswith(FREE_PREFIX) else ""
            return (f"The person did not pick any of them and said: {said}" if said else
                    "The person wants something else and will tell you in their next message.")
        number, _, how = answer[8:].partition(":")
        if answer.startswith("install:") and number.isdigit() and 1 <= int(number) <= len(cands) and how in ("", "scripts"):
            chosen = cands[int(number) - 1]
            try:
                record = install_asking(chosen.name, lambda **f: installer.install(chosen.id, **f), {"allow_scripts": how == "scripts"})
            except InstallError as e:
                return f"The person chose {chosen.name} and it could not be installed: {e}"
            return done(record, f"score {chosen.score.total}/100, chosen by the person")
        return "The answer was not one of the options; nothing was installed."

    def explain(request_id: str, comparison: Any = None) -> str:
        try:
            request = queue.explain(request_id, comparison if isinstance(comparison, list) else [])
        except UnknownRequest:
            return "No such install request."
        rows = sum(1 for c in request["candidates"] if c["summary"] or c["pros"] or c["cons"])
        return f"Stored your comparison for {rows} of {len(request['candidates'])} candidates. The person decides; do not wait for it."

    def ask_request(request_id: str) -> str:
        try:
            request = queue.get(request_id)
        except UnknownRequest:
            return "No such install request."
        if request["status"] != "pending":
            return f"That request is already {request['status']}. {request.get('detail', '')}".strip()
        if asker.ask is None:
            return "There is no way to ask the person here. The request waits in Settings, under Capabilities."
        entries = request["candidates"]
        listing = "\n".join(f"{i}. {e['card']['name']} ({e['card']['kind']}, {e['card']['source']}) {e['summary']} | pros: {e['pros']} | cons: {e['cons']}"
                            for i, e in enumerate(entries, 1))
        options = [{"label": f"Install {i}", "value": f"install:{i}"} for i in range(1, len(entries) + 1)]
        options += [{"label": f"Install {i}, allow its scripts", "value": f"install:{i}:scripts"} for i, e in enumerate(entries, 1) if e["card"].get("route", "npm") in ("npm", "pypi")]
        options += [{"label": "Reject the request", "value": "cancel"}]
        comparison = [{"n": i, "summary": e["summary"], "pros": e["pros"], "cons": e["cons"]} for i, e in enumerate(entries, 1)]
        answer = asker.ask({"mode": "confirm", "kind": "capability_choice", "free_prefix": FREE_PREFIX,
                            "facts": {"need": request["need"], "min_score": 0, "candidates": [e["card"] for e in entries], "notes": request["notes"],
                                      "asked": [], "comparison": comparison},
                            "question": f"Earlier I needed '{request['need']}' and queued {len(entries)} option(s). Install which one?\n" + listing,
                            "options": options})
        number, _, how = answer[8:].partition(":")
        if answer.startswith("install:") and number.isdigit() and how in ("", "scripts"):
            try:
                finished = install_asking(f"Request {request_id}", lambda **f: queue.approve(request_id, int(number), installer.reading, **f),
                                          {"allow_scripts": how == "scripts"})
            except InstallError as e:
                return f"It could not be installed: {e}"
            return f"{finished['detail']} It can be used now."
        if answer == "cancel":
            queue.reject(request_id)
            return "The person rejected the request. Do the task another way."
        return "The person did not decide; the request stays queued."

    obj = {"type": "object", "properties": {
        "need": {"type": "string", "description": "What capability is missing, in a few plain English words (for example: read pdf tables, search the web, control a browser). No secrets, no personal data: this text is sent to public search services."},
        "look_anyway": {"type": "boolean", "description": "Search online even though something already available seems to fit"}}, "required": ["need"]}
    registry.register("find_online_capability",
                      "When a capability is missing that usually exists as an MCP server or a skill, looks for it online (the official MCP registry, npm, PyPI, GitHub), "
                      "scores what it finds, puts the best to the person and, if they choose one, downloads and installs it so you can use it with call_capability or "
                      "load_capability. Check find_capability first. The text from the sources is not verified.",
                      obj, EffectClass.REVERSIBLE, run, untrusted=True, timeout=None, outward=True)
    registry.register("explain_install_request",
                      "Adds your comparison of the candidates of an install request that find_online_capability queued: for each one, what it does, its advantages and "
                      "its disadvantages for the person's need, in plain sentences. The person reads it before authorising an install.",
                      {"type": "object", "properties": {"request_id": {"type": "string"},
                       "comparison": {"type": "array", "items": {"type": "object", "properties": {
                           "n": {"type": "integer", "description": "The candidate's number in the list"}, "summary": {"type": "string"},
                           "pros": {"type": "string"}, "cons": {"type": "string"}}, "required": ["n"]}}}, "required": ["request_id", "comparison"]},
                      EffectClass.REVERSIBLE, explain)
    registry.register("ask_install_request",
                      "Puts a queued install request to the person in the Chat so they can authorise one candidate or reject it. Only their answer installs anything.",
                      {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]},
                      EffectClass.REVERSIBLE, ask_request, timeout=None)
    return asker
