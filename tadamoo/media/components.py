"""What each media skill needs on this PC and how to get it: every component is checked first and only the missing ones have steps to run.

The models and their programs belong to LTX Studio, so its own downloader and installers do the work (resume, integrity checks, the environments it keeps).
`check` reads the disk and changes nothing; `steps` lists the commands for what is missing; `install` runs them, one after the other, and checks again.

    python -m tadamoo.media.components check SKILL [--ltx-dir DIR] [--media-file FILE]
    python -m tadamoo.media.components install SKILL [--ltx-dir DIR] [--media-file FILE]
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.media import client
from tadamoo.media.settings import DEFAULTS, MediaSettings, ltx_dir_of
from tadamoo.videoedit.qa import MODELS

LANDMARKERS = MODELS
MEDIAPIPE_URL = "https://storage.googleapis.com/mediapipe-models/"

# id -> label (en, it), the kind of check, and what to run when it is missing. A step is a command (a list) or {"download": url, "to": path}; "{py}" is the Python
# that runs LTX Studio's scripts and "{ltx}" its folder. `ready` components are asked of the worker, which reads LTX Studio's own registry of weights and environments.
COMPONENTS: Dict[str, Dict[str, Any]] = {
    "ltx-studio": {"label": ("LTX Studio, the program that runs the models", "LTX Studio, il programma che esegue i modelli"), "check": "folder",
                   "how": ("LTX Studio is a separate program: install it, then give its folder in the media settings (ltx_dir) or in TADAMOO_LTX_STUDIO.",
                           "LTX Studio e' un programma a parte: installalo, poi indica la sua cartella nelle impostazioni media (ltx_dir) o in TADAMOO_LTX_STUDIO.")},
    "ffmpeg": {"label": ("ffmpeg, the video tool", "ffmpeg, lo strumento per i video"), "check": "program", "program": "ffmpeg",
               "steps": [["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements", "--accept-package-agreements"]]},
    "image-model": {"label": ("Qwen-Image 2.1 (pictures and edits, NVFP4)", "Qwen-Image 2.1 (immagini e modifiche, NVFP4)"), "check": "ready", "kind": "image",
                    "steps": [["{py}", "{ltx}/scripts/download_weights.py", "--ids", "qwen21-dit-nvfp4"]]},
    "video-model": {"label": ("LTX 2.5 distilled (clips from words or a picture, NVFP4)", "LTX 2.5 distilled (clip da parole o da un'immagine, NVFP4)"), "check": "ready", "kind": "video",
                    "steps": [["{py}", "{ltx}/scripts/download_weights.py", "--ids", "ltx25-transformer-nvfp4"]]},
    "talking-head-model": {"label": ("LongCat-Video-Avatar 1.5 (a person who speaks a recording)", "LongCat-Video-Avatar 1.5 (una persona che pronuncia una registrazione)"),
                           "check": "ready", "kind": "avatar", "steps": [["{py}", "{ltx}/scripts/install_longcat_avatar.py"]],
                           "how": ("After the environment, the weights come from LTX Studio's Weights tab (LongCat Avatar, int8).", "Dopo l'ambiente, i pesi si scaricano dal tab Pesi di LTX Studio (LongCat Avatar, int8).")},
    "frame-maker": {"label": ("RIFE (frames made between the real ones, MIT)", "RIFE (fotogrammi intermedi, MIT)"), "check": "ready", "kind": "interpolate",
                    "steps": [["{py}", "{ltx}/scripts/download_weights.py", "--ids", "rife-flownet-v4"]]},
    "video-enlarger": {"label": ("FlashVSR+ (larger video with more detail, Apache-2.0)", "FlashVSR+ (video piu' grande e dettagliato, Apache-2.0)"), "check": "ready", "kind": "upscale",
                       "steps": [["{py}", "{ltx}/scripts/install_flashvsrplus.py"], ["{py}", "{ltx}/scripts/download_weights.py", "--ids", "flashvsr-plus-turbo"]]},
    "mediapipe": {"label": ("the mediapipe package (measures of faces and bodies for the seam detector)", "il pacchetto mediapipe (misure di volti e corpi per il rilevatore di stacchi)"),
                  "check": "module", "module": "mediapipe", "steps": [[sys.executable, "-m", "pip", "install", "mediapipe"]]},
    "xtts-triton": {"label": ("Triton for the XTTS environment (torch.compile of the voice; the triton-windows package, the one LTX Studio's inference environment uses)",
                              "Triton per l'ambiente di XTTS (torch.compile della voce; il pacchetto triton-windows, quello dell'ambiente di inferenza di LTX Studio)"),
                    "check": "venv_module", "module": "triton", "steps": [["{xtts_py}", "-m", "pip", "install", "-U", "triton-windows"]]},
    "landmarkers": {"label": ("the MediaPipe face and pose models", "i modelli MediaPipe di volto e posa"), "check": "files",
                    "files": {"face_landmarker.task": MEDIAPIPE_URL + "face_landmarker/face_landmarker/float16/1/face_landmarker.task",
                              "pose_landmarker.task": MEDIAPIPE_URL + "pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task"}},
}

SKILLS: Dict[str, List[str]] = {
    "image-generation": ["ltx-studio", "image-model"],
    "image-editing": ["ltx-studio", "image-model"],
    "video-generation": ["ltx-studio", "video-model", "ffmpeg", "frame-maker", "video-enlarger"],
    "talking-head": ["ltx-studio", "talking-head-model"],
    "video-editing": ["ffmpeg", "mediapipe", "landmarkers"],
    "speech-synthesis": ["xtts-triton"],
}


def _row(cid: str, present: bool, where: str = "") -> Dict[str, Any]:
    spec = COMPONENTS[cid]
    return {"id": cid, "label": spec["label"][0], "label_it": spec["label"][1], "present": present, "where": where}


def check(skill: str, settings: Optional[Dict[str, Any]] = None, command: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """One row for each component of the skill: `{id, label, label_it, present, where}`, and for a missing one `missing` (what is lacking) when LTX Studio says."""
    if skill not in SKILLS:
        raise ValueError(f"no skill named {skill}: " + ", ".join(SKILLS))
    settings = settings or dict(DEFAULTS)
    folder = ltx_dir_of(settings.get("ltx_dir", ""))
    asked: Dict[str, Any] = {}
    rows = []
    for cid in SKILLS[skill]:
        spec = COMPONENTS[cid]
        how = spec["check"]
        if how == "folder":
            row = _row(cid, bool(folder), folder)
        elif how == "program":
            found = shutil.which(spec["program"]) or ""
            row = _row(cid, bool(found), found)
        elif how == "module":
            row = _row(cid, importlib.util.find_spec(spec["module"]) is not None)
        elif how == "venv_module":
            py = _xtts_python(settings)
            found = bool(py) and subprocess.run([py, "-I", "-c", f"import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('{spec['module']}') else 1)"], capture_output=True).returncode == 0
            row = _row(cid, found, py)
        elif how == "files":
            gone = [n for n in spec["files"] if not (LANDMARKERS / n).is_file()]
            row = _row(cid, not gone, str(LANDMARKERS))
            if gone:
                row["missing"] = gone
        else:
            if not asked:
                asked.update(client.check(settings, command, force=True) if folder else {})
            state = asked.get(spec["kind"]) or {}
            row = _row(cid, bool(state.get("ready")), folder)
            if not row["present"]:
                row["missing"] = list(state.get("missing") or []) + ([state["error"]] if state.get("error") else [])
        rows.append(row)
    return rows


def steps(skill: str, settings: Optional[Dict[str, Any]] = None, rows: Optional[List[Dict[str, Any]]] = None, python: str = "") -> List[Dict[str, Any]]:
    """The commands for what is missing, in the order of the skill: `{component, step}` where a step is a command or {"download", "to"}; empty when everything is there.
    A missing LTX Studio has no steps (it is installed by hand: its `how` says so) and the components that need it wait."""
    settings = settings or dict(DEFAULTS)
    rows = rows if rows is not None else check(skill, settings)
    folder = ltx_dir_of(settings.get("ltx_dir", ""))
    py = python or _python(settings, folder)
    out = []
    for row in rows:
        if row["present"]:
            continue
        spec = COMPONENTS[row["id"]]
        for step in spec.get("steps", []):
            if "{ltx}" in " ".join(step) and not folder:
                continue
            if "{xtts_py}" in step and not _xtts_python(settings):
                continue
            out.append({"component": row["id"], "step": [str(a).replace("{py}", py).replace("{ltx}", folder).replace("{xtts_py}", _xtts_python(settings)) for a in step]})
        for name, url in (spec.get("files") or {}).items():
            if name in row.get("missing", []):
                out.append({"component": row["id"], "step": {"download": url, "to": str(LANDMARKERS / name)}})
    return out


def _xtts_python(settings: Dict[str, Any]) -> str:
    """The Python of the XTTS environment: the one the settings name (`xtts_python`), else the one the voice uses; empty when there is none on this PC."""
    from tadamoo.speech import clausevoice
    for candidate in (settings.get("xtts_python"), clausevoice.XTTS_PYTHON):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return ""


def _python(settings: Dict[str, Any], folder: str) -> str:
    """The Python that runs LTX Studio's installers and downloader: the one the settings name, LTX Studio's own environment, its inference environment, else this one."""
    for candidate in (settings.get("image_python"), Path(folder) / ".venv" / "Scripts" / "python.exe" if folder else "", r"E:\AI\Tools\inference_server_venv\Scripts\python.exe"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return sys.executable


def _download(url: str, to: str) -> int:
    target = Path(to)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as got, open(partial, "wb") as out:
            shutil.copyfileobj(got, out)
        partial.replace(target)
        return 0
    except OSError as exc:
        print(f"download of {url} failed: {exc}", file=sys.stderr)
        partial.unlink(missing_ok=True)
        return 1


def install(skill: str, settings: Optional[Dict[str, Any]] = None, run: Callable[[Sequence[str]], int] = lambda argv: subprocess.run(list(argv)).returncode,
            download: Callable[[str, str], int] = _download, say: Callable[[str], None] = print, command: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Runs the steps of what is missing and checks again; stops at the first step that fails. Nothing present is touched."""
    settings = settings or dict(DEFAULTS)
    rows = check(skill, settings, command)
    todo = steps(skill, settings, rows)
    failed = ""
    for item in todo:
        step = item["step"]
        say(f"[{item['component']}] " + (f"download {step['download']}" if isinstance(step, dict) else " ".join(step)))
        code = download(step["download"], step["to"]) if isinstance(step, dict) else run(step)
        if code != 0:
            failed = item["component"]
            break
    after = check(skill, settings, command) if todo else rows
    return {"skill": skill, "ran": len(todo), "failed": failed, "ready": all(r["present"] for r in after), "components": after,
            "by_hand": [COMPONENTS[r["id"]]["how"][0] for r in after if not r["present"] and COMPONENTS[r["id"]].get("how")]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python -m tadamoo.media.components", description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("check", "install"))
    ap.add_argument("skill", choices=sorted(SKILLS))
    ap.add_argument("--ltx-dir", default="")
    ap.add_argument("--media-file", default="", help="the media settings (default: media.json in the user data folder)")
    a = ap.parse_args(argv)
    if a.media_file:
        path = Path(a.media_file)
    else:
        from serve.userdata import user_data_dir
        path = user_data_dir() / "media.json"
    settings = MediaSettings(path).read()
    if a.ltx_dir:
        settings["ltx_dir"] = a.ltx_dir
    if a.action == "check":
        rows = check(a.skill, settings)
        print(json.dumps({"skill": a.skill, "ready": all(r["present"] for r in rows), "components": rows,
                          "steps": [{"component": s["component"], "step": s["step"]} for s in steps(a.skill, settings, rows)]}, indent=1, ensure_ascii=False))
        return 0
    result = install(a.skill, settings)
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
