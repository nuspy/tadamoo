"""The objective numbers of any recording of a voice: pauses, clicks, digital silence, loudness, pace.

    python audit.py WAV [--text TEXTFILE] [--lang hu] [--url http://127.0.0.1:7232]

With --text each silence is put after a written word and judged against the punctuation (a pause over 150 ms with none is a defect)."""
import argparse
import json
import sys
from pathlib import Path

from tadamoo.speech import audiofx, audiojoin as J, service, textnorm, voicecheck, voiceprint


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--text", default="")
    ap.add_argument("--lang", default="hu")
    ap.add_argument("--url", default=service.DEFAULT_URL)
    args = ap.parse_args()
    wav = Path(args.wav).read_bytes()
    x, rate = J.read(wav)
    out = {"seconds": round(len(x) / rate, 2), "lufs": round(audiofx.measure_lufs(wav), 1), "splices": J.splices(x, rate), "dead_gaps": J.dead_gaps(x, rate),
           "clicks": [c.__dict__ for c in J.click_report(x, rate) if c.bad]}
    if args.text:
        text = textnorm.normalise(Path(args.text).read_text(encoding="utf-8"), args.lang)
        words = voicecheck.transcribe(args.url, wav, args.lang)
        out["silences"] = [s.__dict__ for s in J.pause_audit(x, rate, text, words, 0.15, 0.12, args.lang)]
        out["pace_syllables_per_s"] = round(voiceprint.pace(wav, text, args.lang), 2)
    else:
        out["silences"] = [{"start": a, "end": b} for a, b in J.silences(x, rate)]
    print(json.dumps(out, ensure_ascii=False, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
