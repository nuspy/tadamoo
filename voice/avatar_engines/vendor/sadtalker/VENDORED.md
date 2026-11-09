# Vendored: sadtalker

- Origin: https://github.com/Kedreamix/Linly-Talker (the Linly-Talker copy of SadTalker)
- Commit: dc831b3eea1202728c45042dd92de73ade8c3abd
- Taken on: 2026-11-09, from the raw blobs of that commit (`git show <commit>:<path>`), not from a checkout.
- Size: 252307 bytes in 49 files.
- Patches: none. Every file below is byte for byte the upstream file (SHA-256 of the blob in the table). Some keep the CRLF line ends of upstream
  (`voice/avatar_engines/vendor/.gitattributes` turns text conversion off), so a checkout hashes the same on every platform.

The paths are those of the Linly-Talker tree (`TFG/`, `src/`, `NeRF/`): the files are the import closure of the upstream script the engine runs plus the data files that code
opens, found by reading the code (the same lists as the install-on-request pack they replace).

## What is not here

Weights are not vendored: `SadTalker_V0.0.2_256.safetensors` and `mapping_00229-model.pth.tar` are the catalog entry `sadtalker-weights`, the face detector and aligner
(`gfpgan/weights`) are `sadtalker-gfpgan`. The engine runs the code in a run folder (`<models folder>/avatar-engines/sadtalker/code/`) that it fills from this folder before
each run. Not vendored: the training and evaluation scripts of `src/face3d/models/arcface_torch`, the other face3d options and data loaders, `src/face3d/extract_kp_videos.py`,
the other `src/face3d` and `src/facerender` files that `TFG/SadTalker.py` does not reach, the PIRender config, the web UIs, the other engines and the demo media.

## Licence

See `LICENSE-NOTE.md`. `LICENSE` is the MIT file of Linly-Talker, unchanged.

## Files

