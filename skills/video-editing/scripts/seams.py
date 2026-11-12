"""The seams of a video as a table: every jump that is not a smooth move, each marked designed, at a graphic event, or accidental.

    python seams.py VIDEO [--cuts 6.5,14.0] [--graphics 8.0,12.5]

Exits 1 when a seam falls at a graphic event or is accidental."""
import argparse
import sys

from tadamoo.videoedit import qa


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--cuts", default="")
    ap.add_argument("--graphics", default="")
    args = ap.parse_args()
    nums = lambda s: [float(x) for x in s.split(",") if x.strip()]
    found = qa.report(args.video, nums(args.cuts), nums(args.graphics))["seams"]
    print(f"{'time':>8} {'kind':<10} {'face':>6} {'face px':>8} {'hist':>6} {'colour':>7}  note")
    for s in found:
        print(f"{s['time_s']:>8.2f} {s['kind']:<10} {s['face_jump']:>6.3f} {s['face_diff']:>8.1f} {s['hist']:>6.3f} {s['color']:>7.1f}  {s['note']}")
    bad = [s for s in found if s["kind"] != "designed"]
    print(f"{len(found)} seams, {len(bad)} not designed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
