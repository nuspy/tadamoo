# Licence of the SadTalker code

Status: licence to confirm. This code is vendored for the experimental phase; the licence is not a blocker now, and nothing about it is shown in the GUI.

What is known, from files read (not legal advice):

- `LICENSE` is Linly-Talker's, MIT. It covers Linly-Talker's own files (`TFG/SadTalker.py` is the wrapper). No file of this folder carries a licence header of its own.
- `src/` is SadTalker (github.com/OpenTalker/SadTalker, Tencent). Its LICENSE (read at commit cd4c0465 on 2026-11-09) is Apache 2.0 "except for the third-party components listed
  below" and its README says the licence was changed to Apache 2.0 and the non-commercial restriction removed. Whether the copy in Linly-Talker is unmodified is not checked.
- `src/face3d` includes arcface_torch (insightface), a 3DMM face model code and `src/facerender` code that follow the SadTalker tree; their own terms are not stated in this tree.
- The weights (`sadtalker-weights`, `sadtalker-gfpgan`) are separate downloads with their own terms, not checked here.

To settle before the code is given to anyone else or used commercially: the third-party components of SadTalker's licence and the terms of the weights.
