"""Trains one stage of an ER-NeRF (the head, then the torso) on a prepared data folder, with the model, dataset and trainer of Linly-Talker's NeRF folder.

Started by voice/avatar_engines/ernerf.py in the engine environment with the run folder (the vendored code and the weights) as its working directory.
The options are those of the wrapper's `Options` (TFG/NeRFTalk.py) set for training; ER-NeRF's own main.py is not in the vendored code, so this script
stands in for it. It prints a progress event after each epoch and, at the end, the checkpoint it wrote."""
from __future__ import annotations

import argparse
import glob
import os
import sys
from types import SimpleNamespace

import _frames

# The wrapper's Options, with what training changes: the data folder, the stage, the iterations, no test mode.
DEFAULTS = dict(
    test=False, test_train=False, data_range=[0, -1], seed=0, lr=1e-2, lr_net=1e-3, num_rays=4096 * 16, cuda_ray=True, max_steps=16, num_steps=16,
    upsample_steps=0, update_extra_interval=16, max_ray_batch=4096, warmup_step=10000, amb_aud_loss=1, amb_eye_loss=1, unc_loss=1, lambda_amb=1e-4,
    fp16=True, bg_img="white", fbg=False, exp_eye=True, fix_eye=-1, smooth_eye=False, torso_shrink=0.8, color_space="srgb", preload=0, bound=1, scale=4,
    offset=[0, 0, 0], dt_gamma=1 / 256, min_near=0.05, density_thresh=10, density_thresh_torso=0.01, patch_size=1, init_lips=False, finetune_lips=False,
    smooth_lips=False, gui=False, W=450, H=450, radius=3.35, fovy=21.24, max_spp=1, att=2, aud="", emb=False, ind_dim=4, ind_num=10000, ind_dim_torso=8,
    amb_dim=2, part=False, part2=False, train_camera=False, smooth_path=False, smooth_path_window=7, asr=False, asr_model="deepspeech", asr_play=False,
    asr_wav="", asr_save_feats=False, play=False, model="", pose="", fps=50, l=10, m=50, r=10, O=True, ckpt="latest", head_ckpt="", eval_interval=50)


def options(args) -> SimpleNamespace:
    opt = SimpleNamespace(**DEFAULTS)
    opt.path, opt.workspace, opt.iters, opt.asr_model = args.data, args.workspace, args.iters, args.asr_model
    opt.torso = args.stage == "torso"
    if opt.torso:
        heads = sorted(glob.glob(os.path.join(args.head_workspace, "checkpoints", "ngp_ep*.pth")))
        if not heads:
            raise RuntimeError(f"no head checkpoint in {args.head_workspace}")
        opt.head_ckpt = heads[-1]
    return opt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--stage", choices=("head", "torso"), required=True)
    ap.add_argument("--head-workspace", default="")
    ap.add_argument("--iters", type=int, required=True)
    ap.add_argument("--asr-model", default="deepspeech")
    args = ap.parse_args(argv)
    os.chdir(args.pack)
    sys.path.insert(0, args.pack)
    sys.path.insert(0, os.path.join(args.pack, "NeRF"))
    try:
        import numpy as np
        import torch
        from nerf_triplane.network import NeRFNetwork
        from nerf_triplane.provider import NeRFDataset
        from nerf_triplane.utils import LPIPSMeter, PSNRMeter, Trainer
        opt = options(args)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = NeRFNetwork(opt)
        train_loader = NeRFDataset(opt, device=device, type="train").dataloader()
        model.aud_features = train_loader._data.auds
        model.eye_areas = train_loader._data.eye_area
        trainer = Trainer("ngp", opt, model, device=device, workspace=opt.workspace, criterion=torch.nn.MSELoss(reduction="none"),
                          optimizer=lambda m: torch.optim.Adam(m.get_params(opt.lr, opt.lr_net), betas=(0.9, 0.99), eps=1e-8),
                          lr_scheduler=lambda o: torch.optim.lr_scheduler.LambdaLR(o, lambda it: 0.1 ** min(it / opt.iters, 1)),
                          scheduler_update_every_step=True, ema_decay=0.95, fp16=opt.fp16, metrics=[PSNRMeter(), LPIPSMeter(device=device)],
                          use_checkpoint=opt.ckpt, eval_interval=opt.eval_interval)
        valid_loader = NeRFDataset(opt, device=device, type="val", downscale=1).dataloader()
        epochs = int(np.ceil(opt.iters / len(train_loader)))
        one_epoch = trainer.train_one_epoch

        def counted(loader):
            one_epoch(loader)
            _frames.emit(event="progress", phase=args.stage, percent=round(100.0 * trainer.epoch / epochs, 1), epoch=trainer.epoch, epochs=epochs)

        trainer.train_one_epoch = counted
        _frames.emit(event="progress", phase=args.stage, percent=0, epoch=0, epochs=epochs)
        trainer.train(train_loader, valid_loader, epochs)
        made = sorted(glob.glob(os.path.join(opt.workspace, "checkpoints", "ngp_ep*.pth")))
        if not made:
            raise RuntimeError("the training wrote no checkpoint")
        _frames.emit(event="done", stage=args.stage, checkpoint=made[-1])
        return 0
    except Exception as e:                                       # noqa: BLE001 (the job reports it; nothing else can)
        _frames.emit(event="error", message=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
