#!/usr/bin/env python3
"""Perplexity and KL divergence through the decode path, against a llama.cpp base.

The base is `llama-perplexity --kl-divergence-base`'s file (wikitext-2, 512-token chunks, second half scored). The
engine runs as `--serve` with the model plus --ppl-base and --ppl-out; per chunk it reads the first half as a prompt
and teacher-forces the rest through verify windows of T tokens (see the engine's ppl_gate.hpp). Flags this tool does
not know are passed to the engine as they are; it sets only what the measurement needs (--spec 5,
no adaptive swaps, the first two visible GPUs when there are two: --gpu 0,1), and an engine flag given here replaces
that value.

  python3 tools/ppl_gate.py --model FILE.basalt --base base.kld --out DIR [--engine-bin BIN] [--chunks 20] [--t 3]
                            [--gpu 0 ...]
  python3 tools/ppl_gate.py --report DIR/rows.tsv          # summary of a finished run
  python3 tools/ppl_gate.py --diff A/rows.tsv B/rows.tsv   # row by row: identical rows, largest NLL/KLD change

A base written by this engine instead of llama.cpp (a later run against it measures the shift from this build with the
flags given here, not the quality in absolute terms; rewrite it when the package, the build or a flag changes):

  python3 tools/ppl_gate.py --write-base --text FILE.txt --model FILE.basalt --base base.kld --out DIR [--chunks 20]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

ENGINE_BIN = Path(__file__).resolve().parents[1] / "bin" / "basalt"
N_CTX = 512
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def row_bytes(n_vocab: int) -> int:
    """One base row: scale and min (two f32), then n_vocab u16 padded to an even count."""
    return 2 * (2 * ((n_vocab + 1) // 2) + 4)


def base_size(n_ctx: int, n_vocab: int, n_chunk: int) -> int:
    return 20 + n_chunk * n_ctx * 4 + n_chunk * (n_ctx - 1 - n_ctx // 2) * row_bytes(n_vocab)


def write_base_file(path: str, chunks: list[list[int]], n_ctx: int, n_vocab: int) -> int:
    """The base's header and tokens, with the space of its rows reserved (the engine writes them). Returns the size."""
    if any(len(c) != n_ctx for c in chunks):
        raise ValueError("every chunk needs exactly n_ctx tokens")
    with open(path, "xb") as f:                                   # never over an existing base
        f.write(b"_logits_" + struct.pack("<Iii", n_ctx, n_vocab, len(chunks)))
        for c in chunks:
            f.write(struct.pack(f"<{n_ctx}i", *c))
        size = base_size(n_ctx, n_vocab, len(chunks))
        f.truncate(size)
    return size


def tokenize_text(model: str, text: str) -> tuple[list[int], int]:
    """The token ids of `text` with the model file's own tokenizer, and the model's vocabulary size."""
    from model_file import ModelFile
    from serve.engine import modelfile
    mf = ModelFile(model)
    ids = modelfile.build_tokenizer(modelfile.tokenizer_files(mf)).encode(text)
    shape = ((json.loads(mf.read("meta")).get("tensors") or {}).get("output.weight") or {}).get("shape") or []
    if not shape:
        sys.exit(f"{model}: the meta section has no output.weight shape (the vocabulary size)")
    return ids, int(shape[-1])


def read_base(path: str):
    with open(path, "rb") as f:
        if f.read(8) != b"_logits_":
            sys.exit(f"{path}: not a llama.cpp KL-divergence base")
        n_ctx, n_vocab, n_chunk = struct.unpack("<Iii", f.read(12))
        toks = struct.unpack(f"<{n_ctx * n_chunk}i", f.read(4 * n_ctx * n_chunk))
    return n_ctx, n_vocab, n_chunk, [toks[k * n_ctx:(k + 1) * n_ctx] for k in range(n_chunk)]


def paired_se(by_chunk: dict[int, list[float]]) -> float:
    """The standard error of the mean of nll - nll_base, taken over the chunk means (the rows of a chunk are not independent)."""
    means = [sum(v) / len(v) for v in by_chunk.values() if v]
    if len(means) < 2:
        return float("nan")
    m = sum(means) / len(means)
    return math.sqrt(sum((x - m) ** 2 for x in means) / (len(means) - 1) / len(means))


