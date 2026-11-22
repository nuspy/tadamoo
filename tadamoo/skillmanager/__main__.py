"""The skill manager from a shell: the same code as the Settings > Skills tab and the `/install-skill` command.

    python -m tadamoo.skillmanager list [--json]
    python -m tadamoo.skillmanager plan ADDRESS
    python -m tadamoo.skillmanager install NAME ... [--no-models]               built-in skills, each with what it requires
    python -m tadamoo.skillmanager install --defaults | --all-builtin           the ones the setup ticks | every built-in one
    python -m tadamoo.skillmanager install ADDRESS [--only ID ...] [--all]      a folder, a GitHub address or a web address
    python -m tadamoo.skillmanager install --token TOKEN [--only ID ...] [--all]   what `plan` read (a skill the model wrote from a page is installed this way)
    python -m tadamoo.skillmanager remove NAME
    python -m tadamoo.skillmanager enable NAME | disable NAME
    python -m tadamoo.skillmanager update NAME [--check] [--overwrite-local]
    python -m tadamoo.skillmanager update-plan NAME [--instruction TEXT ...]    the update merged with what you changed; prints the three versions, changes nothing
    python -m tadamoo.skillmanager update-apply TOKEN [--choice merge|overwrite|cancel] [--resolve FILE=ours|theirs|merged ...] [--remember]
    python -m tadamoo.skillmanager models NAME [--check]                        download (or only check) the models of an installed skill

Only what is named is installed: no command installs, imports or downloads a skill that was not asked for. The state is in the user data folder (TADAMOO_DATA, else the
app's folder; `--data-dir` names another). A running app reads a change at its next scan, or at once through the Settings tab or `/install-skill`."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tadamoo.importer.common import Problem

from . import catalogue
from .jobs import Cancelled, Job
from .manager import SkillManager

STATE_TEXT = {"not_installed": "not installed", "installed": "installed", "disabled": "installed, turned off"}


def _gb(row: Dict[str, Any]) -> str:
    return f"about {row['download_gb']:g} GB of models" if row.get("download_gb") else ""


def _print_list(rows: List[Dict[str, Any]]) -> None:
    width = max([len(r["name"]) for r in rows] + [4])
    for r in rows:
        extra = [x for x in (_gb(r) if r["state"] == "not_installed" or r.get("models") == "missing" else "", "models: " + r["models"] if r.get("models") not in (None, "none") and r["state"] != "not_installed" else "",
                             "needs " + ", ".join(r["requires"]) if r["requires"] and r["state"] == "not_installed" else "") if x]
        print(f"{r['name']:<{width}}  {STATE_TEXT[r['state']]:<22} {'; '.join(extra)}")


def _plan_text(plan: Dict[str, Any]) -> None:
    print(f"From {plan['address']} ({plan['type']}){', written by the model from the page' if plan['generated'] else ''}:")
    for c in plan["candidates"]:
        flag = c.get("error") or ("; ".join(c.get("blockers", [])) or ("already installed: it will be replaced" if c.get("already_installed") else ""))
        print(f"  {c['id']:<30} {c['kind']:<7} {c['name']}  {c.get('files', 0)} files, {c.get('bytes', 0) // 1024} KB  {flag}")
        if c.get("skill_md"):
            print("  ---\n" + "\n".join("  " + ln for ln in c["skill_md"].splitlines()) + "\n  ---")


def _client(args: argparse.Namespace) -> Any:
    from tadamoo.client import TadamooClient
    return TadamooClient(base_url=args.url, api_key=args.key or None)


def run(args: argparse.Namespace, out=print) -> int:
    data = Path(args.data_dir) if args.data_dir else None
    if data is None:
        from serve import userdata
        data = userdata.user_data_dir()
    mgr = SkillManager(data, program=args.program_dir or None, model=lambda: _client(args))
    job = Job(args.cmd, [], echo=lambda line: print(line, file=sys.stderr, flush=True))
    cmd = args.cmd
    if cmd == "list":
        rows = mgr.list()
        if args.json:
            out(json.dumps({"skills": rows}, ensure_ascii=False, indent=1))
        else:
            _print_list(rows)
        return 0
    if cmd == "plan":
        plan = mgr.plan(args.address)
        if args.json:
            out(json.dumps(plan, ensure_ascii=False, indent=1))
        else:
            _plan_text(plan)
            print(f"token: {plan['token']}")
        return 0
    if cmd == "install":
        return _install(mgr, args, job, out)
    if cmd in ("enable", "disable"):
        row = mgr.set_enabled(args.name, cmd == "enable")
        out(json.dumps(row, ensure_ascii=False) if args.json else f"{row['name']}: {STATE_TEXT[row['state']]}")
        return 0
    if cmd == "remove":
        res = mgr.remove(args.name)
        if args.json:
            out(json.dumps(res, ensure_ascii=False))
        else:
            out(f"{args.name}: removed." + (" Its models stay on the disk." if res["models_kept"] else "")
                + (f" Needed by {', '.join(res['needed_by'])}: they will not work until it is installed again." if res["needed_by"] else ""))
        return 0
    if cmd == "update":
        res = mgr.update(args.name, overwrite_local=args.overwrite_local, check_only=args.check)
        out(json.dumps(res, ensure_ascii=False) if args.json else f"{args.name}: {res['status']}")
        return 0
    if cmd == "update-plan":
        res = mgr.update_plan(args.name, args.instruction, args.language)
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if cmd == "update-apply":
        res = mgr.update_apply(args.token, args.choice, dict(r.rsplit("=", 1) for r in args.resolve), args.remember)
        out(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if cmd == "models":
        if args.check:
            res = mgr.check_models(args.name)
            out(json.dumps(res, ensure_ascii=False, indent=1) if args.json else f"{args.name}: models {'all there' if res['ready'] else 'missing: ' + ', '.join(r['id'] for r in res['components'] if not r['present'])}")
            return 0 if res["ready"] else 1
        res = mgr.download_models(args.name, job)
        out(json.dumps(res, ensure_ascii=False) if args.json else f"{args.name}: models {'ready' if res['ready'] else 'not all there'}")
        return 0 if res["ready"] else 1
    return 2


def _install(mgr: SkillManager, args: argparse.Namespace, job: Job, out) -> int:
    entries = catalogue.load(args.program_dir or None)
    names = {e["name"] for e in entries}
    token, targets = args.token, list(args.targets)
    if args.defaults or args.all_builtin or (targets and all(t in names for t in targets)):
        chosen = [e["name"] for e in entries] if args.all_builtin else catalogue.defaults(entries) if args.defaults else targets
        res = mgr.install(chosen, models=not args.no_models, job=job, overwrite_local=args.overwrite_local)
    else:
        if not token:
            if len(targets) != 1:
                print("Name built-in skills (python -m tadamoo.skillmanager list), or give one address.", file=sys.stderr)
                return 2
            plan = mgr.plan(targets[0])
            token = plan["token"]
            if not args.json:
                _plan_text(plan)
            if plan["generated"]:
                print(f"The model wrote this from the page. If it is right: python -m tadamoo.skillmanager install --token {token}" + (" --all" if len(plan["candidates"]) > 1 else ""), file=sys.stderr)
                return 3
        plan = mgr.saved_plan(token)
        only: Optional[List[str]] = list(args.only) or None
        if only is None and len(plan["candidates"]) > 1 and not args.all:
            if args.token:
                _plan_text(plan)
            print("Several skills were found: choose with --only ID (repeat) or take every one with --all.", file=sys.stderr)
            return 2
        res = mgr.install(token=token, only=only, models=not args.no_models, job=job, overwrite_local=args.overwrite_local)
    out(json.dumps(res, ensure_ascii=False, indent=1) if args.json else _install_text(res))
    return 1 if res["problems"] or any(not m["ready"] for m in res["models"]) else 0


def _install_text(res: Dict[str, Any]) -> str:
    lines = [f"{label}: {', '.join(res[key])}" for key, label in (("installed", "installed"), ("updated", "updated"), ("same", "already up to date")) if res[key]]
    lines += [f"models of {m['name']}: " + ("ready" if m["ready"] else "not all there" + (f" ({'; '.join(m['by_hand'])})" if m["by_hand"] else "")) for m in res["models"]]
    lines += [f"not installed: {p['name']}: {p['why']}" for p in res["problems"]]
    return "\n".join(lines) or "nothing to do"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tadamoo.skillmanager", description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="", help="the user data folder (default: the app's)")
    ap.add_argument("--program-dir", default="", help="the program's skills folder (default: the one beside the code)")
    ap.add_argument("--url", default=os.environ.get("TADAMOO_URL") or "http://127.0.0.1:7232", help="the server whose model writes a skill from a page")
    ap.add_argument("--key", default=os.environ.get("TADAMOO_API_KEY", ""))
    ap.add_argument("--json", action="store_true", help="print the answer as JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("plan").add_argument("address")
    ins = sub.add_parser("install")
    ins.add_argument("targets", nargs="*", help="built-in skill names, or one address")
    ins.add_argument("--token", default="", help="what `plan` read")
    ins.add_argument("--only", action="append", default=[], help="an id from the list of what an address holds (repeat)")
    ins.add_argument("--all", action="store_true", help="every skill an address holds")
    ins.add_argument("--defaults", action="store_true", help="the built-in skills the setup ticks")
    ins.add_argument("--all-builtin", action="store_true", help="every built-in skill")
    ins.add_argument("--no-models", action="store_true", help="copy only; download the models later (models NAME)")
    ins.add_argument("--overwrite-local", action="store_true", help="replace a copy that was edited here")
    for name in ("remove", "enable", "disable"):
        sub.add_parser(name).add_argument("name")
    up = sub.add_parser("update")
    up.add_argument("name")
    up.add_argument("--check", action="store_true", help="only say what would change")
    up.add_argument("--overwrite-local", action="store_true")
    mp = sub.add_parser("update-plan", help="the update merged with what you changed: three versions of each file; nothing is changed")
    mp.add_argument("name")
    mp.add_argument("--instruction", action="append", default=[], help="how to change the merge (repeat)")
    mp.add_argument("--language", default="en", choices=("en", "it", "hu"))
    ma = sub.add_parser("update-apply", help="do what you chose for a proposed update")
    ma.add_argument("token")
    ma.add_argument("--choice", choices=("merge", "overwrite", "cancel"), default="merge")
    ma.add_argument("--resolve", action="append", default=[], metavar="FILE=ours|theirs|merged")
    ma.add_argument("--remember", action="store_true", help="keep the instructions for the next updates")
    mo = sub.add_parser("models")
    mo.add_argument("name")
    mo.add_argument("--check", action="store_true", help="only check what is there")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Cancelled:
        print("stopped", file=sys.stderr)
        return 130
    except Problem as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
