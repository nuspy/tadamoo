# Vendored: ernerf

- Origin: https://github.com/Kedreamix/Linly-Talker (the Linly-Talker copy of ER-NeRF)
- Commit: dc831b3eea1202728c45042dd92de73ade8c3abd
- Taken on: 2026-11-09, from the raw blobs of that commit (`git show <commit>:<path>`), not from a checkout.
- Size: 486227 bytes in 59 files.
- Patches: none. Every file below is byte for byte the upstream file (SHA-256 of the blob in the table). Some keep the CRLF line ends of upstream
  (`voice/avatar_engines/vendor/.gitattributes` turns text conversion off), so a checkout hashes the same on every platform.

The paths are those of the Linly-Talker tree (`TFG/`, `src/`, `NeRF/`): the files are the import closure of the upstream script the engine runs plus the data files that code
opens, found by reading the code (the same lists as the install-on-request pack they replace).

## What is not here

Weights and per-person data are not vendored: `checkpoints/audio_visual_encoder.pth` is the catalog entry `ernerf-audio-encoder`; the four AD-NeRF 3DMM files (`ernerf-bfm`) and the face-parsing weights (`ernerf-face-parsing`) are catalog entries too; only the Basel Face Model file
(`01_MorphableModel.mat`, a registration) and the `3DMM_info.npy` that `convert_BFM.py` makes from it are placed by the person. The engine runs the code in a run folder
(`<models folder>/avatar-engines/ernerf/code/`) that it fills from this folder before each run, so the CUDA sources are there to be built (`freqencoder`, `gridencoder`,
`raymarching`, `shencoder`, each with its own `setup.py`). Not vendored: `NeRF/nerf_triplane/gui.py` and `asr.py` (the live viewer, which nothing here imports), ER-NeRF's own
`main.py` (not in Linly-Talker; `runners/ernerf_train.py` stands in for it), and the rest of Linly-Talker. Linly-Talker's `NeRF/` is a modified copy of ER-NeRF, not the upstream tree:
its `nerf_triplane/network.py`, `provider.py` and `utils.py` differ from github.com/Fictionarry/ER-NeRF (an audio encoder for `ave`, a wav2vec path, the audio muxing).

## Licence

See `LICENSE-NOTE.md`. `LICENSE` is the MIT file of Linly-Talker, unchanged.

## Files

