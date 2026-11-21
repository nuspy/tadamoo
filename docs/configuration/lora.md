# LoRA adapters

A LoRA adapter (a GGUF written by `tools/convert/lora.py`, in llama.cpp's naming convention) is fused into the dense
native matrices (the ten projection families `attn_qkv`, `attn_gate`, `ssm_out`, `attn_q`, `attn_k`, `attn_v`,
`attn_output`, `ffn_gate_shexp`, `ffn_up_shexp`, `ffn_down_shexp`) once, at load; a request does not choose or change
it. A covered tensor becomes Q8_0 in VRAM regardless of its type in the pack: see
[precision](../precision/features.md#lora-adapters). An adapter that touches a tensor outside those ten families
(the router, the routed experts, or anything else) is refused at startup, naming how many and what share of the
adapter's update that is.

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `lora` | engine | file path | off | A LoRA adapter at scale 1. |
| `lora-scaled` | engine | `FILE:SCALE[,...]` | off | LoRA adapters with their scales. |
