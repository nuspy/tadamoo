# Vendored: feathertalk

- Origin: https://github.com/anliyuan/FeatherTalk
- Commit: 7a8bb0439eb5da5e10601168df329a7052b7edf3
- Taken on: 2026-11-09, from the raw blobs of that commit (`git show <commit>:<path>`), not from a checkout.
- Size: 154837 bytes in 19 files.
- Patches: none. Every file below is byte for byte the upstream file (SHA-256 of the blob in the table).

## What is not here

Weights are not vendored: `feather_hubert.pth` is the catalog entry `feathertalk-weights`; the face detector and the landmark checkpoint (`data_utils/scrfd_2.5g_kps.onnx`,
`data_utils/checkpoint_epoch_335.pth.tar`) are the entry `engine-landmarks`. The engine copies them next to the code in a run folder before it starts a script. Not vendored either:
the C++ sources of `FeatherTalk-CPP`, the tests, `data_utils/build_clip_audio_features.py`, the demo video and the images.

## Licence

`LICENSE` is the Apache License 2.0 file of the repository, unchanged. Its copyright line is the unfilled template.

## Files

| Path | SHA-256 | Bytes |
|---|---|---|
| LICENSE | 5c9817c129b98e7bb966bca028c43c19107102ef8e03fe799bffb4354f4ef015 | 11356 |
| README.md | 71becc9c3ab0e59bfb6b36fb32228b05413df225cc7d9b5d12bdbbcfc5232ce9 | 7070 |
| requirements.txt | a8f266141fe42ec93aa9f6f163299a69c9bdabda89a38ccc411ffb6c8042ebca | 66 |
| docs/WEIGHTS.md | 429b51dacec2abd9f8a944cfdade24c89460bd64f8b30d14b389342c61aeab67 | 1653 |
| train.py | 042b9edf68f9f1153d48a7b857284afe335113545a6131fcac3d03f744766f42 | 16500 |
| inference.py | 81e7094b9f5202b4e9d19f981259414941860907775f52c33d5aff5c87158712 | 6439 |
| model.py | d1ef3e4ea5695f67a3a8b610af25495e7f7d7bd36334890600a98f343c8b02d6 | 25951 |
| face_utils.py | 8f983f6a8108bb8b40a7589af5ee816a82c38a3a7e1e4e1c38e99876b54390ba | 6320 |
| dataset_mouth_roi.py | 14ba92217c6f56b78e36a42149662302be54b99b8960b6c10780551567317bff | 9143 |
| data_utils/process.py | 1aeee7056341b012a9cc8f5e3266d6ee9962052efe65142b6a91d073d7819916 | 4098 |
| data_utils/get_landmark.py | 16200f0ff6ed6060ac42b5796a460ef55195e2e9451de335385c8384a3d4935d | 3979 |
| data_utils/detect_face.py | 3dc5a8af99abf2c7798f1d8186490c57bbe71c7cfed6f518562c89c30dc5c640 | 5136 |
| data_utils/pfld_mobileone.py | b1ab91e9591bc1ac84aaefcd361adb5f43eb2bf0c6b966d25979f43567fec07c | 17013 |
| data_utils/base_module.py | c86174981155d4fba83c3d271304950b656094731405608b4a7147f1bbcc3263 | 17497 |
| data_utils/mean_face.txt | 40bed2653ae861e9a24b4766c3d085986b7c1245a27373913a9a5b1a6ce069ef | 2394 |
| data_utils/feather_hubert/feather_hubert.py | 1a73d8184794bb728628707e8cbd4cfa61e06d1e3f8512cc19afcffd9ab67e8f | 11178 |
| FeatherTalk-CPP/README.md | 5019383660ac63baba42a98b97fe8fed684489f476549cb1c80d627b6af00a56 | 5156 |
| FeatherTalk-CPP/convert_mnn_models.sh | 75e2e2b3c0c9e1dd260d78b85c2cce11c11b76455fcfd783a73e36a802b981c2 | 1081 |
| FeatherTalk-CPP/tools/export_models.py | afcb1653928710e24514dbf4f962d011bfe7e024839a0cd62d724ef371269fc1 | 2807 |
