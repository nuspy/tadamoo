"""One recording of a text through the clause-voice pipeline (see ../SKILL.md).

    python speak.py TEXTFILE OUT.wav [--speaker "Gitta Nikolina"] [--lang hu] [--speed 1.14] [--seeds 4] [--url http://127.0.0.1:7232] [--work FOLDER]

The voice's profile is read from tadamoo/speech/data/profiles; when there is none it is built from the local XTTS (or from the TaDaMoo server with `--stt service`). The read-back of the takes uses
the TaDaMoo server's Whisper when the server answers at `--url`, else a Whisper in this process (`--stt auto`, the default; `local` and `service` say which). Without the server nothing else is needed.
Exits 1 and says why when a piece has no right take."""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

from tadamoo.speech import asr, clausevoice, localstt, service, voicecheck, voiceprint


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("out")
    ap.add_argument("--speaker", default="Gitta Nikolina")
    ap.add_argument("--lang", default="hu")
    ap.add_argument("--speed", type=float, default=1.14)
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--precision", default="fp32", choices=("fp32", "tf32", "fp16", "bf16"))
    ap.add_argument("--compile", default="off", choices=("off", "on"), help="torch.compile of the autoregressive pass (needs Triton: python -m tadamoo.media.components install speech-synthesis)")
    ap.add_argument("--cache", default="", help="a folder where takes are kept by text, seeds and settings and found again by any run")
    ap.add_argument("--rating-device", default="auto", choices=("auto", "cpu", "cuda"), help="where the read-back and the speaker embedder run (auto: the GPU when it has room)")
    ap.add_argument("--stt", default="auto", choices=("auto", "local", "service"), help="the Whisper of the read-back: the TaDaMoo server's, or one in this process (auto: the server's when it answers)")
    ap.add_argument("--url", default=service.DEFAULT_URL)
    ap.add_argument("--work", default="")
    args = ap.parse_args()
    p = clausevoice.Params(speaker=args.speaker, lang=args.lang, speed=args.speed, seeds=args.seeds, xtts_batch=args.batch, xtts_precision=args.precision, xtts_compile=args.compile, xtts_cache=args.cache)
    began = time.perf_counter()
    local = args.stt == "local" or (args.stt == "auto" and not service.server_up(args.url))
    device = localstt.pick_device(args.rating_device)
    if local:
        voicecheck.LOCAL_STT = localstt.LocalWhisper(device=device)
        threading.Thread(target=voicecheck.LOCAL_STT.warm, daemon=True).start()               # the Whisper loads while XTTS does
    emb = voiceprint.Embedder(device=device)
    runner = clausevoice.LocalXtts(p, Path(args.work) if args.work else None)
    path = voiceprint.profile_path("xtts", args.speaker, args.lang)
    if path.is_file():
        profile = voiceprint.Profile.load(path)
    else:
        profile = voiceprint.build(clausevoice.local_synth(runner, p) if local else (lambda t: service.synthesize(args.url, t, "xtts", args.speaker, args.lang)), emb, args.speaker, args.lang)
        profile.save(path)
    folder = asr.MODELS / "mms-1b-hungarian"
    ctc = asr.Ctc(folder, "mms-1b-hungarian (CTC)", device) if args.lang == "hu" and (folder / "config.json").is_file() else None
    rater = clausevoice.Rater(args.url, ctc, voiceprint.Judge(profile, emb, voiceprint.Targets(p.clause_pace_min, p.clause_pace_max)), p)
    try:
        res = clausevoice.speak(Path(args.text).read_text(encoding="utf-8"), runner, rater, p)
    except clausevoice.VoiceFailed as exc:
        print("failed:", exc)
        return 1
    finally:
        runner.close()
    Path(args.out).write_bytes(res.wav)
    took, audio = time.perf_counter() - began, len(res.wav) / 48000
    print(json.dumps({"seconds": round(audio, 2), "took_s": round(took, 1), "x_real_time": round(took / audio, 2), "read_back": "local" if local else "server", "rating_device": device, "sentences": res.sentences, "joins": res.joins, "params": res.params}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
