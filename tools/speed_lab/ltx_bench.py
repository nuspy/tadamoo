"""Runs LTX Studio's `ltx2_infer.py` (LTX 2.5 audio-to-video) with one attention backend, one compile mode and one cache chosen from the environment, and prints what it did.

    python ltx_bench.py ltx2_infer.py PARAMS.json

Environment (all optional; unset = what LTX Studio runs today):
    SL_ATTN      sdpa (LTX default order) | cudnn | sdpa_flash | efficient | fa4 | fa2 | sage1 | sage2 | sage3 | xformers | flashinfer
    SL_COMPILE   none | default | reduce-overhead | max-autotune | capture   (ltx_core CompilationConfig)
    SL_GUIDER    '' (the worker's: cfg 3, stg 1, modality 3 = 4 passes) | cfg (2 passes) | one (1 pass)
    SL_CACHE     none | fb:<thresh> | mag:<thresh>                            (own first-block / residual caches, see caches.py)
    LTX_X264_CRF, LTX_X264_PRESET   the saved quality (as tadamoo/media/ltx_launch.py)
It runs the worker in this process, so the patches below are the only difference from a normal job. Stage times and the peak VRAM of the process are printed as `[SL] ...` lines."""
from __future__ import annotations

import atexit
import os
import sys
import time
from pathlib import Path

OLD = '"crf": "19", "preset": "veryfast"'
HERE = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"[SL] {msg}", flush=True)


def qkv(q, k, v, heads):
    b, _, inner = q.shape
    d = inner // heads
    return b, d, *(t.view(b, -1, heads, d) for t in (q, k, v))          # (b, n, h, d)


def make_attention(name: str):
    """A callable (q, k, v, heads) -> out on (b, n, heads*dim) tensors, as ltx_core's AttentionCallable."""
    import torch
    from ltx_core.model.transformer import attention as A
    enum = {"cudnn": "SDPA_CUDNN", "sdpa_flash": "SDPA_FLASH", "efficient": "SDPA_EFFICIENT", "fa4": "FLASH_ATTENTION_4"}
    if name in enum:
        return getattr(A.AttentionFunction, enum[name]).to_callable()
    if name == "sdpa":
        return A._sdpa_full_priority()
    if name == "fa2":
        from flash_attn import flash_attn_func
        def fn(q, k, v, heads):
            b, d, q, k, v = qkv(q, k, v, heads)
            return flash_attn_func(q, k, v).reshape(b, -1, heads * d)
    elif name == "xformers":
        import xformers.ops as xo
        def fn(q, k, v, heads):
            b, d, q, k, v = qkv(q, k, v, heads)
            return xo.memory_efficient_attention(q, k, v).reshape(b, -1, heads * d)
    elif name in ("sage1", "sage2"):
        from sageattention import sageattn
        def fn(q, k, v, heads):
            b, d, q, k, v = qkv(q, k, v, heads)
            return sageattn(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), tensor_layout="HND").transpose(1, 2).reshape(b, -1, heads * d)
    elif name == "sage3":
        from sageattn3 import sageattn3_blackwell
        def fn(q, k, v, heads):
            b, d, q, k, v = qkv(q, k, v, heads)
            return sageattn3_blackwell(q.transpose(1, 2).contiguous(), k.transpose(1, 2).contiguous(), v.transpose(1, 2).contiguous()).transpose(1, 2).reshape(b, -1, heads * d)
    elif name == "flashinfer":
        import flashinfer
        def fn(q, k, v, heads):
            b, d, q, k, v = qkv(q, k, v, heads)
            outs = [flashinfer.single_prefill_with_kv_cache(q[i].contiguous(), k[i].contiguous(), v[i].contiguous(), causal=False) for i in range(b)]
            return torch.stack(outs).reshape(b, -1, heads * d)
    else:
        raise SystemExit(f"[SL] unknown SL_ATTN {name}")
    fn.label = name
    return fn


def install_attention(name: str) -> None:
    """Every Attention module of every transformer a stage builds gets the chosen callable (and the calls are counted)."""
    import atexit
    from ltx_core.model.transformer.attention import Attention
    from ltx_pipelines.utils.blocks import DiffusionStage
    call = make_attention(name)
    calls = {"n": 0, "modules": 0}

    def counted(q, k, v, heads, *rest, **kw):
        calls["n"] += 1
        return call(q, k, v, heads, *rest, **kw)
    counted.label = getattr(call, "label", name)
    build = DiffusionStage._build_transformer

    def build_with(self, *a, **kw):
        model = build(self, *a, **kw)
        for module in model.modules():
            if isinstance(module, Attention):
                module.attention_function = counted
                calls["modules"] += 1
        return model
    DiffusionStage._build_transformer = build_with
    atexit.register(lambda: log(f"attention {name}: {calls['modules']} modules set, {calls['n']} calls"))
    log(f"attention {name} -> {counted.label}")


