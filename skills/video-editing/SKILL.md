---
name: video-editing
description: Edit video in code with a virtual camera and its move library, overlays composed frame by frame (cards sliding in and out, lower-thirds, a small window over a clip), shots joined by stream copy, clips joined untouched and verified packet by packet, and a seam and movement detector that proves the result has no accidental cuts. Use when a task assembles a video from takes, clips and graphics.
tags: video, editing, ffmpeg, camera, overlay, seams, sigla
requires: python, ffmpeg
---

# Video editing

The code is `tadamoo/videoedit/` (docs in `docs/videoedit/`); the graphics of a particular programme (the cards of a newscast) are drawn by that programme on a `videoedit.frame.Frame`.

## When to use it

Several pieces (a continuous take of a presenter, B-roll clips, opening and closing clips) become one video with graphics over them and a camera that moves. Not for generating the pieces
(`video-generation`, `talking-head`, `image-generation`) nor the voice (`speech-synthesis`).

## The grammar

- **One continuous take, graphics only as overlays.** The picture underneath must not change, jump or re-frame at a graphic event (a card sliding in, a bar, a window). A cut between takes may fall
  only where the plan says, never at a graphic event: the graphics start `graphic_delay_s` after a take starts.
- **A virtual camera** on the take: a state (zoom, face position, roll) moved along an S-curve (smootherstep, zero speed and acceleration at both ends) by a library of moves (push in, pull out, drift,
  roll). Limits, which `camera.check` enforces: zoom at most 1.10 times a second, roll at most 4 degrees, drift at most 3% of the width a second, a move at least 3 s. With `continuous=True` a
  move starts exactly where the one before ended; a cut on motion starts a small step back at the same face position.
- **Untouched clips** (an opening and a closing sigla) are joined by stream copy and the result is verified packet by packet (`compose.verify_untouched`): the video packets are identical.
- **A uniform format** for every shot (size, frame rate, audio rate) so that joining is a copy and nothing is re-encoded twice.

## Proving the result (`videoedit/qa.py`)

`python -m tadamoo.videoedit.qa VIDEO [--cuts t1,t2] [--graphics t1,t2] [--body]` prints, as JSON: every seam above a robust threshold (a jump of the face, of its pixels, of the brightness histogram or of
the mean colour) with its time, each marked `designed` (the plan says a cut is there), `graphic` (at a graphic event: a fault) or `accidental` (a fault); the mouth opening (inner lips over face height,
percentiles); the head speed and roll; with `--body` the shoulder speed and the motion of the lower half. Run it on a reference video and on the result and compare the percentiles. Needs the MediaPipe
landmarker models (`E:\Modelli\basalt\qa\face_landmarker.task`, `pose_landmarker.task`).

## Heavy-swap behaviour

None: editing is CPU work (ffmpeg and PIL) and runs while the local model is loaded. The measures use MediaPipe on the CPU.

## Scripts

`scripts/seams.py VIDEO [--cuts ...] [--graphics ...]` runs the detector and prints a table of the seams.

## Installation

Check first, install only what is missing, only when the person asks:

1. `python -m tadamoo.media.components check video-editing` prints what is present and the command for what is missing; it changes nothing.
2. `python -m tadamoo.media.components install video-editing` runs those commands and checks again.

Components: **ffmpeg** on the PATH (`winget install Gyan.FFmpeg`); for the seam and movement detector only, the **mediapipe** package and the two MediaPipe landmarker models (face and pose, in `E:\Modelli\basalt\qa`).
