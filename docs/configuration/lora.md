# LoRA adapters

A LoRA adapter (a GGUF written by `tools/convert/lora.py`, in llama.cpp's naming convention, or by a llama.cpp
trainer) is fused once, at load; a request does not choose or change it. Two parts of the model take it:

- **The dense matrices**, the ten projection families `attn_qkv`, `attn_gate`, `ssm_out`, `attn_q`, `attn_k`,
  `attn_v`, `attn_output`, `ffn_gate_shexp`, `ffn_up_shexp`, `ffn_down_shexp`. A covered tensor becomes Q8_0 in VRAM
  regardless of its type in the pack: see [precision](../precision/features.md#lora-adapters).
- **The routed experts** (`ffn_gate_exps`, `ffn_up_exps`, `ffn_down_exps`, stacked over the experts or one pair per
  expert). The experts are written once into a fused copy of the `experts` section, in the pack's own types, so the
  blobs, the VRAM they take and the expert cache are the same size as without the adapter. The copy is kept beside the
  adapter, named from the adapter, the model and the scale, and used again by the next load; when the adapter or the
  scale changes the old copy is removed and a new one is written. It takes the size of the experts section on disk
  (about 61 GiB for the IQ4_XS pack) and has to be written before the model loads. A layer whose experts are in a type
  the engine cannot quantize again is refused by name.

An adapter that touches a tensor outside these (the router or anything else) still has the rest fused, by default; a
log line at startup names how many tensors it could not apply and what share of the adapter's update that is.
`no-lora-partial` on refuses that adapter outright instead.

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `lora` | engine | file path | off | A LoRA adapter at scale 1. |
| `lora-scaled` | engine | `FILE:SCALE[,...]` | off | LoRA adapters with their scales. |
| `no-lora-partial` | engine | switch | off | Refuse an adapter that touches a tensor outside the dense families and the routed experts instead of fusing the rest. |

**What it costs.** Development measurement (`tests/lora_gpu_checks.md`), an adapter covering all ten dense families on
every layer of an IQ4_XS pack (300 tensors, GPU power limit 600 W): the dense weights go up 1328.9 MiB, the
expert cache gives up 392 slots to hold the room, and fusing those 300 tensors at load takes 15 to 18 seconds. No
measurable decode-speed cost at `--spec 2`; `--spec 7` itself varies run to run by more than any adapter could
explain, so it shows nothing either way.
