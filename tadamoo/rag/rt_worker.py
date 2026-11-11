"""Starts the embedding worker for the CPU: `python rt_worker.py <threads> [bf16|fp32]`. For the real-time question embedder, which shares the CPU with the
engine: a limited number of threads, and float32 weights unless told otherwise (a CPU without native bfloat16 is several times slower with it). The worker
itself is `embed_worker.py`, run as it is."""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    threads = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    import torch
    if threads > 0:
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
    if (sys.argv[2] if len(sys.argv) > 2 else "fp32") == "fp32":
        from transformers import AutoModel
        load = AutoModel.from_pretrained                    # the worker asks for bfloat16: ask for float32 instead
        AutoModel.from_pretrained = staticmethod(lambda *a, **k: load(*a, **{**{x: y for x, y in k.items() if x not in ("dtype", "torch_dtype")}, "dtype": torch.float32}))
    runpy.run_path(str(Path(__file__).with_name("embed_worker.py")), run_name="__main__")