def summary(rows_path: str, extra: dict | None = None) -> str:
    nll, nllb, kld, same, dlp = [], [], [], 0, []
    chunks = set()
    by_chunk: dict[int, list[float]] = {}
    for line in open(rows_path):
        f = line.split("\t")
        if len(f) < 10:
            continue
        chunks.add(int(f[0]))
        nll.append(float(f[3])); nllb.append(float(f[4])); kld.append(float(f[5]))
        by_chunk.setdefault(int(f[0]), []).append(nll[-1] - nllb[-1])
        same += int(f[6]); dlp.append(float(f[9]))
    n = len(nll)
    if n == 0:
        return "no rows"
    ks = sorted(kld)
    pct = lambda q: ks[min(n - 1, int(round(q * (n - 1))))]
    mn = sum(nll) / n
    se = math.sqrt(max(0.0, sum(v * v for v in nll) / n - mn * mn) / max(1, n - 1))
    out = [f"chunks {len(chunks)}  rows {n}",
           f"PPL {math.exp(mn):.4f} +/- {math.exp(mn) * se:.4f}   base PPL {math.exp(sum(nllb) / n):.4f}   "
           f"ln(PPL/base) {mn - sum(nllb) / n:.5f} +/- {paired_se(by_chunk):.5f} (paired, over chunks)",
           f"KLD mean {sum(kld) / n:.5f}  median {pct(0.5):.5f}  p99 {pct(0.99):.4f}  max {ks[-1]:.4f}",
           f"same top-1 {100.0 * same / n:.2f}%   mean |dlogp| of the base top-1 {sum(dlp) / n:.5f}"]
    if extra:
        out.append("  ".join(f"{k} {v}" for k, v in extra.items()))
    return "\n".join(out)


def diff(pa: str, pb: str) -> str:
    def load(p):
        return {(f[0], f[1]): f for f in (l.rstrip("\n").split("\t") for l in open(p)) if len(f) >= 10}
    a, b = load(pa), load(pb)
    keys = sorted(set(a) & set(b), key=lambda k: (int(k[0]), int(k[1])))
    if not keys:
        return "no common rows"
    same = sum(a[k][3:] == b[k][3:] for k in keys)
    top = sum(a[k][7] == b[k][7] for k in keys)
    dn = max(abs(float(a[k][3]) - float(b[k][3])) for k in keys)
    dk = max(abs(float(a[k][5]) - float(b[k][5])) for k in keys)
    first = next((k for k in keys if a[k][3:] != b[k][3:]), None)
    return (f"rows {len(keys)}: identical {same} ({100.0 * same / len(keys):.2f}%), same top-1 {top}, "
            f"max |dNLL| {dn:.6f}, max |dKLD| {dk:.6f}, first difference "
            f"{'none' if first is None else 'chunk %s pos %s' % first}")


def gpu_count() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--list-gpus"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if line.startswith("GPU "))


def set_args(args: list[str], extra: list[str]) -> list[str]:
    """`args` with each flag of `extra` replacing the same flag (and its value) in `args`, or added."""
    out = list(args)
    i = 0
    while i < len(extra):
        flag = extra[i]
        has_value = i + 1 < len(extra) and not extra[i + 1].startswith("--")
        value = [extra[i + 1]] if has_value else []
        if flag in out:
            j = out.index(flag)
            old_has_value = j + 1 < len(out) and not out[j + 1].startswith("--")
            out[j:j + 1 + old_has_value] = [flag, *value]
        else:
            out += [flag, *value]
        i += 1 + has_value
    return out


