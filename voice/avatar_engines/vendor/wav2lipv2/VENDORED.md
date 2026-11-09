# Vendored: wav2lipv2

- Origin: https://github.com/Kedreamix/Linly-Talker (the Linly-Talker copy of Wav2Lip v2 288 px)
- Commit: dc831b3eea1202728c45042dd92de73ade8c3abd
- Taken on: 2026-11-09, from the raw blobs of that commit (`git show <commit>:<path>`), not from a checkout.
- Size: 95938 bytes in 26 files.
- Patches: none. Every file below is byte for byte the upstream file (SHA-256 of the blob in the table). Some keep the CRLF line ends of upstream
  (`voice/avatar_engines/vendor/.gitattributes` turns text conversion off), so a checkout hashes the same on every platform.

The paths are those of the Linly-Talker tree (`TFG/`, `src/`, `NeRF/`): the files are the import closure of the upstream script the engine runs plus the data files that code
opens, found by reading the code (the same lists as the install-on-request pack they replace).

## What is not here

Weights are not vendored. `checkpoints/wav2lipv2.pth` is the catalog entry `wav2lipv2-weights`; the face detector `yolov8n-face.pt` (`wav2lipv2-yolo-face`) and the WFLW
landmark net (`wav2lipv2-landmarks`) are catalog entries too. The engine runs the code in a run folder (`<models folder>/avatar-engines/wav2lipv2/code/`) that it fills from this
folder before each run. Not vendored: `src/utils/face_enhancer.py` and `src/utils/videoio.py` (imported only when `enhance=True`, which the runner never asks for), the first
Wav2Lip model (`src/models`, `TFG/Wav2Lip.py`), `face_detection/detection` (the S3FD detector, which `api.py` loads only when a `FaceAlignment` is built), and the rest of Linly-Talker (the web UIs, the other engines, the demo media).
`ultralytics`, which `TFG/Wav2Lipv2.py` imports for the face detector, is a package of the engine environment and is not vendored.

## Licence

See `LICENSE-NOTE.md`. `LICENSE` is the MIT file of Linly-Talker, unchanged.

## Files

| Path | SHA-256 | Bytes |
|---|---|---|
| LICENSE | cbbbe5f1b5da77831c4b62279a8609607b85d886daab2dd5d16f75b9de192eca | 1066 |
| TFG/Wav2Lipv2.py | a0374b431969dca1d22779022482f283942343e328ca0abc9d5ca5f9b1d6ea51 | 16037 |
| face_detection/__init__.py | 7215846a10fcb2162dab619cf1cb45bd6c0faac2b7d1a71ed8cc1cc0811db86a | 183 |
| face_detection/api.py | c050676db49d8eb5de19f13c171101892b60b992b2a730cfbc8534967e4faeaa | 2266 |
| face_detection/models.py | 2cde93a32467b9c5cfd009499c917afe8c89f512622237c2d865ba8fbb8a37e9 | 8619 |
| face_detection/utils.py | 561bfe5ec349a28f086e04efbb2d43cbc45c15fbcac48c311d497cb2bd2efe9a | 11808 |
| src/modelsv2/__init__.py | 8d4e18f750e5fa8c96282d89a2f56b65b313151f974967b0b2dc3ecf63c02e55 | 85 |
| src/modelsv2/conv.py | 9cdf6a033f3105daee400df8f8e32711c161b16f1811387b7409be2d0ef5d1bb | 1620 |
| src/modelsv2/syncnet.py | f3458489a8dcbc4684629cf50678869f1123ac27cdae1fc558f8b885e0fe3028 | 3219 |
| src/modelsv2/wav2lip_v2.py | 2ed64ce51313775ce8d17d6971eec9c715a6523a3383d069cdbf559b9a8df19a | 10826 |
| src/torchalign/__init__.py | af91fd02ae41ca6d688b06a4e3016b346fd4cdff4e1b1208a16656513e5dd0af | 18 |
| src/torchalign/api.py | 0a7e9868c4bb31e17ab2126709137f17e374c3b7520b189de9fa84807f715771 | 2660 |
| src/torchalign/backbone/__init__.py | b7901092b7577dc7d4d83717ff452ef2b93d72d5397f99e13a71eb2e391ba592 | 46 |
| src/torchalign/backbone/hrnet.py | d73d03315b2cda96ee4e01f6b5ef2153cfa8c20b651dc6a33bbc1c4d0b243ee3 | 16658 |
| src/torchalign/backbone/mobilenet.py | f0b3e61cc1811f25c2bd154e2a21b5b889457d5e0d1be0a11367b0f3c5a7e83e | 2929 |
| src/torchalign/cfg.py | 424cac0b0f637e40072b2dfb696cda4228f5ccad1e29bfe08454e25776a8253b | 1067 |
| src/torchalign/heatmap_head/__init__.py | 686ce54b7aabe8ce69a87c9d54815b4f8c523054c271115eae8bdb13aa3453d6 | 27 |
| src/torchalign/heatmap_head/blocks/__init__.py | dc89251e4483993dcc8a0bcd54c3eba26455d36c160bc82c1ee485c1af41c38d | 25 |
| src/torchalign/heatmap_head/blocks/head_block.py | 6906b59b5b80040df2595495dfaeff8a4fea07fb8356cb9c493b38fa3697551f | 680 |
| src/torchalign/heatmap_head/heatmap_head.py | cb91f418b77a1bb487f49c9425d18a75744e10fbeb130f6a1124e1f024fb0c8b | 732 |
| src/torchalign/heatmap_head/transforms/__init__.py | 3dae244f6f20c4163eb21ebe67bf60ad047434129a486cd6f674d273b812233d | 48 |
| src/torchalign/heatmap_head/transforms/functional.py | cad329b150c9d813a4c5fc14ee741944aaf14376aa45e653025c2f62b5597d89 | 305 |
| src/torchalign/heatmap_head/transforms/module.py | 774ffb949708a5f70ddf9845c2621057fca3482d5eb0250a49128ec75f4e5737 | 737 |
| src/utils/audio.py | d394cd719544aa5daa7cb4de6ab32089b2aafb0a748a86d0e77750417aaaf70f | 4518 |
| src/utils/hparams.py | 2ed3e2b713c6e9bb388637a3d401559fae0caf24cbe00f9a74f134c3a39a681d | 6055 |
| src/utils/utils.py | 758beaa2445c4543587804867fd03ccbe65f668994864f48517fe4f237e03667 | 3704 |
