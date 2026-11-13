"""Runs XTTS v2 on lists of jobs with the sampling parameters and the seed under our control (the voice service takes neither). Run with the XTTS venv's Python:
    C:\\Projects\\callAPIcall\\tts-venvs\\xtts\\Scripts\\python.exe xtts_run.py job.json [job2.json ...]
    C:\\Projects\\callAPIcall\\tts-venvs\\xtts\\Scripts\\python.exe xtts_run.py --serve        (the path of a job file on each line of the standard input)
job.json: {"model_dir", "speaker", "out_dir", "precision", "batch", "items": [{"id", "text", "lang", "seed", "temperature", "top_p", "top_k", "repetition_penalty", "length_penalty", "speed"}]}
Each item becomes out_dir/<id>.wav (24 kHz mono 16-bit); a line per item is printed as JSON, and a line {"batch": "done"} when a job file is finished. The model is loaded for the first
job and stays loaded for the next ones: loading it takes longer than reading a short sentence.

The items that share a text and sampling settings (the seeds of one sentence) are generated together: one autoregressive pass makes up to `batch` takes, because a decoding step is bound by
the launch of its many small kernels and not by its arithmetic, so a batch of four costs about what one does. The batch is seeded once, with the seed of its first item. Each take is then cut
at its own stop token and turned into audio by itself, as a single take is. `precision` is fp32 (default), fp16 or bf16: autocast over the autoregressive pass only, the vocoder stays fp32."""
import contextlib
import json
import os
import sys
import time

import numpy as np

LOADED = {}
SAMPLING = ("lang", "temperature", "top_p", "top_k", "repetition_penalty", "length_penalty", "speed")
DEFAULTS = {"lang": "hu", "temperature": 0.75, "top_p": 0.85, "top_k": 50, "repetition_penalty": 5.0, "length_penalty": 1.0, "speed": 1.0}


def group_items(items, batch):
    """The items that share a text and sampling settings, in their order, in groups of at most `batch`."""
    groups = {}
    for it in items:
        key = (it["text"],) + tuple(it.get(k, DEFAULTS[k]) for k in SAMPLING)
        groups.setdefault(key, []).append(it)
    size = max(1, int(batch))
    return [g[i:i + size] for g in groups.values() for i in range(0, len(g), size)]


def take_end(codes, stop):
    """The length of one generated sequence of a batch: up to and including its first stop token (the rest is padding that finished sequences are filled with), all of it when it has none."""
    for i, c in enumerate(codes):
        if c == stop:
            return i + 1
    return len(codes)


def load(job):
    import torch
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


def autocast(precision):
    import torch
    if precision in ("fp16", "bf16"):
        return torch.autocast("cuda", dtype=torch.float16 if precision == "fp16" else torch.bfloat16)
    return contextlib.nullcontext()


def takes(m, gpt, emb, items, precision):
    """The audio of each item of a group (one text, one set of sampling settings), made together; a group that does not fit in memory is made in two halves."""
    import torch
    try:
        return batch_takes(m, gpt, emb, items, precision)
    except torch.cuda.OutOfMemoryError:
        if len(items) == 1:
            raise
        torch.cuda.empty_cache()
        half = len(items) // 2
        return takes(m, gpt, emb, items[:half], precision) + takes(m, gpt, emb, items[half:], precision)


def batch_takes(m, gpt, emb, items, precision):
    import torch
    first = items[0]
    opt = {k: first.get(k, DEFAULTS[k]) for k in SAMPLING}
    torch.manual_seed(int(first["seed"]))
    np.random.seed(int(first["seed"]) % (2 ** 32))
    secs = len(first["text"]) / 12.0
    tokens = torch.IntTensor(m.tokenizer.encode(first["text"].strip().lower(), lang=str(opt["lang"]).split("-")[0])).unsqueeze(0).to("cuda")
    stop = m.gpt.stop_audio_token
    out = []
    with torch.inference_mode():
        with autocast(precision):
            codes = m.gpt.generate(cond_latents=gpt, text_inputs=tokens, do_sample=True, top_p=float(opt["top_p"]), top_k=int(opt["top_k"]), temperature=float(opt["temperature"]),
                                   num_return_sequences=len(items), num_beams=1, length_penalty=float(opt["length_penalty"]), repetition_penalty=float(opt["repetition_penalty"]),
                                   output_attentions=False, max_new_tokens=max(40, int((secs * 2.0 + 1.0) * 21.5)))
        rows = codes.tolist()
        scale = 1.0 / max(float(opt["speed"]), 0.05)
        for j in range(len(items)):
            row = codes[j:j + 1, :take_end(rows[j], stop)]
            latents = m.gpt(tokens, torch.tensor([tokens.shape[-1]], device="cuda"), row, torch.tensor([row.shape[-1] * m.gpt.code_stride_len], device="cuda"),
                            cond_latents=gpt, return_attentions=False, return_latent=True)
            if scale != 1.0:
                latents = torch.nn.functional.interpolate(latents.transpose(1, 2), scale_factor=scale, mode="linear").transpose(1, 2)
            out.append(np.asarray(m.hifigan_decoder(latents, g=emb).cpu().squeeze(), dtype=np.float32))
    return out


def run(path):
    import soundfile as sf
    job = json.load(open(path, encoding="utf-8"))
    m, gpt, emb = load(job)
    os.makedirs(job["out_dir"], exist_ok=True)
    for group in group_items(job["items"], job.get("batch", 12)):
        t0 = time.time()
        wavs = takes(m, gpt, emb, group, job.get("precision", "fp32"))
        wall = round(time.time() - t0, 2)
        for it, wav in zip(group, wavs):
            sf.write(os.path.join(job["out_dir"], f"{it['id']}.wav"), wav, 24000, subtype="PCM_16")
            print(json.dumps({"id": it["id"], "seconds": round(len(wav) / 24000, 3), "wall": wall, "batch": len(group)}), flush=True)
    print(json.dumps({"batch": "done"}), flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--serve"]:
        for line in sys.stdin:
            if line.strip():
                run(line.strip())
    else:
        for job_path in sys.argv[1:]:
            run(job_path)
