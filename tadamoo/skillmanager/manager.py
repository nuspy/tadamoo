"""The skill manager: what is installed, and install, remove, turn on, turn off and update, for the app's Settings tab, the `/install-skill` command and the shell.

It adds nothing to how copies are made: that is the importer's (tadamoo/importer). The manager keeps the person's choice (state.py), reads addresses (sources.py),
and does the one thing the importer does not: it installs nothing that was not asked for. Installing a skill is its copy plus, when it needs them, its models; the models
belong to the program that runs them (LTX Studio) and are never deleted from here."""
from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.importer import Importer, Problem, Spaces
from tadamoo.importer.common import Lock, now_iso

from . import catalogue, describe, sources
from .jobs import Job, Jobs
from .state import Choices, hide_personal, off_ids, personal_hidden, set_off

STAGING_DAYS = 1
SKILL_MD_SHOWN = 20000


def _plugin_opts(kind: str, builtin: bool) -> Dict[str, Any]:
    """What a plugin's import is asked for: its dependencies come with it; a plan of the same id is taken over only for what ships with the program."""
    return {"adopt": builtin, "with_dependencies": True} if kind == "plugin" else {}


class SkillManager:
    def __init__(self, data: Any, program: Any = None, importer: Optional[Importer] = None, fetcher: Optional[sources.Fetcher] = None,
                 model: Optional[Callable[[], Any]] = None, installer: Optional[Callable[[str, Job], Dict[str, Any]]] = None,
                 on_change: Optional[Callable[[], None]] = None, jobs: Optional[Jobs] = None) -> None:
        """`program` is the program's skills folder; `model()` gives the client of the active model (or None); `installer(name, job)` downloads the models of a skill
        (default: tadamoo.media.components); `on_change()` is called after the installed or turned-on skills changed, so the harness reads them again; `jobs` is the runner to share between managers."""
        self.data = Path(data)
        self.dir = self.data / "skills"
        self.imp = importer or Importer(Spaces.from_data(self.data))
        self.program = Path(program) if program else catalogue.default_source()
        self.choices = Choices(self.dir / "choices.json")
        self.fetcher, self.model, self.on_change = fetcher, model, on_change
        self.installer = installer or self._install_models
        self.jobs = jobs or Jobs()
        self._component_skills: Optional[set] = None

    # -- what the program knows about models ------------------------------------------------------------------------

    def _has_components(self, name: str) -> bool:
        if self._component_skills is None:
            from tadamoo.media import components
            self._component_skills = set(components.SKILLS)
        return name in self._component_skills

    def _install_models(self, name: str, job: Job) -> Dict[str, Any]:
        from tadamoo.media import components
        from tadamoo.media.settings import MediaSettings
        return components.install(name, MediaSettings(self.data / "media.json").read(), run=job.run, say=job.say)

    def check_models(self, name: str) -> Dict[str, Any]:
        """Reads the disk (and LTX Studio's registry) for the models of a skill and records whether they are all there; nothing is downloaded."""
        from tadamoo.media import components
        from tadamoo.media.settings import MediaSettings
        rows = components.check(name, MediaSettings(self.data / "media.json").read())
        ready = all(r["present"] for r in rows)
        if name in self.choices.load():
            self.choices.put(name, models={"state": "ready" if ready else "missing", "checked_at": now_iso()})
        return {"skill": name, "ready": ready, "components": rows}

    # -- reading --------------------------------------------------------------------------------------------------

    def _top(self) -> Dict[str, Dict[str, Any]]:
        """The installed skills and plugins as the importer has them (a part of a plugin is not one of them)."""
        return {e["name"]: e for e in self.imp.manifest.load().values() if e.get("kind") in ("skill", "plugin") and not e.get("parent")}

    def _skill_ids(self, entry: Dict[str, Any]) -> List[str]:
        if entry["kind"] == "skill":
            return [f"skill:{entry['name']}"]
        return [p for p in entry.get("parts", []) if p.startswith("skill:")]

    def _slugs(self, entry: Dict[str, Any]) -> List[str]:
        items = self.imp.manifest.load()
        keys = [f"skill:{entry['name']}"] if entry["kind"] == "skill" else list(entry.get("parts", []))
        return [items[k]["personal"]["slug"] for k in keys if k.startswith("skill:") and k in items and items[k].get("personal")]

    def _enabled(self, entry: Dict[str, Any], off: set, stored: bool) -> bool:
        """Whether the harness sees it now: read from the places that hide it, so a change made elsewhere (the Capabilities page) is followed; the stored choice only when
        there is nothing to read."""
        ids, slugs = self._skill_ids(entry), self._slugs(entry)
        if not ids and not slugs:
            return stored
        hidden = (bool(ids) and all(i in off for i in ids)) or any(personal_hidden(self.imp.spaces.personal_dir, s) for s in slugs)
        return not hidden

    def _source_of(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "builtin", "ref": entry["name"]} if catalogue.find(self._entries(), entry["name"]) else {"type": "folder", "ref": str(entry.get("source", ""))}

    def _entries(self) -> List[Dict[str, Any]]:
        try:
            return catalogue.load(self.program)
        except Problem:
            return []

    def sync(self) -> Dict[str, Dict[str, Any]]:
        """The choices made to agree with what is really imported: what the importer holds and the choices do not know (skills imported before, by hand) is taken in as
        installed, what is gone from the importer is dropped, and the on/off state is read back from the places that carry it."""
        with Lock():
            top, choices, off = self._top(), self.choices.load(), off_ids(self.imp.spaces.cap_dir)
            now = {}
            for name, e in top.items():
                have = choices.get(name, {})
                now[name] = {**have, "kind": e["kind"], "source": have.get("source") or self._source_of(e), "version": e.get("version", ""), "hash": e.get("hash", ""),
                             "installed_at": have.get("installed_at") or e.get("imported_at", ""), "updated_at": e.get("updated_at", ""),
                             "enabled": self._enabled(e, off, bool(have.get("enabled", True)))}
            if now != choices:
                self.choices.save(now)
            return now

    def list(self) -> List[Dict[str, Any]]:
        """Every skill the program ships and every one installed from elsewhere, each with its state: not_installed, installed or disabled."""
        with Lock():
            state, top, entries = self.sync(), self._top(), self._entries()
            by_name = {e["name"]: e for e in entries}
            rows = [self._row(e["name"], e, state.get(e["name"]), top.get(e["name"]), by_name, state) for e in entries]
            rows += [self._row(n, None, c, top.get(n), by_name, state) for n, c in sorted(state.items()) if n not in by_name]
            return rows

    def get(self, name: str) -> Dict[str, Any]:
        row = next((r for r in self.list() if r["name"] == name), None)
        if row is None:
            raise Problem("not_found", f"'{str(name)[:60]}' is not a skill this program ships and is not installed.")
        return row

    def _set_to_install(self, name: str, by_name: Dict[str, Dict[str, Any]], state: Dict[str, Any]) -> List[str]:
        """The skill and what it requires that is not installed yet, in the order they are installed."""
        want = [e["name"] for e in catalogue.choose(list(by_name.values()), [name])]
        return [n for n in want if n not in state]

    def _gb(self, names: Sequence[str], by_name: Dict[str, Dict[str, Any]], state: Dict[str, Any]) -> Optional[float]:
        """About how much the models of these skills weigh; a skill that shares its models with one already installed or in the set counts nothing; None when none is known."""
        total, known = 0.0, False
        for n in names:
            e = by_name.get(n, {})
            if e.get("download_gb") is None:
                continue
            if any(s in state or s in names for s in e.get("shares_models_with", [])):
                continue
            total, known = total + float(e["download_gb"]), True
        return round(total, 1) if known else None

    def _row(self, name: str, cat: Optional[Dict[str, Any]], choice: Optional[Dict[str, Any]], imported: Optional[Dict[str, Any]], by_name: Dict[str, Dict[str, Any]],
             state: Dict[str, Any]) -> Dict[str, Any]:
        installed = choice is not None and imported is not None
        row: Dict[str, Any] = {"name": name, "builtin": cat is not None, "kind": (cat or {}).get("kind") or (choice or {}).get("kind", "skill"),
                               "state": ("installed" if choice["enabled"] else "disabled") if installed else "not_installed",
                               "group": (cat or {}).get("group", ""), "requires": list((cat or {}).get("requires", [])), "default": bool((cat or {}).get("default")),
                               "needed_by": sorted(n for n in state if name in (by_name.get(n) or {}).get("requires", [])) if installed else []}
        if cat is not None:
            row.update(title=cat["title"], summary=cat["summary"])
        else:
            desc = (imported or {}).get("description", "")
            row.update(title={"en": name, "it": name}, summary={"en": desc, "it": desc})
        has = self._has_components(name)
        row["has_models"] = has
        if installed:
            row.update(source=choice["source"], version=choice.get("version", ""), hash=choice.get("hash", ""), installed_at=choice.get("installed_at", ""),
                       updated_at=choice.get("updated_at", ""), files=imported.get("files", 0), bytes=imported.get("bytes", 0), parts=list(imported.get("parts", [])),
                       models=(choice.get("models") or {}).get("state", "unknown") if has else "none")
            row["download_gb"] = self._gb([name], by_name, {k: v for k, v in state.items() if k != name}) if has and row["models"] != "ready" else None
        else:
            todo = self._set_to_install(name, by_name, state) if cat is not None else [name]
            row.update(source={"type": "builtin", "ref": name} if cat is not None else None, models="none", will_install=todo,
                       download_gb=self._gb(todo, by_name, state), present=bool(cat and cat["present"]))
        return row

    # -- an address ---------------------------------------------------------------------------------------------------

    def _staging(self) -> Path:
        return self.dir / "staging"

    def _sweep_staging(self) -> None:
        base = self._staging()
        for d in base.iterdir() if base.is_dir() else []:
            if d.is_dir() and time.time() - d.stat().st_mtime > STAGING_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)

    def plan(self, address: str) -> Dict[str, Any]:
        """Reads an address and says what could be installed from it, with the size and what is already installed; nothing is installed. `token` names what was read."""
        self._sweep_staging()
        token = secrets.token_hex(6)
        work = self._staging() / token
        client = self.model() if self.model else None
        try:
            got = sources.fetch(address, work, self.fetcher, describe.make(client) if client is not None else describe.unavailable)
            found = sources.discover(got.root)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        top = self._top()
        cands = []
        for folder, kind in found:
            rel = folder.relative_to(got.root).as_posix()
            row: Dict[str, Any] = {"id": rel, "kind": kind}
            try:
                check = self.imp.verify(kind, folder, **_plugin_opts(kind, False))
                snap = check["snapshot"]
                row.update(name=snap["name"], description=snap["description"], version=snap["version"], files=snap.get("files", 0), bytes=snap.get("bytes", 0),
                           warnings=snap["warnings"], already_installed=snap["name"] in top, blockers=[b["message"] for b in check["blockers"]])
                if snap["name"] in top:
                    row["installed_from"] = (self.choices.load().get(snap["name"]) or {}).get("source")
                if got.generated and kind == "skill":
                    row["skill_md"] = (folder / "SKILL.md").read_text(encoding="utf-8")[:SKILL_MD_SHOWN]
            except Problem as exc:
                row.update(name=folder.name, description="", error=str(exc))
            cands.append(row)
        plan = {"token": token, "address": got.ref, "type": got.type, "generated": got.generated, "notes": got.notes, "root": str(got.root), "candidates": cands}
        work.mkdir(parents=True, exist_ok=True)
        (work / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return {k: v for k, v in plan.items() if k != "root"}

    def discard(self, token: str) -> None:
        if isinstance(token, str) and token.isalnum():
            shutil.rmtree(self._staging() / token, ignore_errors=True)

    def saved_plan(self, token: str) -> Dict[str, Any]:
        """What `plan` said for this token, as it was kept."""
        return {k: v for k, v in self._read_plan(token).items() if k != "root"}

    def _read_plan(self, token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or not token.isalnum():
            raise Problem("bad_request", "Send the `token` that the address was read with.")
        try:
            return json.loads((self._staging() / token / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Problem("no_plan", "That address was read a while ago and is not kept any more: read it again.") from None

    # -- installing -----------------------------------------------------------------------------------------------

    def _put(self, kind: str, path: Path, builtin: bool, overwrite_local: bool = False) -> str:
        """Makes the copy: imports it, or brings an installed one up to the source. Returns installed, updated or same."""
        opts = _plugin_opts(kind, builtin)
        check = self.imp.verify(kind, path, **opts)
        name = check["snapshot"]["name"]
        if not check["already_imported"]:
            if not check["can_import"]:
                raise Problem("blocked", check["blockers"][0]["message"], blockers=check["blockers"])
            self.imp.import_(kind, path, **opts)
            return "installed"
        with self._visible(name):
            seen = self.imp.reimport(kind, name, source=str(path), **opts)
            if not seen["source_changed"] and not seen["local_edited"]:
                return "same"
            only_plans = bool(seen["local_edited"]) and all(str(x).startswith("plan:") for x in seen.get("local_changes", []))
            kept = self._plan_values([str(x)[5:] for x in seen.get("local_changes", [])]) if only_plans else {}
            self.imp.reimport(kind, name, confirm=True, overwrite_local=overwrite_local or only_plans, source=str(path), **opts)
            self._restore_plan_values(kept)
        return "updated"

    def _plan_values(self, plan_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """The values the person set in the parameters of these plans: what a plan's own copy holds that the source does not."""
        out: Dict[str, Dict[str, Any]] = {}
        for pid in plan_ids:
            try:
                bridge = self.imp.plans().get(pid)
                out[pid] = {p["name"]: p["value"] for p in bridge.dag.parameters()}
            except Exception:
                continue
        return out

    def _restore_plan_values(self, kept: Dict[str, Dict[str, Any]]) -> None:
        """Puts the person's values back into the new copy of each plan, for the parameters it still has."""
        for pid, values in kept.items():
            try:
                bridge = self.imp.plans().get(pid)
                have = {p["name"] for p in bridge.dag.parameters()}
                bridge.handle({"type": "plan:parameters", "values": {k: v for k, v in values.items() if k in have}})
            except Exception:
                continue

    def _visible(self, name: str) -> "_Visible":
        return _Visible(self, name)

    def _record(self, name: str, kind: str, source: Dict[str, Any]) -> None:
        """Notes what was installed in the person's choices: turned on (a skill installed again is turned on)."""
        entry = self._top().get(name) or {}
        have = self.choices.load().get(name)
        self.choices.put(name, kind=kind, source=source, version=entry.get("version", ""), hash=entry.get("hash", ""),
                         installed_at=(have or {}).get("installed_at") or entry.get("imported_at") or now_iso(), updated_at=entry.get("updated_at", ""),
                         enabled=(have or {}).get("enabled", True))

    def install(self, names: Optional[Sequence[str]] = None, token: str = "", only: Optional[Sequence[str]] = None, models: bool = True, job: Optional[Job] = None,
                overwrite_local: bool = False) -> Dict[str, Any]:
        """Installs what the person asked for: built-in skills by `names` (with what each requires), or what an address gave (`token` of `plan`, `only` the ids to take,
        None for every one). Then the models of each skill that has them, unless `models` is off. Nothing else is touched. Runs in the caller's thread."""
        job = job or Job("install", names or [])
        out: Dict[str, Any] = {"installed": [], "updated": [], "same": [], "models": [], "problems": []}
        if token:
            plan = self._read_plan(token)
            wanted = [c for c in plan["candidates"] if only is None or c["id"] in only]
            if not wanted:
                raise Problem("nothing_chosen", "None of the skills of that address was chosen.")
            job.steps = len(wanted)
            for c in wanted:
                job.check()
                job.current, job.phase = c["name"], "copy"
                if c.get("error") or c.get("blockers"):
                    out["problems"].append({"name": c["name"], "why": c.get("error") or c["blockers"][0]})
                    job.step += 1
                    continue
                folder = Path(plan["root"]) / c["id"] if c["id"] != "." else Path(plan["root"])
                source = {"type": plan["type"], "ref": plan["address"], "sub": c["id"], "generated": plan["generated"]}
                if plan["type"] == "folder":
                    source = {"type": "folder", "ref": str(folder)}
                else:
                    keep = self.dir / "sources" / c["name"]
                    shutil.rmtree(keep, ignore_errors=True)
                    shutil.copytree(folder, keep, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                    folder = keep
                with Lock():
                    result = self._put(c["kind"], folder, False, overwrite_local)
                    self._record(c["name"], c["kind"], source)
                out[result].append(c["name"])
                job.step += 1
            if len(wanted) == len(plan["candidates"]):                         # what was read stays until every skill of it is taken or it is discarded
                self.discard(token)
            installed = out["installed"] + out["updated"] + out["same"]
        else:
            entries = self._entries()
            chosen = catalogue.choose(entries, list(names or []))
            job.steps = len(chosen)
            for e in chosen:
                job.check()
                job.current, job.phase = e["name"], "copy"
                if not e["present"]:
                    out["problems"].append({"name": e["name"], "why": f"the folder {e['path']} is missing from the program folder"})
                    job.step += 1
                    continue
                with Lock():
                    result = self._put(e["kind"], Path(e["path"]), True, overwrite_local)
                    self._record(e["name"], e["kind"], {"type": "builtin", "ref": e["name"]})
                out[result].append(e["name"])
                job.step += 1
            installed = [e["name"] for e in chosen if e["present"]]
        self._changed()
        if models:
            todo = [n for n in installed if self._has_components(n)]
            job.phase, job.step, job.steps = "models", 0, len(todo)
            for n in todo:
                job.check()
                job.current = n
                done = self.installer(n, job)
                self.choices.put(n, models={"state": "ready" if done.get("ready") else "missing", "checked_at": now_iso()})
                out["models"].append({"name": n, "ready": bool(done.get("ready")), "by_hand": done.get("by_hand", [])})
                job.step += 1
        job.current, job.phase = "", ""
        return out

    def download_models(self, name: str, job: Optional[Job] = None) -> Dict[str, Any]:
        """Downloads the models an installed skill is missing."""
        row = self.get(name)
        if row["state"] == "not_installed":
            raise Problem("not_installed", f"'{name}' is not installed: models are downloaded only for a skill that is.")
        if not row["has_models"]:
            raise Problem("no_models", f"'{name}' has no models to download.")
        job = job or Job("models", [name])
        job.current, job.phase, job.step, job.steps = name, "models", 0, 1
        done = self.installer(name, job)
        self.choices.put(name, models={"state": "ready" if done.get("ready") else "missing", "checked_at": now_iso()})
        job.step = 1
        return {"name": name, "ready": bool(done.get("ready")), "by_hand": done.get("by_hand", [])}

    def check_request(self, names: Optional[Sequence[str]], token: str) -> None:
        """A Problem when an install request names an unknown built-in skill or an address that is no longer kept, so that it is refused before a job starts."""
        if token:
            self._read_plan(token)
        else:
            catalogue.choose(self._entries(), list(names or []))

    def start(self, what: str, names: Sequence[str], work: Callable[[Job], Dict[str, Any]], wait: bool = False) -> Job:
        """Runs `work(job)` in the background (one at a time); the page reads `jobs.view()`."""
        return self.jobs.start(what, names, work, wait)

    # -- changing what is installed -------------------------------------------------------------------------------

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _entry(self, name: str) -> Dict[str, Any]:
        entry = self._top().get(name)
        if entry is None:
            raise Problem("not_imported", f"'{str(name)[:60]}' is not installed.")
        return entry

    def set_enabled(self, name: str, enabled: bool) -> Dict[str, Any]:
        """Turns an installed skill on (the harness sees it) or off (it does not; its files and models stay)."""
        with Lock():
            self.sync()
            entry = self._entry(name)
            ids = self._skill_ids(entry)
            set_off(self.imp.spaces.cap_dir, ids, not enabled)
            for slug in self._slugs(entry):
                hide_personal(self.imp.spaces.personal_dir, slug, not enabled)
            if entry["kind"] == "plugin" and not enabled:                       # a plugin that is off also stops its servers; turning it on does not start them again
                set_off(self.imp.spaces.cap_dir, [p for p in entry.get("parts", []) if p.startswith("mcp:")], True)
            self.choices.put(name, enabled=enabled)
        self._changed()
        return self.get(name)

    def remove(self, name: str) -> Dict[str, Any]:
        """Removes the installed copy (and a plugin's parts). The models stay on the disk: they belong to the program that runs them."""
        with Lock():
            self.sync()
            entry = self._entry(name)
            ids = self._skill_ids(entry) + [p for p in entry.get("parts", []) if p.startswith("mcp:")]
            slugs = self._slugs(entry)
            needed_by = sorted(n for n, e in self._top().items() if name in (catalogue.find(self._entries(), n) or {}).get("requires", []))
            for slug in slugs:
                hide_personal(self.imp.spaces.personal_dir, slug, False)
            try:
                self.imp.delete(entry["kind"], name)
            except Problem:
                if (self.choices.load().get(name) or {}).get("enabled") is False:
                    for slug in slugs:
                        hide_personal(self.imp.spaces.personal_dir, slug, True)
                raise
            set_off(self.imp.spaces.cap_dir, ids, False)
            self.choices.drop(name)
            shutil.rmtree(self.dir / "sources" / name, ignore_errors=True)
        self._changed()
        return {"removed": name, "models_kept": self._has_components(name), "needed_by": needed_by}

    def update(self, name: str, overwrite_local: bool = False, check_only: bool = False) -> Dict[str, Any]:
        """Brings an installed skill up to its source again: the program's folder, the folder it was installed from, or its address read again. A copy edited here is
        replaced only with `overwrite_local`. `check_only` says what would change."""
        with Lock():
            self.sync()
            entry = self._entry(name)
            choice = self.choices.load().get(name) or {}
            source = choice.get("source") or {}
            if source.get("type") == "builtin":
                cat = catalogue.find(self._entries(), name)
                if cat is None or not cat["present"]:
                    raise Problem("source_missing", f"'{name}' is no longer in the program's folder.")
                path = Path(cat["path"])
            elif source.get("type") == "folder":
                path = Path(str(entry.get("source", source.get("ref", ""))).split("#", 1)[0])
                if not path.exists():
                    raise Problem("source_missing", f"The folder it was installed from is not there any more: {path}")
            elif source.get("generated"):
                raise Problem("not_updatable", "This skill was written from a page by the model: install it again from the address to get a new version.")
            else:
                path = None
        if path is None:
            plan = self.plan(source["ref"])
            try:
                match = next((c for c in plan["candidates"] if c["name"] == name), None)
                if match is None:
                    raise Problem("renamed", f"The address no longer holds a skill called '{name}'.")
                root = Path(self._read_plan(plan["token"])["root"])
                keep = self.dir / "sources" / name
                shutil.rmtree(keep, ignore_errors=True)
                shutil.copytree(root / match["id"], keep, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
                path = keep
            finally:
                self.discard(plan["token"])
        with Lock():
            if check_only:
                seen = self.imp.reimport(entry["kind"], name, source=str(path), **_plugin_opts(entry["kind"], source.get("type") == "builtin"))
                return {"name": name, "status": "preview", "source_changed": seen["source_changed"], "local_edited": seen["local_edited"], "diff": seen["diff"]["counts"]}
            result = self._put(entry["kind"], path, source.get("type") == "builtin", overwrite_local)
            self._record(name, entry["kind"], source)
        if result != "same":
            self._changed()
        return {"name": name, "status": "updated" if result == "updated" else "unchanged"}


class _Visible:
    """While it is open, a turned-off skill's personal copy is back under its plain name, so the importer finds it; on leaving it is put away again."""

    def __init__(self, manager: SkillManager, name: str) -> None:
        self.m, self.name, self.slugs = manager, name, []

    def __enter__(self) -> "_Visible":
        entry = self.m._top().get(self.name)
        self.slugs = [s for s in (self.m._slugs(entry) if entry else []) if hide_personal(self.m.imp.spaces.personal_dir, s, False)]
        return self

    def __exit__(self, *exc: Any) -> None:
        for slug in self.slugs:
            hide_personal(self.m.imp.spaces.personal_dir, slug, True)
