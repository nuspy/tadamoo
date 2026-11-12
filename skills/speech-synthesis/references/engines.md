# Engines, and what leaves the PC

| Engine | Where it runs | Needs | Leaves the PC |
|---|---|---|---|
| XTTS v2 (clause voice) | here, its own Python (`tts-venvs/xtts`) | the model folder; ~3 GB VRAM | nothing |
| XTTS v2 (voice service `/tts/test`) | here, the voice service | the service up | nothing |
| Piper, MMS | here | their models | nothing |
| Edge neural voices (hu-HU-NoemiNeural, TamasNeural), Azure, Google Cloud, ElevenLabs | the cloud | the person's OK and a key (Azure, Google, ElevenLabs) | the text of the script (one news item) |

No cloud engine is implemented. The `tts_engine` setting of a production is where one would be named; it would be refused unless the person allowed it and the key is present, and it would go through the same
normaliser and the same read-back and judging. Nothing in this skill calls a cloud service.
