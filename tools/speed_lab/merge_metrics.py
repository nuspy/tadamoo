"""Merges the metrics files of a speed study (keyed by clip path) into metrics.json keyed by cell name: run 1 of each cell.

    python merge_metrics.py DIR FILE.json [FILE.json ...]"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1])
    out: dict = {}
    for name in sys.argv[2:]:
        for path, values in json.loads((root / name).read_text(encoding="utf-8")).items():
            m = re.search(r"/([^/]+)/run(\d+)/take\.mp4$", path.replace("\\", "/"))
            if m and m.group(2) == "1":
                out.setdefault(m.group(1), {}).update(values)
    (root / "metrics.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(sorted(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
