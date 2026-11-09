"""Brings the personas, avatars and cloned voices of a callAPIcall folder into TaDaMoo: a plan of what would be made, then the run.

    <source>/avatar/persone/<Name>/    manifest.json  memory.json  rag/chunks.jsonl  docs/  clips/*.mp4  poster.jpg  (wav2lip/ is a cache: left)
    <source>/cloned_voices/ or <source>/call-bridge/cloned_voices/    voice samples

A persona becomes a TaDaMoo personality (name, voice, personality, system prompt); the rolling profile and the remembered sessions of memory.json
go into the personality's encrypted memory and nowhere else; its documents (or, without them, its saved passages) become a knowledge archive linked
to it; its clips and poster are copied to `<avatars>/<slug>/`. The Telegram values are not read here. Nothing is deleted, nothing is fetched, a
persona that already exists is skipped unless the item says `replace`, and a second run changes nothing.

The stores are handed in, so this module imports nothing of TaDaMoo; every problem of one item is reported on that item."""
from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PERSONE = ("avatar", "persone")
VOICE_FOLDERS = (("cloned_voices",), ("call-bridge", "cloned_voices"))
VOICE_EXT = (".wav", ".mp3", ".flac", ".ogg")
CLIP_EXT = (".mp4", ".mov", ".webm", ".mkv")
POSTER_EXT = (".jpg", ".jpeg", ".png", ".webp")
JSON_MAX = 8 << 20
SCAN_MAX = 20000                         # files counted in a folder that is only measured
FIELD_LIMITS = {"personality": 8000, "system_prompt": 8000, "profile": 1500}


class SourceError(Exception):
    """The source folder cannot be used; the message says why."""


