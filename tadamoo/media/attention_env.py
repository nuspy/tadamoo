"""The environment that runs LTX 2.5 with SageAttention 2: a copy of LTX Studio's LTX 2.5 environment (never LTX Studio's own) with the Sage 2.2.0 wheel and Triton installed in it.

The clips are made by LTX Studio's worker; what differs is the attention of the transformer, chosen by the setting `ltx_attention` (`sage2` by default) and applied by `ltx_launch.py`. PyTorch's cuDNN attention needs nothing
installed. The others need a package, which lives in the copy: `resolve` says which Python runs a clip and with which attention, and when what the attention needs is not there it says so in a plain warning and the clip is
made with cuDNN attention (a clip is always made). `install` makes the copy and installs the wheels; it is the step of the component `video-attention` (components.py).

    python tadamoo/media/attention_env.py install --ltx-dir DIR --env FOLDER --wheels FOLDER
    python tadamoo/media/attention_env.py check --env FOLDER [--attention sage2]

How the Sage 2 wheel is built, and where it is kept: docs/media/sage-wheel.md."""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

NEEDS = {"sage2": ("sageattention", "2."), "sage1": ("sageattention", "1."), "fa4": ("flash_attn_4", ""), "fa2": ("flash_attn", ""), "xformers": ("xformers", "")}    # attention -> (the package, the version it starts with)
WHEELS = ("sageattention-2.2.0-*.whl", "triton_windows-*.whl")                  # what `install` takes from the wheels folder: Sage 2.2.0 built for this Python and torch, and Triton for Windows
HOW = "python -m tadamoo.media.components install video-generation"


def python_of(env: str) -> Path:
    return Path(env) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def packages_of(env: str) -> List[Path]:
    root = Path(env)
    return [p for pattern in ("Lib/site-packages", "lib/python*/site-packages") for p in map(Path, glob.glob(str(root / pattern)))]


def has(env: str, package: str, version: str = "") -> bool:
    """The environment holds `package` (a dist-info folder is there), at a version that starts with `version`."""
    name = package.replace("-", "_")
    return any(list(site.glob(f"{name}-{version}*.dist-info")) for site in packages_of(env))


def imports(env: str, package: str, version: str = "") -> bool:
    """The environment's own Python imports `package` at that version (a dist-info is not enough: a wheel for another torch is there and does not load)."""
    py = python_of(env)
    if not py.is_file() or not has(env, package, version):
        return False
    code = f"import importlib.metadata as m, {package}; raise SystemExit(0 if m.version('{package.replace('_', '-')}').startswith('{version}') else 1)"
    try:
        return subprocess.run([str(py), "-I", "-c", code], capture_output=True, timeout=180).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def source_python(ltx_dir: str) -> str:
    """The Python LTX Studio runs LTX 2.5 with (the environment that is copied)."""
    try:
        from tadamoo.media import worker
    except ImportError:                                                # run by file, beside the worker
        import worker
    worker.use_ltx(ltx_dir)
    import importlib
    return importlib.import_module("core.ltx_release").runner("2.5")[0]


def resolve(attention: str, base_python: str, env: str) -> Tuple[str, str, Optional[str]]:
    """`(python, attention, warning)` for a clip: the Python that runs the worker, the attention it is run with, and a warning when the attention asked for was not available. `base_python` is the one LTX Studio
    names for LTX 2.5; `env` the copy with the attention package. cuDNN and the memory-efficient attention are PyTorch's own: no package, no copy."""
    if attention not in NEEDS:
        return base_python, attention, None
    package, version = NEEDS[attention]
    label = {"sage2": "SageAttention 2", "sage1": "SageAttention 1", "fa4": "FlashAttention 4", "fa2": "FlashAttention 2", "xformers": "xformers"}[attention]
    fallback = f"; the clip was made with cuDNN attention. To install it: {HOW}"
    if not python_of(env).is_file():
        return base_python, "cudnn", f"{label} is not available: the environment {env} does not exist (set ltx_attention_env, or install it){fallback}"
    if os.path.normcase(os.path.abspath(env)) == os.path.normcase(os.path.abspath(str(Path(base_python).parents[1]))):
        return base_python, "cudnn", f"{label} is not used: ltx_attention_env is LTX Studio's own environment, and it is never changed; it must be a copy{fallback}"
    if not has(env, package, version):
        return base_python, "cudnn", f"{label} is not available: {package} {version}* is not installed in {env}{fallback}"
    return str(python_of(env)), attention, None


def install(ltx_dir: str, env: str, wheels: str, run: Callable[[Sequence[str]], int] = lambda argv: subprocess.run(list(argv)).returncode, say: Callable[[str], None] = print) -> int:
    """Copies LTX Studio's LTX 2.5 environment to `env` (when it is not there) and installs the wheels of `wheels` in it. 0 when the environment can import Sage 2; 2 and a plain message when
    something it needs is missing."""
    source = Path(source_python(ltx_dir)).parents[1]
    if os.path.normcase(os.path.abspath(env)) == os.path.normcase(os.path.abspath(str(source))):
        say(f"ltx_attention_env is LTX Studio's own environment ({source}); it must be another folder: a copy.")
        return 2
    files = {pattern: sorted(glob.glob(str(Path(wheels) / pattern))) for pattern in WHEELS}
    for pattern, found in files.items():
        if not found:
            say(f"the wheel {pattern} is not in {wheels}. Put it there: docs/media/sage-wheel.md says how it was built (Python 3.12, torch 2.14 nightly cu130, Windows) and how to build it again.")
            return 2
    if not python_of(str(env)).is_file():
        say(f"copying {source} to {env} (a few GB)")
        shutil.copytree(source, env, symlinks=True, dirs_exist_ok=True)
    py = str(python_of(env))
    code = run([py, "-m", "pip", "install", "--no-deps", "--no-index", "--find-links", str(wheels), *[Path(found[-1]).name.split("-")[0].replace("_", "-") + "==" + Path(found[-1]).name.split("-")[1] for found in files.values()]])
    if code != 0:
        say("pip could not install the wheels into the copy")
        return code
    code = run([py, "-I", "-c", "import sageattention; from sageattention import sageattn"])
    if code != 0:
        say(f"the copy {env} cannot import sageattention: the wheel does not match its Python and torch (see docs/media/sage-wheel.md)")
        return 2
    say(f"SageAttention 2 is installed in {env}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tadamoo.media.attention_env", description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("install", "check"))
    ap.add_argument("--ltx-dir", default="")
    ap.add_argument("--env", required=True)
    ap.add_argument("--wheels", default="")
    ap.add_argument("--attention", default="sage2")
    a = ap.parse_args(argv)
    if a.action == "check":
        package, version = NEEDS[a.attention]
        ok = imports(a.env, package, version)
        print("ready" if ok else f"missing: {package} {version}* in {a.env}")
        return 0 if ok else 1
    return install(a.ltx_dir, a.env, a.wheels)


if __name__ == "__main__":
    sys.exit(main())
