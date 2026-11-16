> Reference: [README](../../README.md).

## Models

Ready-to-run `.basalt` packs are on [Hugging Face](https://huggingface.co/jesdga/Qwen3.8-Flash-Next-Basalt). A pack
you convert yourself will be significantly slower than the official `.basalt` files.

Ready-to-run packs, with the quality of each measured against Q8_0, are on
[Hugging Face](https://huggingface.co/jesdga/Qwen3.8-Flash-Next-Basalt):
`Qwen3.8-FN-NVFP4-DQ6_K`, `-NVFP4-DQ8`, `-UD-Q4_K_XL`, `-UD-Q5_K_XL`, `-UD-Q6_K_XL`, `-Q8_0`, `-GSQ-RCO-IQ3_S`, `-GSQ-RCO-IQ3_XXS` and `-GSQ-RCO-Q2_0` (each `.basalt`).
The two NVFP4 packs share NVIDIA's NVFP4 experts; `D` names the dense projections' format: DQ6_K is the default and
faster, DQ8 is about 7% slower on prose with a lower KLD ([precision](../precision/isolated-costs.md)).
A model is one file: weights, tokenizer, MTP drafter, 8-bit hyper-connection copies and vision projector. The expert
profile ships with the engine ([data/profiles](../../data/profiles)).
Quality measured with `basalt-bench --quality`: 20 chunks of wikitext-2 (512 tokens each), scored through the decode
path against the Q8_0 reference. Lower KLD and higher top-1 agreement are closer to Q8_0. How precision is handled and how quality is measured:
[docs/precision.md](../precision.md).

| model | source | file | experts | PPL | KLD mean | same top-1 |
|---|---|---:|---:|---:|---:|---:|
| GSQ-RCO-Coder IQ1_M (256 experts) | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF) | 57.4 GiB | 23.4 GiB | | 1.03 | 69% |
| GSQ-RCO Q2_0 | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF) | 64.9 GiB | 31.6 GiB | 2.971 | 0.486 | 80.6% |
| GSQ-RCO IQ3_XXS | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF) | 74.4 GiB | 40.0 GiB | 2.535 | 0.277 | 86.8% |
| UD-Q2_K_XL | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 76.5 GiB | 42.9 GiB | 2.755 | 0.323 | 84.3% |
| UD-IQ3_XXS | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 79.4 GiB | 45.3 GiB | 2.564 | 0.244 | 87.0% |
| GSQ-RCO IQ3_S | [ISTA-DASLab](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF) | 81.0 GiB | 46.8 GiB | 2.387 | 0.177 | 89.4% |
| UD-Q3_K_XL | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 86.8 GiB | 52.0 GiB | 2.457 | 0.160 | 89.3% |
| UD-Q4_K_XL | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 106.7 GiB | 71.7 GiB | 2.321 | 0.068 | 93.0% |
| Q8_0 | [Unsloth](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) | 178.3 GiB | 119.5 GiB | 2.282 | reference | |

The IQ3_S, IQ3_XXS and Q2_0 packs carry an MTP layer fine-tuned on the model's own output (self-distillation), which
raises draft acceptance. Output is unchanged, because verify decides every token. Q4_K_XL and Q8_0 keep the original
MTP layer. `--mtp-dense FILE` embeds that tuned layer (the `mtp/dense` section's raw bytes, checked against the
source layout) and records it as self-distilled, with its SHA-256, in `meta.mtp`. The tuned layer ships with the
packs, inside the model file; the converter input is only needed to build a pack yourself.
- [Configuration](../configuration/index.md): every setting.
- [Running](../running.md): parallel requests, prompt chunks, memory floors, image input.
- [basalt-probe](../probe.md): measures the machine, the cold decode speed, and suggests values.
- [The .basalt model file](../model-file.md): the layout, and building a pack yourself (including NVFP4).
- [Platforms](../platforms.md): Linux and Windows requirements, the build steps and the Windows limitations.
- [Changelog](../changelog/index.md): what each release changed.
- [Building and testing](../building.md), [Precision and quality](../precision/index.md), [Contributing](../../CONTRIBUTING.md).