def _slug(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:40].strip("-")


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def source_root(raw: Any, forbidden: Iterable[str | Path] = ()) -> Path:
    """The callAPIcall folder named by the person: an existing absolute folder with `avatar/persone`, outside the TaDaMoo data."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise SourceError("source: the full path of the callAPIcall folder")
    path = Path(raw.strip())
    if not path.is_absolute() or ".." in path.parts:
        raise SourceError("source: a full path without '..'")
    try:
        real = path.resolve(strict=True)
    except OSError:
        raise SourceError("there is no such folder") from None
    if not real.is_dir():
        raise SourceError("that is not a folder")
    if _inside(real, [Path(f).resolve() for f in forbidden]):
        raise SourceError("that folder is inside the TaDaMoo data folder")
    if not real.joinpath(*PERSONE).is_dir() and not any(real.joinpath(*v).is_dir() for v in VOICE_FOLDERS):
        raise SourceError("this is not a callAPIcall folder: it has no avatar/persone and no cloned_voices")
    return real


def _json(path: Path) -> Optional[dict]:
    """A small JSON object file, or None when it is not there, too large, damaged or not an object."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > JSON_MAX:
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _files(folder: Path, ext: Iterable[str]) -> List[Path]:
    """The regular files of a folder with one of the extensions (no links, no subfolders), by name."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in tuple(ext))


def _tree_size(folder: Path) -> int:
    total, seen = 0, 0
    for base, dirs, names in os.walk(folder):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for n in names:
            seen += 1
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
            if seen >= SCAN_MAX:
                return total
    return total


def _ts(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# ------------------------------------------------------------------------------------------------ reading one persona
def _read_persona(folder: Path) -> Dict[str, Any]:
    manifest = _json(folder / "manifest.json")
    memory = _json(folder / "memory.json") or {}
    out: Dict[str, Any] = {"folder": folder, "manifest": manifest, "problems": [], "notes": []}
    if manifest is None:
        out["problems"].append("it has no readable manifest.json")
        return out
    name = manifest.get("name") if isinstance(manifest.get("name"), str) and manifest["name"].strip() else folder.name
    voice = manifest.get("voice") if isinstance(manifest.get("voice"), dict) else {}
    out["name"] = name.strip()
    out["voice"] = {k: str(voice.get(k) or "").strip() for k in ("engine", "voice")}
    sessions = [s for s in (memory.get("recent_sessions") or []) if isinstance(s, dict) and (s.get("summary") or s.get("title"))]
    profile = memory.get("rolling_profile") if isinstance(memory.get("rolling_profile"), str) else ""
    out["memory"] = {"profile": profile.strip(), "sessions": sessions}
    out["clips"] = _files(folder / "clips", CLIP_EXT)
    posters = [p for p in _files(folder, POSTER_EXT) if p.stem.lower() == "poster"]
    out["poster"] = posters[0] if posters else None
    out["docs"] = sorted(p for p in (folder / "docs").iterdir() if p.is_file() and not p.is_symlink()) if (folder / "docs").is_dir() else []
    chunks = folder / "rag" / "chunks.jsonl"
    out["chunks_file"] = chunks if chunks.is_file() and not chunks.is_symlink() else None
    out["skipped"] = [{"name": d.name, "bytes": _tree_size(d)} for d in sorted(folder.iterdir()) if d.is_dir() and not d.is_symlink() and d.name not in ("clips", "docs", "rag")]
    reasons = []
    if out["memory"]["profile"] or sessions:
        reasons.append("memory")
    if out["clips"] or out["poster"]:
        reasons.append("video-or-photo")
    out["personal"] = reasons
    return out


def _fields(p: Dict[str, Any], slug: str) -> tuple[Dict[str, Any], List[str]]:
    """The personality fields a manifest gives, cut to the sizes TaDaMoo keeps, and a note for each cut."""
    m, notes = p["manifest"], []
    data: Dict[str, Any] = {"name": p["name"], "voice": p["voice"]}
    for field in ("personality", "system_prompt"):
        text = str(m.get(field) or "").strip()
        if len(text) > FIELD_LIMITS[field]:
            text = text[:FIELD_LIMITS[field]]
            notes.append(f"{field} was cut to {FIELD_LIMITS[field]} characters")
        data[field] = text
    derived = str(m.get("personality_from_docs") or "").strip()
    if derived:
        data["profile"] = derived[:FIELD_LIMITS["profile"]]
        if len(derived) > FIELD_LIMITS["profile"]:
            notes.append(f"the profile from the documents was cut to {FIELD_LIMITS['profile']} characters")
    if m.get("knowledge_project"):
        notes.append("its knowledge project (wikibridge) has no equivalent and is not imported")
    if p["clips"] or p["poster"]:
        data["avatar"] = slug
    return data, notes


def _item(p: Dict[str, Any], personas: Any, rag: Any) -> Dict[str, Any]:
    folder = p["folder"]
    base = {"id": "persona:" + folder.name, "kind": "persona", "folder": folder.name, "problems": list(p["problems"]), "notes": list(p["notes"])}
    if p["manifest"] is None:
        return {**base, "name": folder.name, "slug": "", "personal": False, "personal_reasons": [], "conflict": "", "action": "skip"}
    slug = _slug(p["name"])
    item = {**base, "name": p["name"], "slug": slug, "voice": p["voice"], "personal": bool(p["personal"]), "personal_reasons": p["personal"],
            "parts": {"clips": {"count": len(p["clips"]), "bytes": sum(c.stat().st_size for c in p["clips"])},
                      "poster": p["poster"] is not None,
                      "memory": {"profile": bool(p["memory"]["profile"]), "sessions": len(p["memory"]["sessions"])},
                      "documents": {"count": len(p["docs"]), "bytes": sum(d.stat().st_size for d in p["docs"])},
                      "passages": _count_lines(p["chunks_file"])},
            "skipped": p["skipped"], "conflict": "", "action": "create"}
    item["bytes"] = item["parts"]["clips"]["bytes"] + item["parts"]["documents"]["bytes"] + (p["poster"].stat().st_size if p["poster"] else 0)
    try:
        data, notes = _fields(p, slug)
        item["notes"] += notes
        personas.check(data)                                                      # the store's own checks of the fields, nothing written
    except Exception as e:                                                        # noqa: BLE001 (the store says what is wrong with the fields)
        item["problems"].append(str(e))
    if not slug:
        item["problems"].append("the name has no letters or digits that can name a folder")
    if slug and personas.exists(slug):
        item["conflict"], item["action"] = "exists", "skip"
    if item["problems"]:
        item["action"] = "skip"
    return item


def _count_lines(path: Optional[Path]) -> int:
    if path is None:
        return 0
    try:
        with open(path, "rb") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _voice_item(path: Path, voices_dir: Optional[Path]) -> Dict[str, Any]:
    target = voices_dir / path.name if voices_dir else None
    conflict = "exists" if target is not None and target.exists() else ""
    return {"id": "voice:" + path.stem, "kind": "voice", "name": path.stem, "file": path.name, "bytes": path.stat().st_size, "personal": True,
            "personal_reasons": ["voice-recording"], "conflict": conflict, "action": "skip" if conflict else "create", "problems": [], "notes": []}


def _voice_files(root: Path) -> List[Path]:
    seen: Dict[str, Path] = {}
    for parts in VOICE_FOLDERS:
        for p in _files(root.joinpath(*parts), VOICE_EXT):
            seen.setdefault(p.name, p)
    return [seen[k] for k in sorted(seen)]


def plan(source: Any, personas: Any, rag: Any, voices_dir: Optional[Path] = None, forbidden: Iterable[str | Path] = ()) -> Dict[str, Any]:
    """What a run would do for this folder: one item for each persona and each cloned voice, with sizes and conflicts. Writes nothing."""
    root = source_root(source, forbidden)
    items: List[Dict[str, Any]] = []
    base = root.joinpath(*PERSONE)
    if base.is_dir():
        for folder in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()):
            items.append(_item(_read_persona(folder), personas, rag))
    items += [_voice_item(p, voices_dir) for p in _voice_files(root)]
    return {"source": str(root), "items": items, "bytes": sum(i.get("bytes", 0) for i in items), "personal": sum(1 for i in items if i["personal"])}


# ------------------------------------------------------------------------------------------------ the run
def _copy(src: Path, dst: Path, force: bool = False) -> bool:
    """Copies the file unless the target is already the same size (or `force`); True when it wrote. The target is replaced whole or not at all."""
    if not force and dst.is_file() and dst.stat().st_size == src.stat().st_size:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return True


def _memory(p: Dict[str, Any], memory: Any) -> Dict[str, int]:
    """The profile (a note for each paragraph) and the remembered sessions go into the personality's encrypted memory."""
    added = 0
    for para in [x.strip() for x in re.split(r"\n\s*\n", p["memory"]["profile"]) if x.strip()]:
        memory.add("long", "identity", para, origin="inferred", confidence=0.6, source="callAPIcall")
        added += 1
    for s in p["memory"]["sessions"]:
        when, title, summary = _ts(s.get("ts")), str(s.get("title") or "").strip(), str(s.get("summary") or "").strip()
        text = ": ".join(x for x in (title, summary) if x)
        memory.add("short", "topics", (when + " - " if when else "") + text, origin="inferred", confidence=0.6, source="callAPIcall")
        added += 1
    return {"notes": added}


