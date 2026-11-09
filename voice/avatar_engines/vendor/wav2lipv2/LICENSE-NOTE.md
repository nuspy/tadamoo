# Licence of the Wav2Lip v2 code

Status: licence to confirm. This code is vendored for the experimental phase; the licence is not a blocker now, and nothing about it is shown in the GUI.

What is known, from files read (not legal advice):

- `LICENSE` is Linly-Talker's, MIT. It covers Linly-Talker's own files (`TFG/Wav2Lipv2.py` is the wrapper). No file of this folder carries a licence header of its own.
- The 288 px model code (`src/modelsv2`) comes from `primepake/wav2lip_288x288` (MIT, Sam Nguyen, at the commit read on 2026-11-09), which is built on Wav2Lip.
- Wav2Lip (github.com/Rudrabha/Wav2Lip) says in its README that the repository and its results are for personal, research and non-commercial use only, trained on LRS2,
  and that commercial use needs a request to the authors. It has no LICENSE file. A model derived from it is treated as non-commercial.
- `src/torchalign` (the WFLW landmark net) and `face_detection` (face-alignment) state no licence in this tree.
- `TFG/Wav2Lipv2.py` imports `ultralytics`, which is AGPL-3.0, to run the face detector `yolov8n-face.pt` (whose own licence is not stated anywhere read). The package is not
  vendored: the person installs it in the engine environment. Running it for oneself is allowed; offering it over a network or distributing it brings the AGPL duties.

To settle before the code or its output is given to anyone else or used commercially: the licence of the derived 288 px weights, the terms of Wav2Lip, the licence of the detector
weights, and the AGPL duties of ultralytics.
