#!/usr/bin/env python3
"""Usage emitter of the Basalt harness: the driver already writes /data/usage.jsonl from the harness's own token counts, so this only makes sure the file
exists and, when the driver died before writing it, rebuilds one line from /data/basalt-result.json. No price is looked up: the model is local."""
from __future__ import annotations

import json
import sys
from pathlib import Path

DATA = Path("/data")


def main(argv=None) -> int:
    usage = DATA / "usage.jsonl"
    if usage.exists() and usage.stat().st_size > 0:
        return 0
    rows = []
    try:
        tokens = (json.loads((DATA / "basalt-result.json").read_text(encoding="utf-8")).get("tokens") or {})
    except (OSError, ValueError):
        tokens = {}
    prompt, completion = int(tokens.get("prompt") or 0), int(tokens.get("completion") or 0)
    if prompt or completion:
        rows.append({"type": "usage", "call_id": "basalt-total", "input_tokens": prompt, "output_tokens": completion})
    usage.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
