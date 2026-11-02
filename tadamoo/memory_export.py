"""The personal memory as Markdown the person can read and edit, one file per note, like a vault of notes.

The encrypted store stays the master: the export is a copy, written again each time and never read back. Each file has a small front
matter (id, kind, section, origin, dates, the conversations it was learned from, tags) and the note as its text; `index.md` lists them all.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from tadamoo.personal_memory import PersonalMemory

KINDS = ("long", "short")
SLUG_WORDS = 6
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def _day(stamp: Any) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(stamp))) if stamp else ""


def _slug(text: str) -> str:
    return _NOT_SLUG.sub("-", " ".join(text.lower().split()[:SLUG_WORDS])).strip("-")[:40]


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def file_name(item: Dict[str, Any]) -> str:
    """`<section>-<first words>-<id>.md`: readable, and the id at the end ties the file to its note."""
    return "-".join(p for p in (item["section"], _slug(item["text"]), item["id"]) if p) + ".md"


def _scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render(item: Dict[str, Any]) -> str:
    """One note as a Markdown file: front matter, then the text."""
    conversations = [c for c in item.get("conversations") or [] if isinstance(c, str)]
    tags = [item["section"]] + [t for t in item.get("tags") or [] if isinstance(t, str) and t != item["section"]]
    lines = ["---", f"id: {_scalar(item['id'])}", f"kind: {item['kind']}", f"section: {item['section']}", f"origin: {item['origin']}",
             f"confidence: {round(float(item.get('confidence', 1.0)), 2)}", f"created: {_day(item.get('created'))}", f"updated: {_day(item.get('updated'))}",
             f"seen: {_day(item.get('seen'))}"]
    if item.get("source"):
        lines.append(f"project: {_scalar(item['source'])}")
    if conversations:
        lines.append(f"source_conversation: {_scalar(conversations[0])}")
        if len(conversations) > 1:
            lines.append(f"conversations: [{', '.join(_scalar(c) for c in conversations)}]")
    lines += [f"tags: [{', '.join(_scalar(t) for t in tags)}]", "---", "", item["text"], ""]
    return "\n".join(lines)


def export_markdown(memory: PersonalMemory, folder: str | Path) -> Dict[str, Any]:
    """Writes every note into `folder` (`long/`, `short/` and `index.md`), replacing what an earlier export wrote there. Returns the folder and the counts."""
    root = Path(folder)
    items = memory.items()
    for kind in KINDS:
        (root / kind).mkdir(parents=True, exist_ok=True)
        for stale in (root / kind).glob("*.md"):
            stale.unlink()
    index: List[str] = ["# Memories", "", f"Exported {time.strftime('%Y-%m-%d %H:%M')}. The encrypted store is the master; changes made here are not read back.", ""]
    counts = {kind: 0 for kind in KINDS}
    for kind in KINDS:
        rows = [i for i in items if i["kind"] == kind]
        index += [f"## {'Long-term' if kind == 'long' else 'Short-term'} ({len(rows)})", ""]
        for item in sorted(rows, key=lambda r: (r["section"], -float(r.get("created") or 0))):
            name = file_name(item)
            _write(root / kind / name, render(item))
            index.append(f"- [{item['section']}] [{item['text'].replace('[', '(').replace(']', ')')}]({kind}/{name})")
            counts[kind] += 1
        index.append("")
    _write(root / "index.md", "\n".join(index))
    return {"folder": str(root), "notes": counts["long"] + counts["short"], "long": counts["long"], "short": counts["short"]}