def _archive(p: Dict[str, Any], name: str, rag: Any, warnings: List[str]) -> Optional[str]:
    """The knowledge archive of a persona: its documents, or, when none can be read, its saved passages. Returns the archive id."""
    if not p["docs"] and p["chunks_file"] is None:
        return None
    title = f"{name} (callAPIcall)"[:60]
    aid = next((a["id"] for a in rag.list() if a["name"].lower() == title.lower()), None)
    if aid is None:
        aid = rag.create(title, "Imported from callAPIcall")["id"]
    added = 0
    for doc in p["docs"]:
        try:
            rag.add_file(aid, doc.name, doc)
            added += 1
        except Exception as e:                                                    # noqa: BLE001
            if "already in the archive" in str(e):
                added += 1
            else:
                warnings.append(f"document {doc.name}: {e}")
    if added == 0 and p["chunks_file"] is not None:
        by_source: Dict[str, List[str]] = {}
        try:
            with open(p["chunks_file"], encoding="utf-8") as f:
                for line in f:
                    try:
                        c = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"].strip():
                        by_source.setdefault(str(c.get("source") or "passages"), []).append(c["text"].strip())
        except OSError as e:
            warnings.append(f"the saved passages could not be read: {e}")
        for source, texts in by_source.items():
            stem = re.sub(r"[\\/\x00-\x1f]", "_", Path(source).stem or "passages")[:100]
            try:
                rag.add_bytes(aid, stem + ".txt", "\n\n".join(texts).encode("utf-8"))
                added += 1
            except Exception as e:                                                # noqa: BLE001
                if "already in the archive" in str(e):
                    added += 1
                else:
                    warnings.append(f"passages of {stem}: {e}")
    return aid


