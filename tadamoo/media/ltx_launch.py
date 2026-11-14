"""Runs LTX Studio's `ltx2_infer.py` with the attention of the transformer and the quality of the saved video set from outside.

    python ltx_launch.py ltx2_infer.py PARAMS.json            with LTX_ATTENTION, LTX_X264_CRF and LTX_X264_PRESET in the environment

LTX Studio's worker leaves two things to its own defaults that matter here, and has no parameter for either:
  - The attention of the transformer: PyTorch's cuDNN SDPA. `LTX_ATTENTION` names another (`sage2`, `sage1`, `efficient`, `fa4`, `fa2`, `xformers`; `cudnn` or nothing: the worker's own): the attention of every
    Attention module the stages build is replaced by that kernel. SageAttention 2 is the default of the product (settings `ltx_attention`); a kernel whose package cannot be imported is a plain warning on the
    output and the clip is made with cuDNN.
  - The saved quality: PyAV's x264 at `crf 19, preset veryfast`, which smears the fine detail of a face. `LTX_X264_CRF` and `LTX_X264_PRESET` replace that one setting in the worker's source before it runs; if the
    setting is not found (the worker changed) the launcher stops and says so instead of saving at the old quality.
(TeaCache needs no launcher: it is the worker's own parameter `teacache_thresh`.) Everything else of the worker is as it is: it is run in this process. Standard library at import; the packages when the worker needs them."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

OLD = '"crf": "19", "preset": "veryfast"'
ATTENTIONS = ("sage2", "sage1", "efficient", "fa4", "fa2", "xformers")


def log(message: str) -> None:
    print(f"[attention] {message}", flush=True)


def qkv(q: Any, k: Any, v: Any, heads: int) -> tuple:
    b, _, inner = q.shape
    d = inner // heads
    return (b, d) + tuple(t.view(b, -1, heads, d) for t in (q, k, v))          # (b, n, heads, d)


def make_attention(name: str) -> Callable:
    """A callable `(q, k, v, heads) -> out` on (batch, tokens, heads x dim) tensors, as ltx_core's attention functions are, for the kernel `name`. Raises ImportError when its package is not there."""
    if name == "efficient":
        from ltx_core.model.transformer import attention as A
        return A.AttentionFunction.SDPA_EFFICIENT.to_callable()
    if name == "fa4":
        from ltx_core.model.transformer import attention as A
        return A.AttentionFunction.FLASH_ATTENTION_4.to_callable()
    if name in ("sage1", "sage2"):
        from sageattention import sageattn

        def sage(q, k, v, heads, *rest, **kw):
            b, d, q, k, v = qkv(q, k, v, heads)
            return sageattn(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), tensor_layout="HND").transpose(1, 2).reshape(b, -1, heads * d)
        return sage
    if name == "fa2":
        from flash_attn import flash_attn_func

        def fa2(q, k, v, heads, *rest, **kw):
            b, d, q, k, v = qkv(q, k, v, heads)
            return flash_attn_func(q, k, v).reshape(b, -1, heads * d)
        return fa2
    if name == "xformers":
        import xformers.ops as xo

        def xf(q, k, v, heads, *rest, **kw):
            b, d, q, k, v = qkv(q, k, v, heads)
            return xo.memory_efficient_attention(q, k, v).reshape(b, -1, heads * d)
        return xf
    raise ImportError(f"no attention named {name}")


def install_attention(name: str, make: Optional[Callable[[str], Callable]] = None) -> bool:
    """Every Attention module of every transformer a stage builds gets the kernel `name`. False (and a warning) when the kernel cannot be had: the worker then runs with its own attention."""
    try:
        call = (make or make_attention)(name)
        from ltx_core.model.transformer.attention import Attention
        from ltx_pipelines.utils.blocks import DiffusionStage
    except Exception as exc:                                           # a package that is not there or does not match this torch: the worker goes on with cuDNN
        log(f"WARNING: {name} could not be used ({type(exc).__name__}: {str(exc)[:160]}); this clip is made with cuDNN attention")
        return False
    build = DiffusionStage._build_transformer

    def build_with(self, *args, **kw):
        model = build(self, *args, **kw)
        for module in model.modules():
            if isinstance(module, Attention):
                module.attention_function = call
        return model
    DiffusionStage._build_transformer = build_with
    log(f"{name} for the attention of the transformer")
    return True


def main() -> int:
    script = Path(sys.argv[1])
    crf, preset = os.environ.get("LTX_X264_CRF", ""), os.environ.get("LTX_X264_PRESET", "")
    attention = os.environ.get("LTX_ATTENTION", "").strip().lower()
    source = script.read_text(encoding="utf-8")
    if crf or preset:
        if not (crf.isdigit() and preset.isalpha()):
            print("ERRORE: LTX_X264_CRF (a number) and LTX_X264_PRESET (a word) are needed together", flush=True)
            return 2
        if OLD not in source:
            print(f"ERRORE: the save setting {OLD} is not in {script.name}: the worker changed, the launcher has to follow", flush=True)
            return 2
        source = source.replace(OLD, f'"crf": "{crf}", "preset": "{preset}"')
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    sys.path.insert(0, str(script.parent.parent))                      # LTX Studio's `core`
    glb = {"__name__": "ltx_launch_worker", "__file__": str(script), "__builtins__": __builtins__}
    exec(compile(source, str(script), "exec"), glb)                    # defines the worker (a script that runs at once, as the tests' stand-in does, has run by now)
    entry = glb.get("main")
    if callable(entry):
        if attention in ATTENTIONS:
            install_attention(attention)
        entry()
    return 0


if __name__ == "__main__":
    sys.exit(main())
