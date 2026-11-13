"""The knowledge archives from a shell.

    python -m tadamoo.rag db status [--project KEY]
    python -m tadamoo.rag db config --project KEY --kind sqlite|postgres|mysql [--path FILE] [--host H --port N --dbname D --user U --sslmode M] [--password-from-stdin]
                                    [--prefix rag_] [--vectors auto|blob|file --vec-file FILE]
    python -m tadamoo.rag db prepare --project KEY [--plan-b blob|file] [--vec-file FILE] [--install-local] [--create-db]
                                    [--superuser U [--superpassword-from-stdin]]
    python -m tadamoo.rag db import [ARCHIVE ...] [--all] [--source DIR] [--replace]
    python -m tadamoo.rag db copy --project KEY --from-sqlite FILE [ARCHIVE ...] [--replace]
    python -m tadamoo.rag db components [install ID]

Every project has a database (a SQLite file of its own unless it is set to another). `status` says which and what is in it, and which archives in the old file format are
waiting to be imported; `config` sets a project's database (the password is read from standard input and goes in the vault, never in a file); `prepare` runs the three
steps (connect, vector extension, tables and a test) and saves the configuration when they work; `import` copies archives in the old file format into their project's
database and reports what it copied. `copy` copies archives from another SQLite file into the project's database (the file is only read). `--superuser` gives `prepare` a PostgreSQL administrator for this run only, to make the user, the database, the rights and the
extension the project's user cannot; its password is read from standard input or the environment variable TADAMOO_SUPERPASSWORD, and is kept nowhere. The old folders are never changed or deleted. `--rag-dir` names another rag folder (default: the user data folder's)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tadamoo.rag import RagStore
from tadamoo.rag.backends import clean_config, components, legacy, prepare as prepare_mod, store_password, vault_password
from tadamoo.rag.errors import RagError


def _paths(rag_dir: str):
    from serve import userdata
    return Path(rag_dir) if rag_dir else userdata.path("rag"), userdata.path("vault")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tadamoo.rag", description=__doc__.splitlines()[0])
    ap.add_argument("--rag-dir", default="")
    sub = ap.add_subparsers(dest="area", required=True)
    db = sub.add_parser("db").add_subparsers(dest="cmd", required=True)
    st = db.add_parser("status")
    st.add_argument("--project", default=None)
    cf = db.add_parser("config")
    cf.add_argument("--project", required=True)
    cf.add_argument("--kind", required=True, choices=("sqlite", "postgres", "mysql"))
    for name in ("path", "host", "port", "dbname", "user", "sslmode", "prefix", "vectors"):
        cf.add_argument("--" + name.replace("_", "-"))
    cf.add_argument("--vec-file")
    cf.add_argument("--password-from-stdin", action="store_true")
    pp = db.add_parser("prepare")
    pp.add_argument("--project", required=True)
    pp.add_argument("--plan-b", choices=("blob", "file"))
    pp.add_argument("--vec-file")
    pp.add_argument("--install-local", action="store_true")
    pp.add_argument("--create-db", action="store_true")
    pp.add_argument("--superuser")
    pp.add_argument("--superpassword-from-stdin", action="store_true")
    cp = db.add_parser("copy")
    cp.add_argument("--project", required=True)
    cp.add_argument("--from-sqlite", required=True)
    cp.add_argument("archives", nargs="*")
    cp.add_argument("--replace", action="store_true")
    co = db.add_parser("components")
    co.add_argument("action", nargs="?", choices=("install",))
    co.add_argument("id", nargs="?")
    im = db.add_parser("import")
    im.add_argument("archives", nargs="*")
    im.add_argument("--all", action="store_true")
    im.add_argument("--source", default="")
    im.add_argument("--replace", action="store_true")
    a = ap.parse_args(argv)
    rag_dir, vault_dir = _paths(a.rag_dir)
    store = RagStore(rag_dir, password=vault_password(vault_dir))
    router = store.router
    try:
        if a.cmd == "status":
            projects = [a.project] if a.project is not None else [("" if n == "none" else n) for n in router.projects()]
            out = {"rag_dir": str(rag_dir), "projects": [], "waiting": [p.name for p in store.legacy()]}
            for project in projects:
                cfg = router.config(project)
                row = {"project": project or "none", "kind": cfg["kind"]}
                try:
                    backend = router.for_project(project)
                    row.update(info=backend.info(), archives=len(backend.archive_ids()))
                except RagError as e:
                    row["error"] = str(e)
                out["projects"].append(row)
        elif a.cmd == "config":
            cfg = {"kind": a.kind, **{k: getattr(a, k) for k in ("path", "host", "port", "dbname", "user", "sslmode", "prefix", "vectors", "vec_file") if getattr(a, k, None)}}
            if a.kind == "sqlite" and "path" not in cfg:
                cfg["path"] = str(Path(rag_dir) / "db" / f"{router._name(a.project)}.sqlite")
            if a.password_from_stdin:
                cfg["vault"] = store_password(vault_dir, a.project, sys.stdin.readline().rstrip("\r\n"))
            out = {"saved": router.set_config(a.project, clean_config(cfg))}
        elif a.cmd == "components":
            if a.action == "install":
                out = components.install(a.id or "", lambda line: print(line, file=sys.stderr))
            else:
                out = {"components": components.listing()}
        elif a.cmd == "copy":
            from tadamoo.rag.backends.sqlite_vec import SqliteVecBackend
            from tadamoo.rag.backends.transfer import copy_archive
            if not Path(a.from_sqlite).is_file():
                raise RagError(f"{a.from_sqlite} is not a file")
            source, target = SqliteVecBackend(a.from_sqlite), router.for_project(a.project)
            try:
                out = {"copied": [copy_archive(source, target, aid, a.replace) for aid in (a.archives or source.archive_ids())]}
            finally:
                source.close()
        elif a.cmd == "prepare":
            cfg = router.config(a.project)
            options = {"plan_b": a.plan_b, "vec_file": a.vec_file, "install_local": a.install_local, "create_db": a.create_db}
            if a.superuser:
                options["superuser"] = a.superuser
                options["superpassword"] = sys.stdin.readline().rstrip("\r\n") if a.superpassword_from_stdin else os.environ.get("TADAMOO_SUPERPASSWORD", "")
            done = prepare_mod.prepare(cfg, vault_password(vault_dir)(cfg["vault"]) if cfg.get("vault") else "",
                                       lambda s: print(f"{s['step']}: {s['state']} - {s['message']}", file=sys.stderr),
                                       options)
            if done["ok"]:
                router.set_config(a.project, done.get("config") or cfg)
            out = done
        else:
            folders = [Path(a.source or rag_dir) / x for x in a.archives] if a.archives else (legacy.legacy_folders(a.source or rag_dir) if a.all else [])
            if not folders:
                raise RagError("name the archives to import, or --all")
            out = {"imported": [store.import_archive(f, a.replace) for f in folders]}
    except RagError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    finally:
        router.close()
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