def run(a) -> int:
    n_ctx, n_vocab, n_chunk, chunks = read_base(a.base)
    first = n_ctx // 2
    args = ["--model", str(Path(a.model).resolve()), "--spec", "5",
            "--adapt-swaps", "0", "--peer-adapt-swaps", "0", "--max-context", "4096"]
    if gpu_count() > 1:
        args += ["--gpu", "0,1"]
    args = set_args(args, a.engine_args + ["--ppl-base", str(Path(a.base).resolve())]
                    + (["--ppl-write-base"] if a.write_base else []))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = out / "rows.tsv"
    rows.unlink(missing_ok=True)
    args = set_args(args, ["--ppl-out", str(rows)])
    log = open(out / "engine.log", "w")
    log.write(f"ppl_gate: engine={a.engine_bin} T={a.t} chunks={a.chunks} args={' '.join(args)}\n")
    log.flush()
    t0 = time.time()
    eng = subprocess.Popen([a.engine_bin, "--serve", *args], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
    try:
        for line in eng.stdout:
            if line.startswith("READY"):
                break
        else:
            print("the engine exited before READY; see", out / "engine.log")
            return 1
        t_load = time.time() - t0
        hits = look = off = 0
        pp_ms = dec_ms = 0.0
        nc = min(a.chunks, n_chunk) if a.chunks > 0 else n_chunk
        t1 = time.time()
        for k in range(nc):
            ids = ",".join(str(t) for t in chunks[k][:first + 1])
            eng.stdin.write(f"GEN {n_ctx - 1 - first} ppl={k} ppl_t={a.t} {ids}\n")
            eng.stdin.flush()
            for line in eng.stdout:
                if line.startswith("ERR"):
                    print(f"chunk {k}: {line.strip()}")
                    return 1
                if line.startswith("DONE"):
                    f = line.split()
                    pp_ms += float(f[3]); dec_ms += float(f[4])
                    if f[8] != "0":
                        print(f"chunk {k}: {f[8]} prompt tokens reused from an earlier request")
                    if len(f) > 10:
                        hits += int(f[9]); look += int(f[10])
                    if len(f) > 15:
                        off += int(f[15])
                    break
            else:
                print(f"chunk {k}: the engine exited; see", out / "engine.log")
                return 1
            if a.progress and (k + 1) % a.progress == 0:
                print(f"  {k + 1}/{nc} chunks, {time.time() - t1:.0f} s", flush=True)
        t_run = time.time() - t1
        eng.stdin.write("QUIT\n")
        eng.stdin.flush()
    finally:
        try:
            eng.wait(timeout=60)
        except subprocess.TimeoutExpired:
            eng.kill()
    extra = {"T": a.t, "load s": f"{t_load:.0f}", "run s": f"{t_run:.1f}",
             "prompt ms/chunk": f"{pp_ms / nc:.0f}", "decode ms/chunk": f"{dec_ms / nc:.0f}",
             "decode routed experts": f"{look + off}: VRAM hits {100.0 * hits / max(1, look + off):.1f}%, "
                                      f"peer GPU/PCIe {100.0 * off / max(1, look + off):.1f}%, "
                                      f"CPU {100.0 * (look - hits) / max(1, look + off):.1f}%"}
    rep = summary(str(rows), extra)
    (out / "report.txt").write_text(f"engine {a.engine_bin}\nargs {' '.join(args)}\n{rep}\n")
    print(rep)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", help="the .basalt file")
    ap.add_argument("--engine-bin", default=str(ENGINE_BIN) if ENGINE_BIN.is_file() else None,
                    help="the engine binary (default: bin/basalt in the repo)")
    ap.add_argument("--base")
    ap.add_argument("--out")
    ap.add_argument("--chunks", type=int, default=0, help="the first N chunks (0: all)")
    ap.add_argument("--t", type=int, default=3, help="tokens per verify window")
    ap.add_argument("--progress", type=int, default=10, help="a progress line every N chunks (0: none)")
    ap.add_argument("--write-base", action="store_true",
                    help="write --base from --text with this engine (run once; refuses an existing file), then measure")
    ap.add_argument("--text", help="with --write-base: the plain text the chunks are cut from")
    ap.add_argument("--report", help="print the summary of an existing rows.tsv and exit")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"), help="compare two rows.tsv files row by row and exit")
    a, extra = ap.parse_known_args()
    a.engine_args = extra
    if a.diff:
        print(diff(*a.diff))
        return 0
    if a.report:
        print(summary(a.report))
        return 0
    if not (a.model and a.base and a.out):
        ap.error("--model, --base and --out are required")
    if not a.engine_bin:
        ap.error("no engine binary: pass --engine-bin")
    if a.write_base:
        if not a.text:
            ap.error("--write-base needs --text")
        final = a.base
        partial = final + ".partial"                      # renamed to `final` only when every chunk was written
        if Path(final).exists() or Path(partial).exists():
            ap.error(f"{final} or {partial} exists: a base is written once")
        ids, n_vocab = tokenize_text(a.model, Path(a.text).read_text(encoding="utf-8"))
        n_chunk = len(ids) // N_CTX
        if a.chunks > 0:
            n_chunk = min(n_chunk, a.chunks)
        if n_chunk < 1:
            ap.error(f"the text gives {len(ids)} tokens, fewer than one chunk of {N_CTX}")
        size = write_base_file(partial, [ids[k * N_CTX:(k + 1) * N_CTX] for k in range(n_chunk)], N_CTX, n_vocab)
        print(f"base {final}: {n_chunk} chunks of {N_CTX}, vocabulary {n_vocab}, {size / 2**30:.2f} GiB reserved")
        a.base = partial
        a.chunks = n_chunk
        code = run(a)
        if code == 0:
            os.replace(partial, final)
        else:
            print(f"the run did not finish: {partial} is incomplete and is not a base")
        return code
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
