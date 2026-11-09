# MuseTalk — copia vendorizzata

Questa cartella NON e' un clone: e' parte del repository callAPIcall, e le
modifiche che ci facciamo sono nostri commit come tutti gli altri.

## Perche'

Prima era un clone di upstream, in .gitignore e con un `.git` proprio. Due
conseguenze, entrambe brutte: qualunque nostra ottimizzazione era invisibile al
nostro repository e sarebbe sparita alla prima reinstallazione, e l'installer
clonava `--depth 1` senza fissare un commit, quindi due macchine installate a
distanza di settimane prendevano codice diverso.

## Provenienza

    origine:  https://github.com/TMElyralab/MuseTalk.git
    commit:   0a89dec45a0192b824e3cf4daf96c239440c5ed8
    data:     2025-09-26
    licenza:  MIT, Copyright (c) 2024 Tencent Music Entertainment Group

Il file LICENSE originale e' conservato qui accanto, come la licenza MIT
richiede. Le nostre modifiche sono identificabili dalla storia di git:

    git log --oneline -- avatar/MuseTalk

## Cosa NON e' stato incluso

  - `models/`  i pesi (~7 GB): li scarica l'installer dalle fonti ufficiali;
  - `assets/` e `data/`  i materiali di esempio. Il README di MuseTalk li
    dichiara "available for non-commercial research purposes only", a
    differenza del codice (MIT) e del modello addestrato ("any purpose, even
    commercially"). Non ci servono, e includerli ci legherebbe a quel vincolo.

## Aggiornare da upstream

Non c'e' un `git pull`: si confronta a mano con il commit di provenienza qui
sopra, si porta cio' che serve e si aggiorna questo file.

## Modifiche locali ai pesi

I pesi si leggono dalla cartella indicata da `MUSETALK_MODELS` (la cartella dei modelli di TaDaMoo, `avatar/musetalk-v15`), mai da `./models`, e
niente si scarica: `musetalk/utils/preprocessing.py` (DWPose), `musetalk/utils/face_parsing/__init__.py` (face parsing) e
`musetalk/utils/face_detection/detection/sfd/sfd_detector.py` (S3FD, che non chiama piu' `load_url`).

## Integrity (SHA-256 per file)

Every file here is checked by `voice/tests/test_avatar_musetalk_vendor.py`: the table is the SHA-256 and size of each file as committed
(line endings read as LF, so a Windows checkout with CRLF gives the same value; the `.npz` is read as it is). Unlike Ultralight and
FeatherTalk, this copy is edited in place and the upstream blobs are not kept, so the table pins the committed state: a change to any file is
deliberate and updates its row in the same commit. Files changed from upstream or from the callAPIcall snapshot: `sfd_detector.py`,
`face_parsing/__init__.py`, `preprocessing.py` (weights read from the models folder, nothing downloaded) and the two scripts below (a comment).

## Not run by the app

`scripts/inference.py` and `scripts/realtime_inference.py` build shell commands with `os.system` (ffmpeg, from file names). Nothing in the app
imports or starts them: the avatar worker imports the `musetalk` package only, and the test fails if any other file of the repository names
these scripts. Each carries a comment saying so. They stay as upstream's command-line entry points; if the app ever calls them, the commands
must become `subprocess.run` with an argument list first.

Size: 3201614 bytes in 81 files. The weights, `models/`, `assets/` and `data/` are not here.

| File | SHA-256 | Bytes |
|---|---|---|
| .gitignore | 70b7e269294078dae676d3e6418e4a68e7946135f2afe5db7eff7d51100421f6 | 617 |
| LICENSE | 992ec5fd1dd4964cfa003665196cd0c0c10a7a5aa10109991e964eebd2c7f116 | 13427 |
| README.md | 63587c38fa53bd3292654a71aa6978628db02dd02cc93153030f3d0dca7721bf | 22817 |
| app.py | ea3cd883dd51a693b2456379f9fd72263be0b4aa12ecdaed49898419003ca957 | 21534 |
| configs/inference/realtime.yaml | c1f85ce231427316814870c1f13e34e8ee8a065886ebdecba10bf425844a8645 | 259 |
| configs/inference/test.yaml | 85c3e131c4ea859daea84cd03f3fa2a0f7288659386ec54827dcb279784db509 | 182 |
| configs/training/gpu.yaml | a6673f68f9693f1ebf6b67deacd47a88176bce367ab33f5eaa103ea514344094 | 511 |
| configs/training/preprocess.yaml | f234faf6aede467d1f2096cf24249e43ab0d27af9c2f8ed71c1d7157ea99c0b5 | 964 |
| configs/training/stage1.yaml | 31a57f8a2f389c2c3baf719fc702c42b023cea8e890cc8e0ef36e374c1e262fc | 4581 |
| configs/training/stage2.yaml | e3ee55c3ca0c430964277e25afc290ab70f042d204a38f3e610504ebf649fa6b | 4606 |
| configs/training/syncnet.yaml | a7057578f815a9e759c7f59476978f0ad6b6736e9ce03bad6fe295341f702922 | 834 |
| download_weights.sh | 926e9f48ca878829eb145ce0a40cc17472c14cda73bf408604358cccc28c78a0 | 1734 |
| entrypoint.sh | b25b95683bb7809c4d870286d35a14ac897e06f572b51b50a77b7fbbf9ff136e | 143 |
| inference.sh | 8c8e48b02fc8a3ebf15740e9aff948b219ede90543831f8902e0577dcb897660 | 1949 |
| musetalk/data/audio.py | 962ab27111f75baadfc6a838b8941228a49fa7eaa4c6058f37309489ec524205 | 5471 |
| musetalk/data/dataset.py | eb7a2f2ab2e54e89ee56f83772a71a78935eab9834c520bcdac18a4fa59c3140 | 22776 |
| musetalk/data/sample_method.py | 104972ed1215af9abaaa3a829e73a9a89fbce7e4287edbddd04862d974754120 | 10979 |
| musetalk/loss/basic_loss.py | 616bf9e089c1794a362786fae6e1f4358d178fdc5869e07a11046df4f926cff4 | 3338 |
| musetalk/loss/conv.py | 72caaee80ae72552aec2c3e08220c7a400c075bcfb8a580a5d5a23f6e80709d3 | 1619 |
| musetalk/loss/discriminator.py | 775d37c520d1e1024939183cfc3c156b3c8e48baaa33f917541c22951a0732aa | 5145 |
| musetalk/loss/resnet.py | 4bd0d246186f5e47eb23eea219a47f572a41f4228ce308a1bc689041c433ef3f | 4645 |
| musetalk/loss/syncnet.py | 78c54618796e52317851a574bf7440deb49fb3abe957b9c0bcce8da78f9c41e2 | 4214 |
| musetalk/loss/vgg_face.py | 80d1bb895b092595564d2068f8f13ecfa05df65f5e41d7abdb5ecb8dc1a36f6e | 8307 |
| musetalk/models/syncnet.py | fc3caaba34baae5ef2490a997aad7de2700bf43b1d8c1715ebcc78ceb2470470 | 8987 |
| musetalk/models/unet.py | 6f0929e1e089c024fae037af159a2e9f939865024c0cb76c5007b9d662da3ddc | 3878 |
| musetalk/models/vae.py | e3c6077294acf8a8c6cd971173179b13e211751eae7a6e3a93b6fa315fe93e29 | 5871 |
| musetalk/utils/__init__.py | ccd6f1e5ebcc92d26d7322a565c120d93aeda34c6ccba5e9aaa6f1138d7d91fb | 160 |
| musetalk/utils/audio_processor.py | b7b62f3daa99678576138bf581a6394a10d346571a2530a760d1299ffd622b81 | 4227 |
| musetalk/utils/audio_utils.py | c38ff3f95a83577612f9a0ff867fff3f28df7de18b1b9563cdee4bfd45e60b9c | 718 |
| musetalk/utils/blending.py | 31951d5ecd4ea15be50643eb70a6f0f6fcf80206826a7b62beefbe3dc0d41c04 | 7548 |
| musetalk/utils/dwpose/default_runtime.py | bb7df18270b1ca337192faae7a416b2dea23b45e798e832caab5781f41189d06 | 1431 |
| musetalk/utils/dwpose/rtmpose-l_8xb32-270e_coco-ubody-wholebody-384x288.py | 7c0da61ee9517ee6c97ec10dd6be592eb51501434a93fa0a8e7f3f3739b5e477 | 7364 |
| musetalk/utils/face_detection/README.md | f41720c331cfb2f079b465643237904d0ce53d7365959c7f1814a582963e00a3 | 209 |
| musetalk/utils/face_detection/__init__.py | 39b9365b1fd90ae823be551c6d55dce6b253fe1ecb015853b724eee3bdcfa470 | 196 |
| musetalk/utils/face_detection/api.py | f16098febe99d47b8886ccad9fc75ef9aecbf3a97eb51f3b459ec8da42aa0561 | 10100 |
| musetalk/utils/face_detection/detection/__init__.py | 1f3233930bfb7ac8b1726e4af8ed9b93925046d16edc640afd218f9d02b6d235 | 30 |
| musetalk/utils/face_detection/detection/core.py | ba16a80fac378da9b5bbfc9c190975ab4fe153b13b0d18d892ab5bbfcbb9c71e | 4868 |
| musetalk/utils/face_detection/detection/sfd/__init__.py | 6fbfb62eccc1a14de7c0a8cb1ca6f3c59af42479955ac08acbf548f28ec1feed | 53 |
| musetalk/utils/face_detection/detection/sfd/bbox.py | 3ed1487243bbde8ab47c567f46ee4b177a549e31440db8d71376bffc70a7bd86 | 4279 |
| musetalk/utils/face_detection/detection/sfd/detect.py | 2bb9e9383e6a582d216515aab36759652cb9e7800a1910a6394ff721c4291a90 | 3792 |
| musetalk/utils/face_detection/detection/sfd/net_s3fd.py | 80960aaefd9fb97a5e9784dc0a48bc38f43bdcaacb268782ede16a959e77a5a9 | 5291 |
| musetalk/utils/face_detection/detection/sfd/sfd_detector.py | 60fc0b2e3a360f99968bc8a7921072f8e1aff4c42ad400f7a766ce4fb7487641 | 1843 |
| musetalk/utils/face_detection/models.py | 2cde93a32467b9c5cfd009499c917afe8c89f512622237c2d865ba8fbb8a37e9 | 8619 |
| musetalk/utils/face_detection/utils.py | 561bfe5ec349a28f086e04efbb2d43cbc45c15fbcac48c311d497cb2bd2efe9a | 11808 |
| musetalk/utils/face_parsing/__init__.py | be3d38ee86271499a1663ea3307a8f5926d5c5cbf12e4f86290262b77b3f0f1d | 4809 |
| musetalk/utils/face_parsing/model.py | 05d69d1bd77fbca7b537602fe3f7851e8b2e1a3ea0279f383b4223598157e2d2 | 10676 |
| musetalk/utils/face_parsing/resnet.py | a3419dabeff41c535e24f4d056b054c1bfc70d56f9ee430808b440005cc3189f | 3706 |
| musetalk/utils/preprocessing.py | 02c2f8c54830521e468d3d22e988a2a9b0aa9738662d1eda45727fb46d1b045d | 7018 |
| musetalk/utils/training_utils.py | 90f678c9ba7cfcb4d7097608d0a35342e1513864013cc5957b50083c21e90e8c | 12167 |
| musetalk/utils/utils.py | 34c4cbc5771d9fa7d2f0d3c23f27298b79f7a356ec286387c42b36cfec038b0c | 11775 |
| musetalk/whisper/audio2feature.py | 8a8ec3a934ef59484417ba64d95c3de6d72d9a49eb07fb3f68d67a740faacf76 | 4859 |
| musetalk/whisper/whisper/__init__.py | 206da24046aea7ed799a11e210ecb9a0a7cdbf6f1dd5a4c4b98319d2aedcc586 | 5442 |
| musetalk/whisper/whisper/__main__.py | 260236595af0a0df76a5121229593502d46b8dcc4acc16eb6b64b2b62be2f278 | 36 |
| musetalk/whisper/whisper/assets/gpt2/merges.txt | fe36cab26d4f4421ed725e10a2e9ddb7f799449c603a96e7f29b5a3c82a95862 | 456356 |
| musetalk/whisper/whisper/assets/gpt2/special_tokens_map.json | c0b3c279b6ecdb71996a86ffb4d4ab94dfdb5df95f00bac9515688faef2ff5dd | 90 |
| musetalk/whisper/whisper/assets/gpt2/tokenizer_config.json | 1e664fe1a5bc404dcb204a76c76bc4100c91bd12c66098d8a8090b11b6ee6f0d | 236 |
| musetalk/whisper/whisper/assets/gpt2/vocab.json | 3ba3c3109ff33976c4bd966589c11ee14fcaa1f4c9e5e154c2ed7f99d80709e7 | 798156 |
| musetalk/whisper/whisper/assets/mel_filters.npz | dd2cc75e70e36fcbdd8ffbc2499062f30094093e6bf2cbafa9859f59972b420b | 2048 |
| musetalk/whisper/whisper/assets/multilingual/added_tokens.json | f1acda6b99dd830d4ad39012259cd256d7e44cb2e3e4836b9804ac3f8e6805b9 | 25 |
| musetalk/whisper/whisper/assets/multilingual/merges.txt | 9ec394d4060fee3cdbf52479ed021ed8bf7a4662d06a10ee45d8bbe95d267c45 | 493855 |
| musetalk/whisper/whisper/assets/multilingual/special_tokens_map.json | c0b3c279b6ecdb71996a86ffb4d4ab94dfdb5df95f00bac9515688faef2ff5dd | 90 |
| musetalk/whisper/whisper/assets/multilingual/tokenizer_config.json | b4594fd40712d072d4d637a42a8fb92abdb65c094dd5cb8d1da8d5cebb7b3473 | 604 |
| musetalk/whisper/whisper/assets/multilingual/vocab.json | 4e8036c8f80442384c12c2eaceb2f1c25d3ee38b8b07584bae6e74fe75ba4de4 | 936041 |
| musetalk/whisper/whisper/audio.py | bc56ce6dd84c10de96db5400448f46afa384408e5f3132a7221ebca273837a08 | 4071 |
| musetalk/whisper/whisper/decoding.py | f97702a1257580765d0be41c43dbc433d5a983a0d66082b18b8e3b63e4dad53c | 30930 |
| musetalk/whisper/whisper/model.py | 93c0671d35aace5c97278232258345176ddac95b293de456d50babbb97120d0b | 10591 |
| musetalk/whisper/whisper/normalizers/__init__.py | 678cbbf410bb76fcfe7edf90c6c26242c80c55e280bc831a1893b86999eeba2a | 82 |
| musetalk/whisper/whisper/normalizers/basic.py | d75a266f48f8e3a2a1e9df0fa6fee2859818e1991fe6d18c5f0c5ba98851c927 | 1882 |
| musetalk/whisper/whisper/normalizers/english.json | d67f3595953324659e454e354496509144c45ff6ac105943c98bb2bd7fb55292 | 56145 |
| musetalk/whisper/whisper/normalizers/english.py | d2944d4db1221a7ee3db453ae5597310bc01d45e683764c57476f514e25f4651 | 20793 |
| musetalk/whisper/whisper/tokenizer.py | ade4fa8792a1eed6e50ec6d2079a3e61c7df67a1d358d14c6d40db70fe57443f | 9837 |
| musetalk/whisper/whisper/transcribe.py | eb832d8873d16c0c4a28d9869d7b5062da9ccdaf9b2a509b02c9f5e13f4f91fe | 10580 |
| musetalk/whisper/whisper/utils.py | 51396c6da293a3d8276f0cbdcf97f38ce1ec7bdd8198923f6b0737ea72927406 | 2660 |
| requirements.txt | f02913b01a30f0c1b5fd54b0f042852ec10167b655441c911fd9d3a9fb9fb427 | 287 |
| scripts/__init__.py | 01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b | 1 |
| scripts/inference.py | 5609d5597766429b1a0013cacc80c96d02ce45e5ad78a915a99835f36e6dd6a7 | 13662 |
| scripts/preprocess.py | eaee2b38febfbe26974fd036dc217b0c590ea56d0e5710b948bccd90e362432f | 12463 |
| scripts/realtime_inference.py | 1265e2d03de7011bd97af78855012e03b45378ae2ae855800bdaf055373d7c23 | 19478 |
| test_ffmpeg.py | e87b9d495bae06c5011d936ff31442cccfbb0a57906befd21f6260412d1cbf5c | 1085 |
| train.py | 0b9e0446fbcb80d805ce50d0cbb5ac9001058ea478773e48af69e0a9d662431a | 26013 |
| train.sh | 6ce9774c099a7090f78db01cca9fa8584bda3afb5ae87d8d0e5624e77c51d427 | 1141 |
