"""One recording of a text through the clause-voice pipeline (see ../SKILL.md).

    python speak.py TEXTFILE OUT.wav [--speaker "Gitta Nikolina"] [--lang hu] [--speed 1.14] [--seeds 4] [--url http://127.0.0.1:7232] [--work FOLDER]

The voice's profile is read from tadamoo/speech/data/profiles (built from the voice service the first time). Exits 1 and says why when a piece has no right take."""
import argparse
import json
import sys
from pathlib import Path

from tadamoo.speech import asr, clausevoice, service, voiceprint


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
    ap.add_argument("--rating-device", default="cpu", choices=("cpu", "cuda"), help="where the CTC read-back and the speaker embedder run")
    ap.add_argument("--url", default=service.DEFAULT_URL)
    ap.add_argument("--work", default="")
    args = ap.parse_args()
    p = clausevoice.Params(speaker=args.speaker, lang=args.lang, speed=args.speed, seeds=args.seeds, xtts_batch=args.batch, xtts_precision=args.precision, xtts_compile=args.compile, xtts_cache=args.cache)
    emb = voiceprint.Embedder(device=args.rating_device)
    path = voiceprint.profile_path("xtts", args.speaker, args.lang)
    if path.is_file():
        profile = voiceprint.Profile.load(path)
    else:
        profile = voiceprint.build(lambda t: service.synthesize(args.url, t, "xtts", args.speaker, args.lang), emb, args.speaker, args.lang)
        profile.save(path)
    folder = asr.MODELS / "mms-1b-hungarian"
    ctc = asr.Ctc(folder, "mms-1b-hungarian (CTC)", args.rating_device) if args.lang == "hu" and (folder / "config.json").is_file() else None
    rater = clausevoice.Rater(args.url, ctc, voiceprint.Judge(profile, emb, voiceprint.Targets(p.clause_pace_min, p.clause_pace_max)), p)
    runner = clausevoice.LocalXtts(p, Path(args.work) if args.work else None)
    try:
        res = clausevoice.speak(Path(args.text).read_text(encoding="utf-8"), runner, rater, p)
    except clausevoice.VoiceFailed as exc:
        print("failed:", exc)
        return 1
    finally:
        runner.close()
    Path(args.out).write_bytes(res.wav)
    print(json.dumps({"seconds": round(len(res.wav) / 48000, 2), "sentences": res.sentences, "joins": res.joins, "params": res.params}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
