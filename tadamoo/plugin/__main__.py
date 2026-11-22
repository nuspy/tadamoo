"""A plugin's program from a shell.

    python -m tadamoo.plugin list
    python -m tadamoo.plugin run NAME [command] [arguments...]

`run` starts the command line of an imported plugin that brings a program (its `plugin.json` has a `python` section), from the plugin's own copy in the user data folder, so
it works from any working folder. `list` says which plugins bring one and what they offer. The user data folder is TADAMOO_DATA, else the app's; `--data-dir` names another."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import enabled, installed, run_cli


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tadamoo.plugin", description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="", help="the user data folder (default: the app's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    run = sub.add_parser("run")
    run.add_argument("name")
    run.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    if args.data_dir:
        data = Path(args.data_dir)
    else:
        from serve import userdata
        data = userdata.user_data_dir()
    cap_dir = data / "capabilities"
    if args.cmd == "list":
        print(json.dumps([{"name": p.name, "on": enabled(cap_dir, p), "folder": str(p.folder), "tools": [t.get("name") for t in p.python.get("tools", []) if isinstance(t, dict)],
                           "commands": sorted((p.python.get("cli") or {}))} for p in installed(cap_dir)], indent=1, ensure_ascii=False))
        return 0
    return run_cli(cap_dir, args.name, args.rest)


if __name__ == "__main__":
    sys.exit(main())
