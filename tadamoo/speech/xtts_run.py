"""Runs XTTS v2 on a list of jobs with the sampling parameters and the seed under our control (the voice service takes neither). Run with the XTTS venv's Python:
    C:\\Projects\\callAPIcall\\tts-venvs\\xtts\\Scripts\\python.exe xtts_run.py job.json
job.json: {"model_dir", "speaker", "out_dir", "items": [{"id", "text", "lang", "seed", "temperature", "top_p", "top_k", "repetition_penalty", "length_penalty", "speed"}]}
Each item becomes out_dir/<id>.wav (24 kHz mono 16-bit); a line per item is printed as JSON."""
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import torch

job = json.load(open(sys.argv[1], encoding="utf-8"))
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts

model_dir = job["model_dir"]
cfg = XttsConfig()
cfg.load_json(os.path.join(model_dir, "config.json"))
m = Xtts.init_from_config(cfg)
m.load_checkpoint(cfg, checkpoint_dir=model_dir, eval=True)
m.to("cuda")
speakers = torch.load(os.path.join(model_dir, "speakers_xtts.pth"), map_location="cpu", weights_only=False)
sp = speakers[job["speaker"]]
gpt, emb = sp["gpt_cond_latent"].to("cuda"), sp["speaker_embedding"].to("cuda")
os.makedirs(job["out_dir"], exist_ok=True)
for it in job["items"]:
    t0 = time.time()
    torch.manual_seed(int(it["seed"]))
    np.random.seed(int(it["seed"]) % (2 ** 32))
    secs = len(it["text"]) / 12.0
    with torch.inference_mode():
        out = m.inference(text=it["text"], language=it.get("lang", "hu"), gpt_cond_latent=gpt, speaker_embedding=emb, temperature=float(it.get("temperature", 0.75)),
                          top_p=float(it.get("top_p", 0.85)), top_k=int(it.get("top_k", 50)), repetition_penalty=float(it.get("repetition_penalty", 5.0)),
                          length_penalty=float(it.get("length_penalty", 1.0)), speed=float(it.get("speed", 1.0)), enable_text_splitting=False,
                          max_new_tokens=max(40, int((secs * 2.0 + 1.0) * 21.5)))
    wav = np.asarray(out["wav"], dtype=np.float32)
    path = os.path.join(job["out_dir"], f"{it['id']}.wav")
    sf.write(path, wav, 24000, subtype="PCM_16")
    print(json.dumps({"id": it["id"], "seconds": round(len(wav) / 24000, 3), "wall": round(time.time() - t0, 2)}), flush=True)
