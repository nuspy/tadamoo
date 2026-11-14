"""Step caches for the LTX 2.5 transformer, applied to stage 1 only (the half-size denoising); stage 2 (3 steps) and the first and last step of stage 1 always run in full.

    fb:<t>    first-block cache: the first block runs, and when its output differs from the one of the last full step by less than t (mean relative L1) the other blocks are skipped
              and their last residual is added to the new first-block output
    tea:<t>   TeaCache-style: the relative change of the block input (the patchified latent after the first block's modulated input) is accumulated; under t the blocks are skipped
              and the last residual is reused

The guided passes of a step are separate calls of the transformer (cond, uncond, perturbed, modality); each keeps its own cache, found by the order of the call inside the step.
Patches `LTXModel._process_transformer_blocks`; reversible by process end."""
from __future__ import annotations

import dataclasses

import torch


def rel_l1(a: torch.Tensor, b: torch.Tensor) -> float:
    return float((a - b).abs().mean() / (b.abs().mean() + 1e-8))


def install(spec: str, log) -> None:
    mode, _, value = spec.partition(":")
    thr = float(value)
    import os
    steps = int(os.environ.get("SL_STEPS", "12"))
    passes = int(os.environ.get("SL_PASSES", "4"))
    from ltx_core.model.transformer.model import LTXModel
    original = LTXModel._process_transformer_blocks
    state = {"tokens": None, "sigma": None, "slot": 0, "step": 0, "slots": {}, "hits": 0, "calls": 0, "metrics": [], "trace": []}

    def sigma_of(video) -> float:
        return round(float(video.timesteps.max()), 6)

    def patched(self, video, audio, perturbations):
        if video is None or not self.transformer_blocks:
            return original(self, video, audio, perturbations)
        tokens = video.x.shape[1]
        if state["tokens"] is None:
            state["tokens"] = tokens                                  # stage 1 is the first stage seen
        if tokens != state["tokens"]:
            return original(self, video, audio, perturbations)       # stage 2: always in full
        k = state["calls"]                                           # stage 1 calls come in steps of `passes` guided passes each
        state.update(step=k // passes + 1, slot=k % passes)
        sig = 0.0
        state["calls"] += 1
        state["trace"].append((state["step"], state["slot"], sig))
        slot = state["slots"].setdefault(state["slot"], {"first": None, "resid_v": None, "resid_a": None, "acc": 0.0, "prev_in": None})
        blocks = self.transformer_blocks
        # first block, exactly as the loop does it
        v0, a0 = video, audio
        v0 = self.block_input_processor(v0, perturbations, 0, self_attn_type=_T.SKIP_VIDEO_SELF_ATTN, cross_attn_type=_T.SKIP_A2V_CROSS_ATTN)
        if a0 is not None:
            a0 = self.block_input_processor(a0, perturbations, 0, self_attn_type=_T.SKIP_AUDIO_SELF_ATTN, cross_attn_type=_T.SKIP_V2A_CROSS_ATTN)
        v1, a1 = blocks[0](video=v0, audio=a0)
        skip = False
        metric = None
        if 1 < state["step"] < steps and slot["first"] is not None:
            if mode == "fb":
                metric = rel_l1(v1.x, slot["first"])
                skip = metric < thr
            else:
                metric = rel_l1(video.x, slot["prev_in"])
                slot["acc"] += metric
                skip = slot["acc"] < thr
            state["metrics"].append((state["step"], state["slot"], round(metric, 4)))
        slot["prev_in"] = video.x
        if skip:
            state["hits"] += 1
            v_out = dataclasses.replace(v1, x=v1.x + slot["resid_v"])
            a_out = dataclasses.replace(a1, x=a1.x + slot["resid_a"]) if (a1 is not None and slot["resid_a"] is not None) else a1
            return v_out, a_out
        # the remaining blocks, exactly as the loop does them
        v, a = v1, a1
        for idx in range(1, len(blocks)):
            if v is not None:
                v = self.block_input_processor(v, perturbations, idx, self_attn_type=_T.SKIP_VIDEO_SELF_ATTN, cross_attn_type=_T.SKIP_A2V_CROSS_ATTN)
            if a is not None:
                a = self.block_input_processor(a, perturbations, idx, self_attn_type=_T.SKIP_AUDIO_SELF_ATTN, cross_attn_type=_T.SKIP_V2A_CROSS_ATTN)
            v, a = blocks[idx](video=v, audio=a)
        slot.update(first=v1.x, resid_v=v.x - v1.x, resid_a=(a.x - a1.x) if a is not None and a1 is not None else None, acc=0.0)
        return v, a

    from ltx_core.guidance.perturbations import PerturbationType as _T
    LTXModel._process_transformer_blocks = patched

    import atexit
    atexit.register(lambda: log(f"cache {spec}: {state['hits']} of {state['calls']} stage-1 passes skipped; trace {state['trace'][:20]} metrics (step, slot, value) {state['metrics']}"))
    log(f"cache {spec} installed")
