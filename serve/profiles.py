"""Saved configurations, shared by the web UI, the desktop app and the start scripts: named load profiles (the
Loader's engine fields, the port and, when saved with one, the model file) and named inference presets (the chat
settings). They live in one JSON file in the basalt cache directory (outside the repository). "Valori sicuri" is a
built-in load profile: it cannot be changed or deleted, it keeps the model the config file has, and it is the default
until another profile is chosen.

    python -m serve.profiles list
    python -m serve.profiles apply NAME --config basalt_config.json [--model FILE.basalt]   # write NAME into the file
    python -m serve.profiles default
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import loader, settings  # noqa: E402

SAFE_NAME = "Valori sicuri"
KINDS = ("load", "inference")
NAME_MAX = 60
DEFAULT_PORT = 7232
_lock = threading.Lock()
LOAD_FIELDS = [f"{s}.{k}" for s, k in settings.LOADER_FIELDS]
# the chat settings a preset keeps (serve/web/chatset.js)
INFERENCE_KEYS = ("system", "stop", "policy", "threshold", "effort", "temperature", "top_p", "top_k", "min_p",
                  "repetition_penalty", "presence_penalty", "frequency_penalty", "penalty_last_n", "seed",
                  "reasoning_budget_tokens", "max_tokens", "format", "schema", "schema_name", "retries")


def path() -> Path:
    from serve.engine.modelfile import cache_root
    return cache_root() / "profiles.json"


def _read() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for kind in KINDS:
        if not isinstance(data.get(kind), dict):
            data[kind] = {}
    if not isinstance(data.get("default"), str):
        data["default"] = SAFE_NAME
    return data


def _write(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="profiles.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def safe_values(port: int = DEFAULT_PORT, facts: dict | None = None, system: dict | None = None) -> dict:
    """Valori sicuri for this PC now and the model (`facts`; the demo sizes without one), computed by loader.safe()."""
    facts = facts or loader.DEMO_FACTS
    system = loader.system_now() if system is None else system
    return {**{k: v for k, v in loader.safe(facts, system).items() if v is not None}, "server.port": port}


def check_name(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name: give the profile a name")
    name = " ".join(name.split())
    if len(name) > NAME_MAX:
        raise ValueError(f"name: at most {NAME_MAX} characters")
    return name


def check_load(values) -> dict:
    """A load profile's values, each checked as the config file would be; None/absent = the engine's default."""
    if not isinstance(values, dict):
        raise ValueError("values: an object of Loader fields")
    out = {}
    for name, value in values.items():
        if name not in LOAD_FIELDS:
            raise ValueError(f"{name}: not a field a load profile keeps")
        if value is None or value == "":
            continue
        section, key = name.split(".", 1)
        out[name] = settings.check_value(section, key, value)
    return out


def check_model(value) -> str | None:
    """A load profile's model: None (the config file's model stays) or a .basalt file the engine can open."""
    if value is None or value == "":
        return None
    from serve import models
    try:
        return models.check_basalt(value)
    except ValueError as e:
        raise ValueError(f"model: {e}") from None


def _model_facts(model: str | None, fallback: dict | None) -> dict | None:
    if not model:
        return fallback
    try:
        from serve import models
        return models.facts_for(model)
    except (OSError, ValueError, KeyError):
        return fallback


def check_inference(values) -> dict:
    if not isinstance(values, dict):
        raise ValueError("settings: an object of chat settings")
    out = {k: v for k, v in values.items() if k in INFERENCE_KEYS}
    if len(json.dumps(out)) > 256 << 10:
        raise ValueError("settings: larger than 256 KiB")
    return out


def listing(system: dict | None = None, facts: dict | None = None) -> dict:
    """Both kinds, the built-in first, each load profile with a one-line summary (context, KV, spec, VRAM, speed)."""
    data = _read()
    facts = facts or loader.DEMO_FACTS
    loads = [{"name": SAFE_NAME, "values": safe_values(facts=facts, system=system or {}), "builtin": True,
              "model": None}]
    loads += [{"name": n, "values": p.get("values", {}), "builtin": False, "saved": p.get("saved"),
               "model": p.get("model") if isinstance(p.get("model"), str) else None}
              for n, p in sorted(data["load"].items(), key=lambda kv: kv[0].lower())]
    names = {p["name"] for p in loads}
    default = data["default"] if data["default"] in names else SAFE_NAME
    for p in loads:
        p["default"] = p["name"] == default
        p["summary"] = summary(p["values"], _model_facts(p["model"], facts), system)
        if p["model"]:
            p["summary"]["model"] = Path(p["model"]).stem
            p["summary"]["model_missing"] = not os.path.isfile(p["model"])
    infer = [{"name": n, "settings": p.get("settings", {}), "saved": p.get("saved"),
              "summary": inference_summary(p.get("settings", {}))}
             for n, p in sorted(data["inference"].items(), key=lambda kv: kv[0].lower())]
    return {"load": loads, "inference": infer, "default": default, "file": str(path())}


def summary(values: dict, facts: dict, system: dict | None) -> dict:
    v = loader.effective(values)
    ctx = int(v["engine.max-context"])
    parts = [f"{ctx // 1024}K" if ctx >= 1024 and ctx % 1024 == 0 else str(ctx), str(v["engine.kv"]),
             f"spec {v['engine.spec']}" if int(v["engine.spec"] or 0) >= 2 else "no MTP"]
    if int(v["server.parallel"]) > 1:
        parts.append(f"{v['server.parallel']} slots")
    out = {"text": " · ".join(parts), "port": values.get("server.port", DEFAULT_PORT)}
    if system:
        p = loader.plan(values, facts, system)
        s = loader.predict_speed(p)
        out.update(vram_gb=round(p["vram"]["need"] / 1e9, 1), tok_s=round(s["decode"]), fits=p["fits"])
    return out


def inference_summary(s: dict) -> str:
    parts = []
    if s.get("temperature") not in (None, ""):
        parts.append(f"temp {s['temperature']}")
    effort = {"": "high", "medium": "medium", "low": "low", "none": "off"}.get(s.get("effort") or "", s.get("effort"))
    parts.append(f"reasoning {effort}")
    if s.get("max_tokens") not in (None, ""):
        n = int(float(s["max_tokens"]))
        parts.append(f"max {n // 1024}K" if n >= 1024 and n % 1024 == 0 else f"max {n}")
    parts.append({"stop": "stop", "rolling": "rolling", "compact": "compact"}.get(s.get("policy") or "stop", "stop"))
    if s.get("format") == "json_schema":
        parts.append("JSON schema")
    elif s.get("format") == "json_object":
        parts.append("JSON")
    return " · ".join(parts)


def get_load(name: str, facts: dict | None = None) -> dict | None:
    """A load profile's values; Valori sicuri computed for this PC now and `facts` (the model's sizes)."""
    if name == SAFE_NAME:
        return safe_values(facts=facts)
    p = _read()["load"].get(name)
    return dict(p.get("values", {})) if p else None


def get_model(name: str) -> str | None:
    """The model file a load profile was saved with (None: the config file's model stays)."""
    if name == SAFE_NAME:
        return None
    p = _read()["load"].get(name) or {}
    return p.get("model") if isinstance(p.get("model"), str) else None


def _load_item(values, model) -> dict:
    item = {"values": check_load(values)}
    model = check_model(model)
    if model:
        item["model"] = model
    return item


def default_name() -> str:
    data = _read()
    return data["default"] if data["default"] == SAFE_NAME or data["default"] in data["load"] else SAFE_NAME


def change(req: dict) -> dict:
    """save | rename | delete | default | import; ValueError with the reason."""
    if not isinstance(req, dict):
        raise ValueError("send a JSON object")
    action, kind = req.get("action"), req.get("kind", "load")
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    with _lock:
        data = _read()
        box = data[kind]
        if action == "save":
            name = check_name(req.get("name"))
            if kind == "load" and name == SAFE_NAME:
                raise ValueError(f"{SAFE_NAME} is built in: save under another name")
            item = _load_item(req.get("values"), req.get("model")) if kind == "load" else \
                {"settings": check_inference(req.get("settings"))}
            box[name] = {**item, "saved": loader.now_iso()}
        elif action == "rename":
            old, new = check_name(req.get("name")), check_name(req.get("new_name"))
            if SAFE_NAME in (old, new) and kind == "load":
                raise ValueError(f"{SAFE_NAME} is built in and cannot be renamed")
            if old not in box:
                raise ValueError(f"no {kind} profile named {old!r}")
            if new in box and new != old:
                raise ValueError(f"a {kind} profile named {new!r} exists already")
            box[new] = box.pop(old)
            if kind == "load" and data["default"] == old:
                data["default"] = new
        elif action == "delete":
            name = check_name(req.get("name"))
            if kind == "load" and name == SAFE_NAME:
                raise ValueError(f"{SAFE_NAME} is built in and cannot be deleted")
            if box.pop(name, None) is None:
                raise ValueError(f"no {kind} profile named {name!r}")
            if kind == "load" and data["default"] == name:
                data["default"] = SAFE_NAME
        elif action == "default":
            name = check_name(req.get("name"))
            if kind != "load" or (name != SAFE_NAME and name not in box):
                raise ValueError(f"no load profile named {name!r}")
            data["default"] = name
        elif action == "import":
            items = req.get("data")
            if not isinstance(items, dict):
                raise ValueError("data: the exported JSON object")
            for k in KINDS:
                for name, item in (items.get(k) or {}).items():
                    name = check_name(name)
                    if k == "load" and name == SAFE_NAME:
                        continue
                    if k == "load":
                        model = item.get("model")
                        if model and not os.path.isfile(str(model)):
                            model = None                 # exported on another PC: the config file's model stays
                        data[k][name] = _load_item(item.get("values"), model) | {"saved": loader.now_iso()}
                    else:
                        data[k][name] = {"settings": check_inference(item.get("settings")), "saved": loader.now_iso()}
        else:
            raise ValueError("action: save, rename, delete, default or import")
        _write(data)
    return {"ok": True}


def export(kind: str | None = None) -> dict:
    data = _read()
    out = {"format": "basalt-profiles", "version": 1}
    for k in KINDS:
        if kind in (None, k):
            out[k] = data[k]
    return out


def apply(name: str, config: str, model: str | None = None, reserve_mib: int | None = None) -> dict:
    """Write load profile `name` into the config file (FILE.bak kept, every other key as it was); its values. The
    model is `model` when given, else the profile's, else the config file's stays (Valori sicuri); `reserve_mib`,
    when given, is the VRAM left free instead of the profile's (the start panel's control)."""
    facts = None
    if name == SAFE_NAME:                                # computed for the model it will load
        try:
            facts = _model_facts(model or settings.model_of(config), None)
        except (OSError, ValueError):
            facts = None
    values = get_load(name, facts)
    if values is None:
        raise KeyError(name)
    if reserve_mib is not None:
        values = {**values, "engine.vram-reserve-mib": settings.check_value("engine", "vram-reserve-mib", reserve_mib)}
    current = settings.values_of(settings.read_file(config), settings.LOADER_FIELDS)
    changes = {f: values.get(f) for f in LOAD_FIELDS if values.get(f) != current.get(f)}
    if changes:
        settings.save(config, changes, settings.LOADER_FIELDS)
    model = model or get_model(name)
    if model:
        from serve import models
        model = settings.check_model_value("model", model)
        try:
            title = models.basalt_info(model, os.path.getsize(model)).get("name")
        except OSError:
            title = None
        settings.set_model(config, model, title or Path(model).stem)
    return values


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m serve.profiles", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the load profiles, one per line (the default marked *)")
    sub.add_parser("default", help="the default load profile's name")
    sub.add_parser("names", help="the load profiles' names as a JSON list")
    ap_json = sub.add_parser("json", help="the profiles with their summaries for this PC now, as JSON")
    ap_json.add_argument("--config", help="the config file whose model the summaries are for")
    ap_apply = sub.add_parser("apply", help="write a load profile into the config file; prints its values as JSON")
    ap_apply.add_argument("name")
    ap_apply.add_argument("--config", required=True)
    ap_apply.add_argument("--model", help="the .basalt file to load instead of the profile's")
    ap_apply.add_argument("--vram-reserve-mib", type=int, help="the VRAM to leave free instead of the profile's")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        d = default_name()
        for p in listing()["load"]:
            print(("* " if p["name"] == d else "  ") + p["name"])
        return 0
    if a.cmd == "default":
        print(default_name())
        return 0
    if a.cmd == "names":
        print(json.dumps([p["name"] for p in listing()["load"]], ensure_ascii=False))
        return 0
    if a.cmd == "json":
        facts = None
        if a.config:
            try:
                model = settings.values_of(settings.read_file(a.config)).get("server.model")
                if model and not os.path.isabs(model):
                    model = str(Path(a.config).resolve().parent / model)
                facts = loader.model_facts(model) if model else None
            except (OSError, ValueError, KeyError):
                facts = None
        print(json.dumps(listing(loader.system_now(), facts), ensure_ascii=False))
        return 0
    try:
        values = apply(a.name, a.config, a.model, a.vram_reserve_mib)
    except KeyError:
        print(f"profilo sconosciuto: {a.name!r}. Profili: " + ", ".join(p["name"] for p in listing()["load"]),
              file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"il profilo {a.name!r} non si può applicare: {e}", file=sys.stderr)
        return 2
    try:
        model = settings.model_of(a.config)
    except (OSError, ValueError):
        model = None
    print(json.dumps({"name": a.name, "values": values, "model": model}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