def _run_persona(item: Dict[str, Any], p: Dict[str, Any], personas: Any, rag: Any, avatars_dir: Path) -> Dict[str, Any]:
    slug = item["slug"]
    exists = personas.exists(slug)
    data, notes = _fields(p, slug)
    warnings: List[str] = list(notes)
    if exists:
        persona = personas.update(slug, data)
        status = "updated"
    else:
        persona = personas.create(data)
        status = "created"
    result: Dict[str, Any] = {"status": status, "slug": persona["slug"], "warnings": warnings}
    try:
        aid = _archive(p, p["name"], rag, warnings)
        if aid is not None:
            links = list(persona.get("knowledge") or [])
            if aid not in links:
                personas.update(persona["slug"], {"knowledge": links + [aid]})
            result["archive"] = aid
    except Exception as e:                                                        # noqa: BLE001
        warnings.append(f"knowledge archive: {e}")
    try:
        result["memory"] = _memory(p, personas.memory(persona["slug"]))
    except Exception as e:                                                        # noqa: BLE001
        warnings.append(f"memory: {e}")
    try:
        target = avatars_dir / persona["slug"]
        copied = [_copy(c, target / "clips" / c.name) for c in p["clips"]]
        if p["poster"] is not None:
            copied.append(_copy(p["poster"], target / ("poster" + p["poster"].suffix.lower())))
        result["media"] = {"files": len(copied), "written": sum(copied)}
    except OSError as e:
        warnings.append(f"clips and poster: {e.strerror or e}")
    return result


def _run_voice(item: Dict[str, Any], root: Path, opt: Dict[str, Any], voices_dir: Optional[Path]) -> Dict[str, Any]:
    if voices_dir is None:
        return {"status": "failed", "message": "there is no models folder for the voices"}
    src = next((p for p in _voice_files(root) if p.name == item["file"]), None)
    if src is None:
        return {"status": "failed", "message": "the sample is no longer there"}
    dst = voices_dir / src.name
    if dst.exists() and not opt.get("replace"):
        return {"status": "skipped", "message": "a voice with that name already exists"}
    existed = dst.exists()
    _copy(src, dst, force=True)
    return {"status": "updated" if existed else "created"}


def run(source: Any, items: Any, personas: Any, rag: Any, avatars_dir: Path, voices_dir: Optional[Path] = None,
        forbidden: Iterable[str | Path] = ()) -> Dict[str, Any]:
    """Does what the person chose. `items` is a list of {id, replace?, confirm_personal?} taken from the plan; an item that holds personal data
    is done only with `confirm_personal: true`. Reports each item: created, updated, skipped, needs_confirmation or failed."""
    root = source_root(source, forbidden)
    if not isinstance(items, list) or not items or len(items) > 100:
        raise SourceError("items: choose what to import from the plan")
    known = {i["id"]: i for i in plan(str(root), personas, rag, voices_dir, forbidden)["items"]}
    results: List[Dict[str, Any]] = []
    for want in items:
        if not isinstance(want, dict) or not isinstance(want.get("id"), str) or set(want) - {"id", "replace", "confirm_personal"}:
            raise SourceError("items: each one is {id, replace, confirm_personal}")
        item = known.get(want["id"])
        if item is None:
            results.append({"id": want["id"], "status": "failed", "message": "the plan has no such item"})
            continue
        replace, confirmed = want.get("replace") is True, want.get("confirm_personal") is True
        out: Dict[str, Any] = {"id": item["id"], "name": item["name"]}
        try:
            if item["problems"]:
                out.update(status="failed", message="; ".join(item["problems"]))
            elif item["personal"] and not confirmed:
                out.update(status="needs_confirmation", message="this holds personal data (" + ", ".join(item["personal_reasons"]) + "): confirm it to import it")
            elif item["conflict"] and not replace:
                out.update(status="skipped", message="it already exists: choose replace to update it")
            elif item["kind"] == "persona":
                out.update(_run_persona(item, _read_persona(root.joinpath(*PERSONE, item["folder"])), personas, rag, Path(avatars_dir)))
            else:
                out.update(_run_voice(item, root, want, voices_dir))
        except Exception as e:                                                    # noqa: BLE001 (one item failing leaves the others to run)
            out.update(status="failed", message=str(e))
        results.append(out)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"source": str(root), "results": results, "counts": counts}