| Path | SHA-256 | Bytes |
|---|---|---|
| LICENSE | cbbbe5f1b5da77831c4b62279a8609607b85d886daab2dd5d16f75b9de192eca | 1066 |
| NeRF/data_utils/deepspeech_features/README.md | fea60ef8d5a6225f5ad16e6af85afbd3ce3bbacd505dbea91a8302c4bb17070e | 498 |
| NeRF/data_utils/deepspeech_features/deepspeech_features.py | 9d3b54ffdc4d84361ba66c07c5c7ce7c9cc4014b7191306e3457adf16ee0ebe5 | 9280 |
| NeRF/data_utils/deepspeech_features/deepspeech_store.py | 6b04a3b502536f5d0a168195a1be148537dd7b572b4858f0c696fb2e81a46bc4 | 6208 |
| NeRF/data_utils/deepspeech_features/extract_ds_features.py | 05d36bbd11d2c1a4e381eb4986bdae71c0b7bf39f1421bd473b5f581ca5f5240 | 4356 |
| NeRF/data_utils/deepspeech_features/extract_wav.py | ea32f2dc825bf8ee28460feb42a0caa61139770120245a2dbbf9a2ca74c97807 | 2634 |
| NeRF/data_utils/deepspeech_features/fea_win.py | 81d431dfb84e1d27d84cefe25732f159bf88e150c2256de5fd08665a472610ad | 458 |
| NeRF/data_utils/face_parsing/logger.py | 2d25bd444c9455c6635a065c788fedda2f360ae1d0a052021e476159baad1b9d | 598 |
| NeRF/data_utils/face_parsing/model.py | 524039ffa675d259672b0837ca4973fe1aedf79d99768f3f405124657dc7df83 | 10628 |
| NeRF/data_utils/face_parsing/resnet.py | 3c33b212a28cdcff1b3f1ab4aa9f59247bf17e6ba66bdc506056f2bf8930ad95 | 3648 |
| NeRF/data_utils/face_parsing/test.py | b771fe7259b15a95193f04fa38ebe6157ed545ecea8f0637dfab68693a340b34 | 3661 |
| NeRF/data_utils/face_tracking/__init__.py | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 | 0 |
| NeRF/data_utils/face_tracking/convert_BFM.py | 34de03a7aa4d663dc1885d63e8eb2e90121c4ea27afdccaff2026ece939be829 | 1189 |
| NeRF/data_utils/face_tracking/data_loader.py | 4889c2952f1128363c96cb41673df9d6b3d02938765bc0f0bb309a741330f022 | 480 |
| NeRF/data_utils/face_tracking/face_tracker.py | eab0f2b1c3de72d3e632f3ff859cdf75c21b3169747fd289a1474d6ddb5d28a9 | 14036 |
| NeRF/data_utils/face_tracking/facemodel.py | 6fbc64a05836494362e8dce9f4bd7347df9940c67177dbd83afed6fe266c8216 | 5966 |
| NeRF/data_utils/face_tracking/geo_transform.py | cd53e637c01b6873275d6f568678f0af4a9af2690cc547e9027e6c6e20278d52 | 1970 |
| NeRF/data_utils/face_tracking/render_3dmm.py | 7ab70b4b55270c86ebdc183ab11d09b9beb71a7eb7cb272748addfe44c15544f | 6781 |
| NeRF/data_utils/face_tracking/render_land.py | fe76efcc5fb31cd57e7fc2eb36a720b31bce2f570a5708129a331064f23169df | 6803 |
| NeRF/data_utils/face_tracking/util.py | aaf9fe1d2fc85c470d135deba2ce1ac3ac3f62d6892a474f1c6058d3fc05ce93 | 3331 |
| NeRF/data_utils/hubert.py | 449abb98199992eaead222ffdec9388fb680ac461c2f5f3190df4f557fe5f96b | 3762 |
| NeRF/data_utils/process.py | b82a5f3850d897c47f38ef8e5f1303b95780cffe1a17f1c88a601028f64aed03 | 15925 |
| NeRF/data_utils/wav2mel.py | ea901eee1b47e796ed3b49365119d5ce5b012a4f39e65261998e1662d4ddc263 | 5726 |
| NeRF/data_utils/wav2mel_hparams.py | 309c506a220a39472acec9e470fd371ecded4c8730dfcb44aa74bf95b6d2b33e | 3072 |
| NeRF/encoding.py | 49c4139978d01e0f33abd709c05ec2f7c3ccbb4a958c0280f5baeb5cbbcef332 | 1788 |
| NeRF/freqencoder/__init__.py | bbea07445527a6fea279bebe73897e8f945eaf0e1b1a9d22314abac41ea32a14 | 29 |
| NeRF/freqencoder/backend.py | 2619b41721bde19032951800d82f1efe02260228081e5d7639951e797aae0fe7 | 1422 |
| NeRF/freqencoder/freq.py | 5e0a750230791d1cba33cdc6d3a6ba8f1844ff814f903be64ba381cecd7c9aa6 | 2308 |
| NeRF/freqencoder/setup.py | 8117dff170be9f675d2e36a49cace0b11dc97c882cbb1537ccd0b460c2ae4063 | 1763 |
| NeRF/freqencoder/src/bindings.cpp | 83256b5715f80fd2c8e37ddf37c6a90b7ad98f0f410833915df5d5e693c9d8be | 282 |
| NeRF/freqencoder/src/freqencoder.cu | 1dc7d01a0d13fe12440abf1f277930bdd92c8affa58d87a392b74298859cd689 | 3701 |
| NeRF/freqencoder/src/freqencoder.h | e391a8449f0a079dfb676f8b0dda3fc43c81fd17311c6580ad2a3bccacacb8e6 | 549 |
| NeRF/gridencoder/__init__.py | 00570fbd96d5546ca2ff8cdda6146f253232bdbc38d92078f4af17ba4530ffe8 | 29 |
| NeRF/gridencoder/backend.py | 7ba7093c4638500ffb101385212792ef687e1997f3677a14d11db7cc29dd27de | 1451 |
| NeRF/gridencoder/grid.py | d15970d47120eabb916c4dbb44e17aca7af389ca81d452d1ddd805b6d24e35ba | 6486 |
| NeRF/gridencoder/setup.py | 28127bdff4b3e9321d0950396f32b06d2a778d37b90b3f8d5a2dd8eaa2bdcda3 | 1741 |
| NeRF/gridencoder/src/bindings.cpp | 9ec970d9d53314befb8521b7ed90e8e0f67f95a802b572ed97309723aefe720b | 282 |
| NeRF/gridencoder/src/gridencoder.cu | e227b5af69b1281ebafaee447c26754e4278f0f3b3d4fc6a9efa04c16091b2a1 | 20535 |
| NeRF/gridencoder/src/gridencoder.h | 97a6dc22f204b7196c5a9d0b78eef6da55b67d5d76056a393eeb6072fb7e2eeb | 966 |
| NeRF/nerf_triplane/network.py | aeb869b1091eb05968ba1e11b0b4a449bfe3f727e12a9d986b6446b235ad7b23 | 17076 |
| NeRF/nerf_triplane/provider.py | 023f629b69bf76c3901b4b65932a3004f986941f9e60d530f2b3ccc1a28e6fad | 34898 |
| NeRF/nerf_triplane/renderer.py | 6dcebab3ad59672dad28c942932d85c0dce29ae2d308da687663d6c584ea3f8c | 30806 |
| NeRF/nerf_triplane/utils.py | 97bc0a11350bd88b341386d04bb029ecd24e033b604e70162cb43af5fb10ffea | 62462 |
| NeRF/nerf_triplane/wav2vec.py | 743a1aae45cf3161a1ab7b2aefc6d470efa2e43b4c99e275e0513c74445dfdf7 | 15084 |
| NeRF/raymarching/__init__.py | 8913bc819411d5e661c5a2cdd179a135c7a498ce54b2e182ae4ec02cef6ad073 | 26 |
| NeRF/raymarching/backend.py | e03e02f7538e381f4e026c8cc2e865aeb95934c8f270a95a5f68558bd2b3cd32 | 1405 |
| NeRF/raymarching/raymarching.py | 6f792ab8f92c086031777c1d091353632063ad728491e8591dcc1a76912e4136 | 28660 |
| NeRF/raymarching/setup.py | 1070424d4fb517f30486db62261abc5dab8c3636f474f532920d2440fa758640 | 2199 |
| NeRF/raymarching/src/bindings.cpp | 636e983f28b1c97e6cfc2cb70f12fbd53043431fba9d77d0648f3c17d28cc800 | 2413 |
| NeRF/raymarching/src/raymarching.cu | 9c70992617a6a524c19f1ef4e10fa273a6b378c42cb5504d68f1d99973fa666c | 80335 |
| NeRF/raymarching/src/raymarching.h | d510fd34c5664668ef2ce086dfd6a4ef7b54ecc1f9f32fa0107f0eba754fdb3e | 7103 |
| NeRF/shencoder/__init__.py | f4aa41f2e0e3f1b4749f24124fce2ff72d35084dd8ff86d4fc87c4a62dc2bee2 | 39 |
| NeRF/shencoder/backend.py | f45aa8184fb6b1eed53ad8447a276a7c3f309f767cf253e0a62e99f36176786b | 1447 |
| NeRF/shencoder/setup.py | b2c962b5e53912f9d9e66436ed3a092ba65655a2ea51aa63b6a1da329813f5bd | 1735 |
| NeRF/shencoder/sphere_harmonics.py | 79fbb185dd5dcf493949219a4ab4ad4d60ad4b805fcdef250fe35ad5d54b2846 | 2783 |
| NeRF/shencoder/src/bindings.cpp | bde9a7166a4a8b9e1eb24ff4482122375f8a684fb6e028c08fdc5659838f8fe7 | 268 |
| NeRF/shencoder/src/shencoder.cu | cc1e579eee2d465cbf03075d02a10c2c1bcbb36d481af201187bc09cf9cd8458 | 37565 |
| NeRF/shencoder/src/shencoder.h | d4d0aeab4cc770079c81743c8aa9c125b679b9052195438ebe5a9b2e289256b6 | 439 |
| TFG/NeRFTalk.py | 451924cfb3136a5a3f6d61deb8fd1e6d54f7265b456d8278b0a31799d0de5e68 | 4076 |
