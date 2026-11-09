"""A stand-in for voice/avatar/avatar_worker.py that speaks its JSON-lines protocol and renders nothing: no torch, no GPU, no model.

It writes what it was started with and every command it receives, as JSON lines, to the file named by FAKE_AVATAR_LOG. A preparation takes
FAKE_AVATAR_PREP_STEPS steps of FAKE_AVATAR_STEP_S seconds each and can be cancelled; with `frames` on it sends small I420 frames to the port."""
import json
import os
import socket
import struct
import sys
import threading
import time

LOG = os.environ.get("FAKE_AVATAR_LOG", "")
STEPS = int(os.environ.get("FAKE_AVATAR_PREP_STEPS", "4"))
STEP_S = float(os.environ.get("FAKE_AVATAR_STEP_S", "0.05"))
W, H = 16, 16


def emit(event, **kw):
    print(json.dumps({"event": event, **kw}), flush=True)


def note(**kw):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw) + "\n")


def frames_loop(port, stop):
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        n = 0
        while not stop.is_set():
            shade = bytes([(n * 20) % 256]) * (W * H) + bytes([128]) * (W * H // 2)
            sock.sendall(struct.pack("<4sII", b"AVI4", W, H) + shade)
            n += 1
            time.sleep(0.02)
    except OSError:
        pass
    finally:
        sock.close()


def main():
    note(started=True, models=os.environ.get("MUSETALK_MODELS"), code=os.environ.get("AVATAR_CODE_DIR"), pythonpath=os.environ.get("PYTHONPATH"),
         hf_offline=os.environ.get("HF_HUB_OFFLINE"), tf_offline=os.environ.get("TRANSFORMERS_OFFLINE"), cwd=os.getcwd(),
         cuda=os.environ.get("CUDA_VISIBLE_DEVICES"), state=os.environ.get("AVATAR_STATE_DIR"),
         fps=os.environ.get("AVATAR_FPS"), height=os.environ.get("AVATAR_OUTPUT_HEIGHT"))
    emit("ready")
    cancelled = set()
    frames_stop = {"ev": None}

    def reader():
        for line in sys.stdin:
            cmd = json.loads(line)
            note(cmd=cmd)
            if cmd.get("cmd") == "quit":
                os._exit(0)                       # the real worker finishes what it is doing first; the client kills it after a while
            if cmd.get("cmd") == "cancel":
                cancelled.add(cmd.get("id"))
            else:
                queue.append(cmd)

    queue = []
    threading.Thread(target=reader, daemon=True).start()
    while True:
        if not queue:
            time.sleep(0.01)
            continue
        cmd = queue.pop(0)
        what = cmd.get("cmd")
        if what == "quit":
            return
        if what == "persona":
            for i in range(STEPS):
                emit("progress", job="", phase="extract", done=i + 1, total=STEPS, percent=100.0 * (i + 1) / STEPS, eta_s=0.1)
                time.sleep(STEP_S)
            emit("persona_ready", clip="clip.mp4", frames=STEPS)
        elif what == "prepare":
            for i in range(STEPS):
                if cmd["id"] in cancelled:
                    break
                emit("progress", job=cmd["id"], phase="latents", done=i + 1, total=STEPS, percent=100.0 * (i + 1) / STEPS, eta_s=0.1)
                time.sleep(STEP_S)
            if cmd["id"] in cancelled:
                cancelled.discard(cmd["id"])
                emit("cancelled", id=cmd["id"])
            elif cmd.get("clip") == "bad.mp4":
                emit("prepare_failed", id=cmd["id"], message="no face found")
            else:
                emit("prepared", id=cmd["id"], clip=cmd["clip"], frames=2 * STEPS)
        elif what == "frames":
            if frames_stop["ev"] is not None:
                frames_stop["ev"].set()
                frames_stop["ev"] = None
            if cmd.get("on"):
                frames_stop["ev"] = threading.Event()
                threading.Thread(target=frames_loop, args=(cmd["port"], frames_stop["ev"]), daemon=True).start()
            emit("frames", on=bool(cmd.get("on")))
        elif what == "speak":
            emit("speaking", wav=os.path.basename(cmd["wav"]))
            emit("playing", wav=os.path.basename(cmd["wav"]), preparazione_s=0.12)
            time.sleep(0.05)
            emit("idle")
        elif what == "interrupt":
            emit("interrupted", frames=0)
            emit("idle")
        elif what == "stream":
            emit("stream", on=bool(cmd.get("on")))


if __name__ == "__main__":
    main()
