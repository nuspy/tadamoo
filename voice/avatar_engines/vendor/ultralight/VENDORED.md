# Vendored: ultralight

- Origin: https://github.com/anliyuan/Ultralight-Digital-Human
- Commit: 5aa8b5a8aae4b003b7de5750abeac5343a7b38e0
- Taken on: 2026-11-09, from the raw blobs of that commit (`git show <commit>:<path>`), not from a checkout.
- Size: 185522 bytes in 21 files.
- Patches: one, listed under Patches. Every other file below is byte for byte the upstream file (SHA-256 of the blob in the table); the table holds the SHA-256 of the file as it is here.

## What is not here

Weights and models are not vendored: the face detector (`data_utils/scrfd_2.5g_kps.onnx`) and the landmark checkpoint (`data_utils/checkpoint_epoch_335.pth.tar`) are the
catalog entry `engine-landmarks`, the wenet audio encoder (`encoder.onnx`, which the upstream README says to download separately) is the entry `ultralight-wenet-encoder`.
The engine copies them next to the code in a run folder before it starts a script (voice/avatar_engines/personalised.py). Not vendored either: the demo images, `dihuman_run.py`
(the streaming demo), `data_utils/wenet/bin`, `data_utils/wenet/transformer` except `swish.py`, and the other files of `data_utils/wenet` that no script here imports.

## Licence

See `LICENSE-NOTE.md`: the repository has no LICENSE file.

## Patches

`data_utils/get_landmark.py`: one line removed. Upstream has `from os import wait3` and never uses `wait3`; Windows has no `os.wait3`, so the import fails there and the
preprocessing cannot run. Before (lines 1 to 3):

```
import argparse
from os import wait3

```

After (lines 1 to 2):

```
import argparse

```

Upstream blob: SHA-256 `cbac181a5f49a3b7c4efc62374ab463f3e724bfcfcd8e0135435e6617c9b350e`, 3848 bytes. Here: the SHA-256 in the table, 3827 bytes. Putting the removed line back after
`import argparse` gives the upstream blob again (`voice/tests/test_avatar_engine_vendor.py` checks it).

## Files

| Path | SHA-256 | Bytes |
|---|---|---|
| README.md | a0aa8be0b9d08a55f051913dabf0865189714b6fcad72f9b55a22ce60edd4e1b | 11888 |
| train.py | ad37c2b48c1875744c5a5d62a349f938cc9a4790a31ede6853f930412082f0bf | 6932 |
| datasetsss.py | f51cc3efd681f6a6b79fe6d531000cd2d8c03a216620f36548a249479c32417e | 2660 |
| unet.py | 3cda255dbb76d422c8bbbd65545207ae6671e0c002e8c76261ff9e38acb39113 | 8536 |
| face_utils.py | c59ef9a4c0f50bb06453e9d99eaf1780dca271ed3e61578ed07a455e6067389f | 4695 |
| inference.py | 892ca62e1aff516dcee8c8c65c4f2be9b07cb718b53ad00d2c9b77924bb9802a | 6178 |
| pth2onnx.py | d1126b3891fbfd6b2db9c83f572e843ec4ac58a7ef2e6322c1593bdb091ae453 | 2421 |
| data_utils/process.py | ad4f1f56ed28c407c86180d0074ce16a7e5c1be6531ed3325a18f44e05aa5174 | 4088 |
| data_utils/get_landmark.py | 0007be8c6af2001f39c4af7aaa7d4e173fbb7bb049eab1f2087fcd40a2309b75 | 3827 |
| data_utils/detect_face.py | 3dc5a8af99abf2c7798f1d8186490c57bbe71c7cfed6f518562c89c30dc5c640 | 5136 |
| data_utils/pfld_mobileone.py | b1ab91e9591bc1ac84aaefcd361adb5f43eb2bf0c6b966d25979f43567fec07c | 17013 |
| data_utils/base_module.py | c86174981155d4fba83c3d271304950b656094731405608b4a7147f1bbcc3263 | 17497 |
| data_utils/mean_face.txt | 40bed2653ae861e9a24b4766c3d085986b7c1245a27373913a9a5b1a6ce069ef | 2394 |
| data_utils/hubert.py | 4871d2bdfd2bee50121d75010c8c8f74ab0bbf55595a9ffa142e90ec4ba0060a | 3577 |
| data_utils/wenet_infer.py | a274253d6351c12f060126c8f0fa44215b50587b365b0f1abbed9d91606a2003 | 26163 |
| data_utils/FeaturePipeline.py | eabe4c8cd18326806ccd3bc99aa5ab0160c388d6f7dfbd456bd5bb9af4825636 | 3086 |
| data_utils/wenet/utils/common.py | d6e27ace1afde121eae2b87e0fc40b8ba049a9c38aeae4bf5da38fee0297fea7 | 5536 |
| data_utils/wenet/transformer/swish.py | 885596e8927ba7cdeff1bcfa35f94d08f4d62a31239f0fac7ba2d8577a74c153 | 511 |
| data_utils/conf/decode_engine_V4.yaml | 713017ffdfed982b63234197fac43f1540bebac3e9d342cb3f4ebf189281b32c | 2000 |
| data_utils/conf/wenetspeech_unified_conformer/global_cmvn | 0dc671e9e07274cea4519b19511f08562d2b6e7663ef86b02d059c9d0b80f139 | 2692 |
| data_utils/conf/wenetspeech_unified_conformer/words.txt | fe74e572e18456633e19305aa237db706448f9aa18f0c841c8b6f89bea061e77 | 48692 |
