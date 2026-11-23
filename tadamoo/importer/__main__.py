"""The import service from a shell. Every command prints JSON; a problem is printed on stderr and the exit code is 1.

    python -m tadamoo.importer list [skill|plugin|mcp|plan]
    python -m tadamoo.importer verify KIND PATH [--server NAME]
    python -m tadamoo.importer import KIND PATH [--server NAME] [--with-dependencies] [--adopt]
    python -m tadamoo.importer reimport KIND NAME [--yes] [--overwrite-local] [--with-dependencies]
    python -m tadamoo.importer update-plan KIND NAME [--source PATH] [--instruction TEXT]
    python -m tadamoo.importer update-apply TOKEN [--choice merge|overwrite|cancel] [--resolve FILE=ours|theirs|merged]
    python -m tadamoo.importer delete KIND NAME
    python -m tadamoo.importer enable|disable NAME
    python -m tadamoo.importer migrate SKILLS_ROOT [--dry-run] [--update]

The copies live in the user data folder (TADAMOO_DATA, else the app's folder; `--data-dir` names another). `reimport` without `--yes` only says what
would change. `migrate` imports a folder of skills that the harness used to read directly (for example D:/skills) and takes it out of the
settings; the folder is not changed. What ships with the program, and what is installed, enabled or removed from an address or a folder by choice, is the skill manager's
(`python -m tadamoo.skillmanager`, docs/skills-manager.md). A running app sees the change at its next scan; restart it only for a new version of the app itself."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import Problem
from .migrate import migrate
from .service import KINDS, Importer, Spaces


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tadamoo.importer", description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="", help="the user data folder (default: the app's)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("kind", nargs="?", choices=KINDS)
    for name in ("verify", "import"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=KINDS)
        p.add_argument("path")
        p.add_argument("--server", default="")
        p.add_argument("--with-dependencies", action="store_true")
        p.add_argument("--adopt", action="store_true")
    re = sub.add_parser("reimport")
    re.add_argument("kind", choices=KINDS)
    re.add_argument("name")
    re.add_argument("--yes", action="store_true")
    re.add_argument("--overwrite-local", action="store_true")
    re.add_argument("--with-dependencies", action="store_true")
    up = sub.add_parser("update-plan")
    up.add_argument("kind", choices=("skill", "plugin", "mcp"))
    up.add_argument("name")
    up.add_argument("--source", default="", help="read this source instead of the recorded one")
    up.add_argument("--instruction", action="append", default=[], help="how to change the merge (needs a model; the server's, so use the app for it)")
    ua = sub.add_parser("update-apply")
    ua.add_argument("token")
    ua.add_argument("--choice", choices=("merge", "overwrite", "cancel"), default="merge")
    ua.add_argument("--resolve", action="append", default=[], metavar="FILE=ours|theirs|merged", help="what to do with a file both sides changed in the same place")
    de = sub.add_parser("delete")
    de.add_argument("kind", choices=KINDS)
    de.add_argument("name")
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("name")
    mg = sub.add_parser("migrate")
    mg.add_argument("skills_root")
    mg.add_argument("--dry-run", action="store_true")
    mg.add_argument("--update", action="store_true", help="re-import what is already imported and whose source changed")
    a = ap.parse_args(argv)
    if a.data_dir:
        data = Path(a.data_dir)
    else:
        from serve import userdata
        data = userdata.user_data_dir()
    imp = Importer(Spaces.from_data(data))
    try:
        if a.cmd == "list":
            out = {"imported": imp.list(a.kind)}
        elif a.cmd in ("verify", "import"):
            opts = {"server": a.server, "with_dependencies": a.with_dependencies, "adopt": a.adopt}
            out = imp.verify(a.kind, a.path, **opts) if a.cmd == "verify" else imp.import_(a.kind, a.path, **opts)
        elif a.cmd == "reimport":
            out = imp.reimport(a.kind, a.name, confirm=a.yes, overwrite_local=a.overwrite_local, with_dependencies=a.with_dependencies)
        elif a.cmd == "update-plan":
            out = imp.update_plan(a.kind, a.name, source=a.source or None, instructions=a.instruction)
        elif a.cmd == "update-apply":
            out = imp.update_apply(a.token, a.choice, resolutions=dict(r.rsplit("=", 1) for r in a.resolve))
        elif a.cmd == "delete":
            out = imp.delete(a.kind, a.name)
        elif a.cmd in ("enable", "disable"):
            out = imp.set_enabled(a.name, a.cmd == "enable")
        else:
            out = migrate(imp, a.skills_root, dry_run=a.dry_run, update=a.update)
    except Problem as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
