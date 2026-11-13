"""Runs XTTS v2 on lists of jobs with the sampling parameters and the seed under our control (the voice service takes neither). Run with the XTTS venv's Python:
    C:\\Projects\\callAPIcall\\tts-venvs\\xtts\\Scripts\\python.exe xtts_run.py job.json [job2.json ...]
    C:\\Projects\\callAPIcall\\tts-venvs\\xtts\\Scripts\\python.exe xtts_run.py --serve        (the path of a job file on each line of the standard input)
job.json: {"model_dir", "speaker", "out_dir", "items": [{"id", "text", "lang", "seed", "temperature", "top_p", "top_k", "repetition_penalty", "length_penalty", "speed"}]}
Each item becomes out_dir/<id>.wav (24 kHz mono 16-bit); a line per item is printed as JSON, and a line {"batch": "done"} when a job file is finished. The model is loaded for the first
job and stays loaded for the next ones: loading it takes longer than reading a short sentence."""
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import torch

LOADED = {}


def load(job):
    key = (job["model_dir"], job["speaker"])
    if LOADED.get("key") != key:
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
        LOADED.update(key=key, model=m, gpt=sp["gpt_cond_latent"].to("cuda"), emb=sp["speaker_embedding"].to("cuda"))
    return LOADED["model"], LOADED["gpt"], LOADED["emb"]


def run(path):
    job = json.load(open(path, encoding="utf-8"))
    m, gpt, emb = load(job)
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
        sf.write(os.path.join(job["out_dir"], f"{it['id']}.wav"), wav, 24000, subtype="PCM_16")
        print(json.dumps({"id": it["id"], "seconds": round(len(wav) / 24000, 3), "wall": round(time.time() - t0, 2)}), flush=True)
    print(json.dumps({"batch": "done"}), flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--serve"]:
        for line in sys.stdin:
            if line.strip():
                run(line.strip())
    else:
        for job_path in sys.argv[1:]:
            run(job_path)