The model files are derived from Qwen3.8-Flash-Next and are distributed under the
[Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE), not Basalt's MIT
license. The GSQ-RCO quants come from IST Austria's Deep Algorithms and Systems Lab; please cite the
[GSQ](https://arxiv.org/abs/2604.18556) and [RCO](https://arxiv.org/abs/2605.00649) papers if you use them.

## Community benchmark runs

Rows submitted to the original project (`docs/community/benchmarks.json` of `jesdga95/basalt`, copied on 2026-10-26, 25 runs), all taken with
`basalt-bench --community`, so they compare with each other: prompt reading speed on a 16k-token prompt (tokens/s) and decode speed (tokens/s),
each with the GPU power limit it ran at. Credit to the people who submitted them. The two rows at the end are **our own development
measurements** on this fork (Windows, RTX 5090, 600 W, 127.6 GiB RAM) and are **not the same method**: decode is a chat request with
temperature 0.6 and the drafter on, prompt reading is a 13.5k-token repeated text, and the model is the OrcaRouter Uncensored build of the
same architecture. Compare the rows of one source with each other, and these two with the others only as an order of magnitude.

| GPU | Power | RAM GiB | OS | Quant | Prompt 16k tok/s | Decode tok/s | Submitted by |
|---|---|---|---|---|---|---|---|
| 2x RTX 5090 32 GB | 500 W | 123 | Linux | NVFP4 | 7,740 | 461 | neuro |
| RTX 5060 Ti + RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7,741 | 438 | jesdga95 |
| RTX 5090 32 GB | 575 W | 62 | Linux | IQ3_S | 5,677 | 376 | knoopx |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_S | 8,859 | 375 | drakiss79 |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_XXS | 8,784 | 564 | drakiss79 |
| RTX 5090 32 GB | 475 W | 247 | Linux | NVFP4 | 9,556 | 312 | giveen |
| RTX 5090 32 GB | 500 W | 62 | Linux | NVFP4 | 9,289 | 305 | drakiss79 |
| RTX 5090 32 GB | 500 W | 191 | Windows | NVFP4 | 2,431 | 199 | HenryPDT |
| RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7,732 | 317 | jesdga95 |
| RTX 5090 Laptop GPU 24 GB | 175 W | 47 | WSL2 | IQ3_XXS | 3,892 | 243 | pyj99_fr |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_S | 3,226 | 187 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_S | 3,236 | 158 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_XXS | 3,169 | 202 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_XXS | 2,894 | 189 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | NVFP4 | 4,072 | 131 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | NVFP4 | 3,316 | 96 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | Q4_K_XL | 2,983 | 72 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q4_K_XL | 2,866 | 104 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q5_K_XL | 2,981 | 77 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q6_K_XL | 2,674 | 66 | foolio |
| 2x RTX 5060 Ti 16 GB | 180 W | 60 | Linux | IQ3_S | 2,370 | 161 | KimDaeHwan99 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | IQ3_S | 2,427 | 137 | jesdga95 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | NVFP4 | 2,687 | 92 | jesdga95 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | IQ3_S | 3,180 | 138 | lindworm-hermes2026 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | Q4_K_XL | 2,835 | 78 | lindworm-hermes2026 |
| RTX 5090 32 GB | 600 W | 127.6 | Windows | IQ3_XXS (this fork, own method) | 7,319 | 179 (exact) / 195 (relaxed 0.3) | this fork |
| RTX 5090 32 GB | 600 W | 127.6 | Windows | IQ4_XS (this fork, own method) | 5,206 | 100 (exact) / 117 (relaxed 0.3) | this fork |


## Contributing

See [CONTRIBUTING.md](../../CONTRIBUTING.md).

## License

New and modified code is licensed under the **TaDaMoo Win+GUI Community and Commercial License** (see [LICENSE](../../LICENSE)), free of charge for individuals and for businesses with annual gross revenue under $10,000,000 USD (enterprise commercial license required at or above $10M). Pre-existing upstream code (Strata, upstream Basalt, ggml) remains under its original MIT license (see [THIRD_PARTY_LICENSES](../../THIRD_PARTY_LICENSES)).