def install_compile(mode: str) -> None:
    from ltx_core.model.transformer.compiling import CompilationConfig
    from ltx_pipelines import a2vid_two_stage as M
    cfg = {"default": CompilationConfig(), "reduce-overhead": CompilationConfig(mode="reduce-overhead"),
           "max-autotune": CompilationConfig(mode="max-autotune-no-cudagraphs"), "capture": CompilationConfig(capture=True)}[mode]
    original = M.A2VidPipelineTwoStage.__init__

    def init(self, *a, **kw):
        kw.setdefault("compilation_config", cfg)
        return original(self, *a, **kw)
    M.A2VidPipelineTwoStage.__init__ = init
    log(f"compile {mode}")


def install_cache(spec: str) -> None:
    sys.path.insert(0, str(HERE))
    import caches
    caches.install(spec, log)


def install_timers() -> None:
    import torch
    from ltx_pipelines.utils.blocks import DiffusionStage
    original = DiffusionStage.__call__
    times = []

    def timed(self, *a, **kw):
        torch.cuda.synchronize()
        t = time.time()
        out = original(self, *a, **kw)
        torch.cuda.synchronize()
        times.append(round(time.time() - t, 2))
        log(f"stage {len(times)} (denoise incl. load) {times[-1]} s")
        return out
    DiffusionStage.__call__ = timed
    from ltx_core.model.transformer.model import X0Model
    forward = X0Model.forward
    acc = {"n": 0, "s": 0.0}

    def timed_forward(self, *a, **kw):
        torch.cuda.synchronize()
        t = time.time()
        out = forward(self, *a, **kw)
        torch.cuda.synchronize()
        acc["n"] += 1
        acc["s"] += time.time() - t
        return out
    X0Model.forward = timed_forward
    build = DiffusionStage._build_transformer
    built = {"n": 0, "s": 0.0}

    def timed_build(self, *a, **kw):
        torch.cuda.synchronize()
        t = time.time()
        out = build(self, *a, **kw)
        torch.cuda.synchronize()
        built["n"] += 1
        built["s"] += time.time() - t
        return out
    DiffusionStage._build_transformer = timed_build

    def report():
        log(f"transformer_forwards {acc['n']} total {acc['s']:.2f} s")
        log(f"transformer_builds {built['n']} total {built['s']:.2f} s")
        try:
            log(f"peak_allocated_gib {torch.cuda.max_memory_allocated() / 2**30:.2f} peak_reserved_gib {torch.cuda.max_memory_reserved() / 2**30:.2f}")
        except Exception:
            pass
    atexit.register(report)


def main() -> int:
    script = Path(sys.argv[1])
    source = script.read_text(encoding="utf-8")
    crf, preset = os.environ.get("LTX_X264_CRF", ""), os.environ.get("LTX_X264_PRESET", "")
    if crf and OLD in source:
        source = source.replace(OLD, f'"crf": "{crf}", "preset": "{preset}"')
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    sys.path.insert(0, str(script.parent.parent))                         # `core` of LTX Studio
    import torch
    try:
        import triton
        tv = getattr(triton, "__version__", "-")
    except ImportError:
        tv = "none"
    log(f"torch {torch.__version__} triton {tv}")
    real_inference_mode = torch.inference_mode                           # the worker replaces it with no_grad, which torch.compile's own code cannot use
    code = compile(source, str(script), "exec")
    glb = {"__name__": "ltx_bench_worker", "__file__": str(script), "__builtins__": __builtins__}
    exec(code, glb)                                                       # defines the worker without running main()
    attn, comp, cache = os.environ.get("SL_ATTN", ""), os.environ.get("SL_COMPILE", "none"), os.environ.get("SL_CACHE", "none")
    install_timers()
    if attn:
        install_attention(attn)
    else:
        from ltx_core.model.transformer import attention as A
        log(f"attention default -> {A.attention_label(A.automatic_attention())}")
    if comp != "none":
        torch.inference_mode = real_inference_mode
        install_compile(comp)
    if cache != "none":
        install_cache(cache)
    guider = os.environ.get("SL_GUIDER", "")
    if guider:
        over = {"one": dict(cfg_scale=1.0, stg_scale=0.0, modality_scale=1.0), "cfg": dict(stg_scale=0.0, modality_scale=1.0)}[guider]
        params_cls = glb["MultiModalGuiderParams"]
        glb["MultiModalGuiderParams"] = lambda **kw: params_cls(**{**kw, **over})
        log(f"guider {guider}: {over} (video guider only; the audio one is built the same way)")
    glb["main"]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
