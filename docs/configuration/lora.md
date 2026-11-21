# LoRA adapters

A LoRA adapter (a GGUF written by `tools/convert/lora.py`, in llama.cpp's naming convention) is fused into the dense
native matrices (the ten projection families `attn_qkv`, `attn_gate`, `ssm_out`, `attn_q`, `attn_k`, `attn_v`,
`attn_output`, `ffn_gate_shexp`, `ffn_up_shexp`, `ffn_down_shexp`) once, at load; a request does not choose or change
it. A covered tensor becomes Q8_0 in VRAM regardless of its type in the pack: see
[precision](../precision/features.md#lora-adapters). An adapter that touches a tensor outside those ten families
(the router, the routed experts, or anything else) still has the ten families it does cover fused, by default; a
log line at startup names how many tensors it could not apply and what share of the adapter's update that is.
`no-lora-partial` on refuses that adapter outright instead.

| Setting | Where | Type, values | Default | What it does |
| --- | --- | --- | --- | --- |
| `lora` | engine | file path | off | A LoRA adapter at scale 1. |
| `lora-scaled` | engine | `FILE:SCALE[,...]` | off | LoRA adapters with their scales. |
| `no-lora-partial` | engine | switch | off | Refuse an adapter that touches a tensor outside the ten families instead of fusing the rest. |
