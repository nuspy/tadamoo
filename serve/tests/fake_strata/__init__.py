"""A fake Strata install for tests: `install(root)` writes bin/strata/ with stub binaries and the stub Python server."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

EXE = ".exe" if os.name == "nt" else ""
HERE = Path(__file__).resolve().parent
FLAGS = b"--max-context --kv --spec --mtp --expert-cache --vram-reserve-mib --reserve-mib --no-expert-cache --serve"


def make_source(dest: Path) -> Path:
    """A source folder as the person would point to: binaries beside the Python server tree."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("strata", "strata-q36", "strata-dsv41"):
        (dest / (name + EXE)).write_bytes(FLAGS)
    tree = dest / "serve"
    tree.mkdir(exist_ok=True)
    (tree / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(HERE / "server.py", tree / "server.py")
    (dest / "strata_control").mkdir(exist_ok=True)
    (dest / "strata_control" / "__init__.py").write_text("", encoding="utf-8")
    return dest


def install(root: Path) -> Path:
    bin_dir = Path(root) / "bin" / "strata"
    return make_source(bin_dir)


def run_config(folder: Path, name: str = "strata-test.json", arch: str = "qwen4exp", **extra) -> Path:
    """A Strata run config and the tokenizer folder it names; returns the config's path."""
    tok = folder / "tokenizer"
    tok.mkdir(parents=True, exist_ok=True)
    (tok / "vocab.json").write_text("{}", encoding="utf-8")
    (tok / "merges.txt").write_text("", encoding="utf-8")
    (tok / "token_type.json").write_text("[]", encoding="utf-8")
    cfg = {"exe": "x", "args": ["--pack", "p", "--max-context", "4096", "--expert-cache", "auto"], "cwd": ".",
           "tokenizer": str(tok), "model_name": {"qwen4exp": "qwen3.8-flash-next", "qwen35moe": "qwen3.6-35b-a3b",
                                                  "deepseek41": "deepseek-v4.1-flash"}[arch], "arch": arch,
           "api_key": "secret", "port": 7232, **extra}
    path = folder / name
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path