| Path | SHA-256 | Bytes |
|---|---|---|
| LICENSE | cbbbe5f1b5da77831c4b62279a8609607b85d886daab2dd5d16f75b9de192eca | 1066 |
| TFG/SadTalker.py | 930f0b95014c7e2e5ab635bfd95ebc6ef704ba2490e4a9947e8de2b5420b5771 | 14264 |
| src/audio2exp_models/audio2exp.py | b7c098a3c2d4e760a1706b2280d1a8b4657be9376e44c603eecac661fb13d035 | 1253 |
| src/audio2exp_models/networks.py | 9cb8d79b6143e5815ccdf0ef25baed44d6b551408e897638dd7bf4cd9c1eae97 | 2977 |
| src/audio2pose_models/audio2pose.py | 69295af76f750412d11607134693a10999669c63e05014d1ed2400772a550ad5 | 3812 |
| src/audio2pose_models/audio_encoder.py | 632d3444b3d901b56f495454678227b0c6b12d375317b77d180cf4a31aa92ff0 | 2745 |
| src/audio2pose_models/cvae.py | 72354f099264fe74be03678e85a1bfb55635574d3e68e9f876d5a8fe8a693cae | 6072 |
| src/audio2pose_models/discriminator.py | 02d02a9cdd5876f3e797439b8ecd5c550b98708559c8f16734b6ca7475d86bb3 | 2669 |
| src/audio2pose_models/networks.py | d8e3788a1119d731324756327db003895dfcc221ef11889565564c5767149c6a | 4247 |
| src/audio2pose_models/res_unet.py | 3f22b3d002bade283250d4641756418e23e7531f3819de6e523dca9a5a1d2758 | 2229 |
| src/config/auido2exp.yaml | 5961f9bf4fb53541deb065efeae7497454836e0362e1c19d0f30e59ee39b1810 | 1209 |
| src/config/auido2pose.yaml | 045223a96f5f447686a866db02455234ccc9b53deb25bf1702550c2cb8ae5dd9 | 1097 |
| src/config/facerender.yaml | 3e2a460eb024b4a6926490ed5c609d3bce46df5eeaf31876bcff1ee3da1d407b | 1087 |
| src/config/facerender_still.yaml | 9fe93b41d221bb82f34ca877a07a230ffb322c9e079dc6bcce3bd7ecf3f7a39f | 1087 |
| src/config/similarity_Lm3D_all.mat | 53b83ce6e35c50ddc3e97603650cef4970320c157e75c241c844f29c1dcba65a | 994 |
| src/face3d/extract_kp_videos_safe.py | 84615576dc0b94baf5c8b4bc061d7ec95a441557c65b35fb89012efeed6f37ce | 5741 |
| src/face3d/models/__init__.py | 14c5fc569d2a775ab35bcae14b641d0b568f78581408ec19c7cc913020eb12e6 | 3090 |
| src/face3d/models/arcface_torch/backbones/__init__.py | 44a007942daef8da07b6ad71a767d37b46e693e80c05716c2dac4130a3f1ba6a | 822 |
| src/face3d/models/arcface_torch/backbones/iresnet.py | 1e257dc81310340e6c68489191f2753dfa4cc0702dddcb661158ce16662b1197 | 7149 |
| src/face3d/models/arcface_torch/backbones/iresnet2060.py | 2bb41694c2af1b532ed78ce5474eb90065354baed1851f3cdc76a52a2893daa4 | 6708 |
| src/face3d/models/arcface_torch/backbones/mobilefacenet.py | d96265a617c82cd58025609c8d2f1c4d5359187b4281812dcab7c8951a744ef8 | 4895 |
| src/face3d/models/base_model.py | 7bac653c3cfe5af9ac3760f8df3b31d72e22f4d2aa230bf6e09033e76ba72bf1 | 13168 |
| src/face3d/models/networks.py | 23089fbe495f406ef25056be2c7386374a32b6ba8965dd6f9c1981f9f083edf4 | 20770 |
| src/face3d/util/__init__.py | bafc2a8cacd14490159ef84e899a4f039ca08aad59418a6eee068f44d1f52b14 | 114 |
| src/face3d/util/load_mats.py | dda88aa1b1340c08b6fbfab55cf6609febfc092920b5cda0a755dfb88f767e3c | 4445 |
| src/face3d/util/preprocess.py | b893ea950c94edc6df92fdf4dd5edc56156021f97cbad81a205f076e18a787a0 | 3336 |
| src/facerender/animate.py | 172e64b348384c847494f8f4435a0803aee62ed230987a02b50c8edb0d276382 | 12075 |
| src/facerender/modules/dense_motion.py | 70bd3715cc7e617471f3065bb452ee43720260b6448afeaf260018dc59bb99a4 | 5866 |
| src/facerender/modules/generator.py | 7b8f1494c92dd5895bf1ea542bba0e8e19e8f28af55869de6a9b0d3553eb2fa9 | 11493 |
| src/facerender/modules/keypoint_detector.py | 462a2ce9f09612b3e4075b0659d52f99ed2458cbc9376a98a03bb03da395013e | 6687 |
| src/facerender/modules/make_animation.py | fd76bec058aac585cecf4a661443a7005cfa72acf81fe292d18f7997d7397cce | 6802 |
| src/facerender/modules/mapping.py | 16a40e34f15e04e0cecff967022b3ebd2879e689bd555b295dc4f17809dd6563 | 1585 |
| src/facerender/modules/util.py | 13479831b799152222e2c3f039bb5fc32a6cb26a03975969430a87fafcce1b00 | 20173 |
| src/facerender/sync_batchnorm/__init__.py | 0701e2062245ab6c90b3df6934a21be5c942b44b13c2ebb70695433599af7a15 | 449 |
| src/facerender/sync_batchnorm/batchnorm.py | cba637062db5ee8cd5d6aaf80cd32cd2a64a8109af6ebdd9aba0618f91e8cd28 | 12973 |
| src/facerender/sync_batchnorm/comm.py | 9d2e776a0398e5ac29ce13d43b7475300f70f409e56f3c38428029000557a57a | 4449 |
| src/facerender/sync_batchnorm/replicate.py | 4498b873404fce05202d4f891262e249a95733816d3877e3aee3d2401138a6af | 3226 |
| src/generate_batch.py | f11bd428216a9d76ee9973ed314aae18200154a44ea5e7be6f616b5ed2d471cb | 4411 |
| src/generate_facerender_batch.py | 05c4ce327122c4a6e0eab9e5e350ba0d7bd0d5bdd588e0d2f7d8ef6f320ee9c3 | 5854 |
| src/test_audio2coeff.py | 2671edc8f22c95e61bc6fb6333ba36b48aed3efe7b14023369c200062204e31d | 5335 |
| src/utils/audio.py | d394cd719544aa5daa7cb4de6ab32089b2aafb0a748a86d0e77750417aaaf70f | 4518 |
| src/utils/croper.py | f5f3db3a0d35bde8658b13982e3a95085644694197adaaa8e8673f2c0cffe43d | 6102 |
| src/utils/face_enhancer.py | 0daececa32e1258dc0b34e6d2dbefdf4ec62ff4d224ac66c1930d7e1ed74abcb | 5014 |
| src/utils/hparams.py | 2ed3e2b713c6e9bb388637a3d401559fae0caf24cbe00f9a74f134c3a39a681d | 6055 |
| src/utils/init_path.py | 36e5de40570251f7c3d3bfb025a75e8595132c0970b2ce5bb9e5ed3d67c57e3d | 1563 |
| src/utils/paste_pic.py | 42754d0b214722c12efee05fdc8e59f2f9344a32e901f03c214ba4baa8a59077 | 2390 |
| src/utils/preprocess.py | 4be6517e64a5f01f24804336ab2bbb992bb776ff3fccfe58d966e50d5a78888c | 6890 |
| src/utils/safetensor_helper.py | 35931a02a7a620508f6ec7281ff648228cc2e51708db0014a54036a64aed6c56 | 198 |
| src/utils/videoio.py | e521e17e5092d27fb4a9fe6238e55d3b336637808f91a32ff26eb30dfa7769d8 | 1153 |
