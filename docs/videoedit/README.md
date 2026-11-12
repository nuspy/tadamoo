# Video editing: `tadamoo/videoedit`

Reusable by any skill that edits video in code; the newscast uses it, so can a documentary, a lecture or a trailer maker.

| Module | What |
|---|---|
| `camera` | the virtual camera: a state (zoom, face position, roll), the S-curve, the library of moves (push in, pull out, drift, roll), the limits of speed, `sequence` (moves for a run of shots, joined on motion or continuously) |
| `render` | overlays composed over a picture frame by frame (a card sliding in and out, a bar, an evidence card, a small window over a clip), frames written to ffmpeg |
| `compose` | one file for each shot, shots joined by stream copy, clips joined untouched and checked packet by packet, probing |
| `frame` | the canvas of an overlay and the box of a picture on it, sizes given for a frame 530 pixels high and scaled |
| `qa` | the measures of a finished picture: seams (jumps of the face, of its pixels, of the histogram and the colour, each marked designed, at a graphic event or accidental), the mouth opening, the head and shoulder motion |

The graphics themselves (the cards, bars and labels of a newscast) are drawn by `tadamoo/newscast/layouts.py` on a `Frame`. Tests: `tests/test_videoedit.py`.
The skill that describes it for a person and for the agent is `skills/video-editing`.
