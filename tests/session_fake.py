"""A stand-in for the media worker in session mode (`python session_fake.py --session`): the protocol of worker.py --session without a model. It makes a small real clip for a video job and copies the video for an upscale
job, and writes a line to the file named by SESSION_FAKE_LOG for what it does (`start KIND PID`, `job KIND ID OUTPUT`). SESSION_FAKE_DIE_ONCE names a marker file: the first video job ends the process (a crash) and makes the marker."""
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from video_fakes import make_clip  # noqa: E402


def log(text):
    path = os.environ.get("SESSION_FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    session = json.loads(sys.stdin.readline())
    kind = session["kind"]
    log(f"start {kind} {os.getpid()}")
    emit({"event": "start", "kind": kind})
    emit({"event": "lease", "ok": True, "granted_mib": session["mib"], "reason": "", "message": "", "message_it": "", "swapped": False})
    for line in sys.stdin:
        if not line.strip():
            continue
        job = json.loads(line)
        spec, jid = job["spec"], job["id"]
        out = Path(spec["output"])
        log(f"job {kind} {jid} {out.parent.name}/{out.name}")
        marker = os.environ.get("SESSION_FAKE_DIE_ONCE")
        if kind == "video" and marker and not Path(marker).exists():
            Path(marker).write_text("x")
            os._exit(9)
        out.parent.mkdir(parents=True, exist_ok=True)
        req = spec["request"]
        emit({"event": "progress", "step": 1, "steps": 3, "id": jid})
        if kind == "video":
            make_clip(out, (req["frames"] - 1) / req["fps"], f"{req['width']}x{req['height']}")
        else:
            shutil.copyfile(req["video"], out)
            if req.get("keep_working"):
                shutil.copyfile(req["video"], out.with_name(out.name + ".big.mp4"))
        emit({"event": "done", "id": jid, "path": str(out), "bytes": out.stat().st_size, "seconds": 1.0, "warnings": [], "lease": {"id": "x", "owner": kind, "granted_mib": session["mib"], "swapped": False, "risky": False}})


if __name__ == "__main__":
    main()
