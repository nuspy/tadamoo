# Layer loop

`--layer-loop A:B:K` runs the pack's layers `[A, B)` K times in a row, as if the window were copied K times into the model.
The weights and the routed experts are the pack's own and are shared by every pass; each pass keeps its own GDN state, its
own K/V cells and its own graphs. Without the flag, or with K = 1, nothing changes.

```
basalt --serve --model M.basalt --layer-loop 8:16:2 --layer-loop-alpha 1,0.5
```

`--layer-loop-alpha a1,...,aK` is the weight each pass writes into the residual streams: the write of both halves of
every slot in pass i is multiplied by `a_i` (default 1 each). An alpha of 0 makes a pass write nothing: it only updates
its own state, which no other layer reads. `1,0` and `0,1` therefore give the model without the loop, which is how the
build is checked.

From the server: `"engine": {"layer-loop": "8:16:2", "layer-loop-alpha": "1,0.5"}`.

## What it needs

| Rule | Why |
|---|---|
| `2 <= A < B <= 48`, `B - A` a multiple of 4, `K` from 1 to 8 | layers 0 and 1 carry the embedding and the PLE; a full-attention layer comes every 4 layers, and a copy of the window must keep that rhythm |
| one GPU | a second GPU, `--peer-device` and `--layer-split` index the layers per card |
| no control vector | its tables are per layer |
| as many alphas as K, finite, 0 or more | one weight per pass |

A refused combination exits with code 2 and one log line, before the model is read where it can be.

## What it costs and what is off

- The model has `48 + (K - 1)(B - A)` slots. The GDN state, the K/V cells and the graphs grow with the slots; the dense
  weights and the experts in RAM and VRAM do not.
- Each decoded token reads the experts of every slot, so the expert traffic grows by about the same factor.
- Expert swaps (`--adapt-swaps`) and the prompt look-ahead are off while the loop is on, and the start says so
  (`layer loop: expert swaps and the prompt look-ahead are off`). Speed numbers against a run without the loop differ for
  that reason too: run the reference with `--adapt-swaps 0`.
- Parked conversations are keyed by the window, K and the alphas: two loops with the same number of slots never share a
  state.

The start logs one line: `layer loop: [8,16) x2, alpha 1,0.5, 56 slots`.

## How to measure it

Write a base with the same engine and flags and run the quality gate against it
([quality](../precision/quality.md#a-base-written-by-the-engine)): `ln(PPL/base)`, KLD and top-1 are then the shift the
loop causes. The numbers change by construction when the loop is on; see [features](../precision/features.md#layer-loop).
