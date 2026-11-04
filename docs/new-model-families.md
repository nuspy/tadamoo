# New model families for the engine

Two questions are open: running DeepSeek V4.1 Flash, and supporting selective state-space layers (Mamba S6). Neither is started.
The figures below come from third-party pages that disagree with each other; the first step of each section is to read the model's
own report and `config.json` and replace them.

## DeepSeek V4.1 Flash

What is reported: 40 layers, hidden size 5120, 6 of 384 routed experts per token, about 8B active parameters in prefill and 16B in decode,
a total of 522B to 552B (748B when the Engram tables are added), sliding-window plus compressed sparse attention with a two-level indexer,
native vision. No source describes MLA for this version.

Two ways to run it:

| Way | What it takes | What it gives |
|---|---|---|
| Strata's `strata-dsv41` through the Loader | nothing new: `serve/engines.py` and `docs/engines-protocol.md` already host it as a second engine | the model runs today, with Strata's speed and limits |
| The family inside Basalt's engine | a new model section, attention, router and tokenizer in `src/`, with parity tests and the quality gate | one engine, Basalt's cache tiers and decode path |

What the engine already has that the family also needs: streamed experts with RAM and SSD cache tiers, the hyper-connection streams
(`docs/precision.md`), the per-layer n-gram table with its `direct`, `ram` and `mmap` modes (the Engram tables look like the same kind
of object, to be checked against the report), MTP decode, the IQ weight formats and the int8 KV option.
What is new: compressed sparse attention with the sliding window and the indexer, 384-expert routing, the Engram lookup, the DSML tool-call
format (already parsed by the Strata host), the tokenizer and the vision path.

Steps before any engine code:

1. Read the official report and `config.json`; write the real numbers here.
2. List every operation of the model against the kernels Basalt has; mark each as reused, adapted or new.
3. Measure `strata-dsv41` on this PC as the baseline: tokens per second, RAM and SSD traffic. It needs the GPU and a model file.
4. Port only if Basalt can beat that baseline by a margin that pays for a second implementation; otherwise keep the Loader route.

A 2-bit file of this size needs about 140 to 160 GB of disk; check the target drive first.
The quality gate needs a reference: the output of the Strata engine on the same prompts.

## Selective state-space layers (S6)

Qwen3.8-Flash-Next is not a Mamba model. It has 36 Gated DeltaNet layers and 12 sparse attention layers (`docs/precision.md`).
Gated DeltaNet updates a matrix state with the delta rule; S6 keeps a diagonal state with input-dependent decay, input and output maps.
They are related, not interchangeable: the weights of one cannot be loaded into the other.

Three different things are meant by "adding S6":

| Intervention | Needs retraining | Risk | Judgement |
|---|---|---|---|
| Replace GDN layers with S6 | yes, at least a distillation | high: it loses what the layers learned | not as a first step |
| Add S6 layers beside GDN and attention | yes, continued pretraining | high, and the gain is a hypothesis | research, outside the engine |
| Support S6 layers in the engine for models that already have them | no | low | the only change that is the engine's job |

The third needs a target model first. Hybrid models with Mamba layers exist (Nemotron-family and Jamba-family files are already on this PC under
LM Studio; which of them use Mamba-1 selective scan and which Mamba-2 is read from each model's config, not assumed).
Decode is a short recurrence per layer and reuses the fp32 state handling of the GDN decode kernel; prefill needs a chunked scan,
which is the real work. The kernel is built only when a model is chosen, with a parity test against a CPU reference and a microbench line.
