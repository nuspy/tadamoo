# musetalk-venv — note di setup

Venv dedicato del worker avatar (`avatar_worker.py`). Python 3.10,
torch 2.11+cu130 (riusato dal sistema), MuseTalk clonato in `MuseTalk/`.

## mmcv 2.0.1 (ops CUDA) — build da sorgente

mmpose/mmdet (DWPose) richiedono `mmcv>=2.0.0rc4,<2.1.0`; `mmcv-lite` NON
basta (manca `mmcv._ext`). Niente wheel per torch 2.11/cu130: si compila con
`build-mmcv.bat` (in questa cartella). Ingredienti critici, tutti necessari:

- `vcvarsall x64 -vcvars_ver=14.44` + `DISTUTILS_USE_SDK=1` — senza,
  setuptools sceglie da solo il toolset 14.51 e `cudafe++` di nvcc 13 crasha
- `_CL_=/std:c++17 /DWIN32_LEAN_AND_MEAN` — gli header torch vogliono C++17;
  LEAN_AND_MEAN evita la macro `small` di rpcndr.h dentro gli header torch
- `NVCC_PREPEND_FLAGS=-allow-unsupported-compiler -std=c++17 -DWIN32_LEAN_AND_MEAN`
- `TORCH_CUDA_ARCH_LIST=12.0` (RTX 5090, sm_120)
- patch sorgente: in `spconv_utils.h`, `spconv_ops_cuda.cu`,
  `sparse_pool_ops_cuda.cu` sostituire `#include <torch/script.h>` con
  `#include <torch/types.h>` (script.h trascina dynamo/compiled_autograd.h
  che con nvcc+MSVC esplode in C2872 'std' ambiguo). Lo sdist va estratto in
  una cartella locale, patchato, e installato con
  `pip install <dir> --no-build-isolation` (vedi bat).

## Patch runtime nel worker

- `torch.load` forzato a `weights_only=False` (checkpoint MuseTalk legacy)
- il primo avvio di DWPose scarica `s3fd-619a316812.pth` (~86 MB) nella
  cache torch hub

## Output video

`pyvirtualcam` → filtro "OBS Virtual Camera" (basta il driver:
`drivers/obs-virtualcam/`, OBS Studio non richiesto).
