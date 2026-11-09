# Licence of the ER-NeRF code

Status: licence to confirm. This code is vendored for the experimental phase; the licence is not a blocker now, and nothing about it is shown in the GUI.

What is known, from files read (not legal advice):

- `LICENSE` is Linly-Talker's, MIT. It covers Linly-Talker's own files (`TFG/NeRFTalk.py` is the wrapper). No file of this folder carries a licence header of its own.
- `NeRF/` is a modified copy of ER-NeRF (github.com/Fictionarry/ER-NeRF), whose LICENSE (read at commit 0cf72d8f on 2026-11-09) is MIT, copyright 2022 hawkey.
- `NeRF/data_utils/deepspeech_features/` says in its own header "Based on VOCA code. See the corresponding license restrictions": VOCA's terms are not in this tree and are
  to be read before any non-research use. These routines are what the data preparation's default audio model (`deepspeech`) runs.
- `NeRF/data_utils/face_tracking` and `face_parsing` follow the Basel Face Model and face-parsing codes of ER-NeRF; the Basel Face Model itself needs a registration with its university
  and is not part of this folder. The weights and the 3DMM files are separate and not vendored.
- The CUDA modules (`freqencoder`, `gridencoder`, `raymarching`, `shencoder`) look like the torch-ngp encoders (not checked); their own terms are not stated in this tree.

To settle before the code is given to anyone else or used commercially: VOCA's terms, the terms of the Basel Face Model and of the face-parsing weights.
