"""One talking-head take from a still and a recording, through TaDaMoo's media client (a heavy job: read the skill's heavy-swap notes first).

    python make_take.py STILL.png VOICE.wav OUT.mp4 [--prompt TEXT] [--negative TEXT] [--seed N] [--url http://127.0.0.1:7232] [--tame]

With --tame the model hears a tamed copy of the voice (quieter, compressed, band-limited) and nothing else changes: mount the real recording over the result yourself."""
import argparse
import sys
from pathlib import Path

from tadamoo.media import client, limits
from serve import userdata
from tadamoo.media.settings import MediaSettings

PROMPT = ("A refined, elegant presenter sits calmly at a desk, framed from the chest up, and speaks to the camera in a soft, composed, natural way. Her speech is relaxed, with a small natural "
          "mouth opening; her shoulders are down; her head stays almost still; slow natural blinking.")
NEGATIVE = ("wide open mouth, exaggerated mouth movement, big gestures, hand gestures, head nodding, head shaking, swaying, shrugging, fidgeting, jittery, over-animated, blurred details, deformed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("still")
    ap.add_argument("voice")
    ap.add_argument("out")
    ap.add_argument("--prompt", default=PROMPT)
    ap.add_argument("--negative", default=NEGATIVE)
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--url", default="http://127.0.0.1:7232")
    ap.add_argument("--tame", action="store_true")
    args = ap.parse_args()
    audio = Path(args.voice)
    if args.tame:
        from tadamoo.speech import audiofx
        audio = Path(args.out).with_suffix(".tamed.wav")                                   # next to the result, never in a temporary folder
        audio.write_bytes(audiofx.tame(Path(args.voice).read_bytes()))
    settings = MediaSettings(userdata.path("media.json")).read()
    request = limits.avatar_request(args.still, str(audio), args.prompt, "480p", seed=args.seed, negative=args.negative)
    got = client.run("avatar", request, Path(args.out), settings, url=args.url, held=False, progress=lambda e: None)
    print(got)
    return 0


if __name__ == "__main__":
    sys.exit(main())
