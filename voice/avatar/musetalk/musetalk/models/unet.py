import torch
import torch.nn as nn
import math
import json

from diffusers import UNet2DConditionModel
import sys
import time
import numpy as np
import os

class PositionalEncoding(nn.Module):
    def __init__(self, d_model=384, max_len=5000):
        super(PositionalEncoding, self).__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)

    def forward(self, x):
        b, seq_len, d_model = x.size()
        pe = self.pe[:, :seq_len, :]
        x = x + pe.to(x.device)
        return x
    
class UNet():
    def __init__(self, 
                 unet_config,
                 model_path,
                 use_float16=False,
                 device=None,
                 dtype=None
        ):
        """`dtype` (opzionale): precisione dei pesi in memoria. Con un device
        CUDA e un dtype, il modello nasce direttamente in VRAM in quella
        precisione e i pesi vanno dal file (mmap) alla GPU un tensore alla
        volta: niente copia fp32 completa in RAM (l'UNet pesa 3,4 GB in fp32 e
        il vecchio percorso ne teneva DUE copie in RAM prima di mandarlo in
        GPU). Senza `dtype` il comportamento resta quello storico."""
        with open(unet_config, 'r') as f:
            unet_config = json.load(f)
        self.pe = PositionalEncoding(d_model=384)
        if device != None:
            self.device = device
        else:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if use_float16 and dtype is None:
            dtype = torch.float16
        if dtype is not None and torch.device(self.device).type == "cuda":
            try:
                self.model = self._carica_diretto(unet_config, model_path, dtype)
                return
            except Exception as e:  # noqa: BLE001 - si ripiega sul percorso storico
                print(f"[unet] caricamento diretto in VRAM non riuscito "
                      f"({type(e).__name__}: {e}): uso il percorso classico",
                      file=sys.stderr, flush=True)
                import gc
                gc.collect()
                torch.cuda.empty_cache()
        self.model = UNet2DConditionModel(**unet_config)
        weights = torch.load(model_path) if torch.cuda.is_available() else torch.load(model_path, map_location=self.device)
        self.model.load_state_dict(weights)
        del weights
        if use_float16:
            self.model = self.model.half()
        self.model.to(self.device)

    def _carica_diretto(self, unet_config, model_path, dtype):
        """Costruisce il modello sulla GPU gia' nel dtype finale e vi copia i
        pesi dal file mappato in memoria (la mappatura non e' una copia: le
        pagine sono quelle del file e tornano al sistema alla chiusura)."""
        precedente = torch.get_default_dtype()
        torch.set_default_dtype(dtype)
        try:
            with torch.device(self.device):
                modello = UNet2DConditionModel(**unet_config)
        finally:
            torch.set_default_dtype(precedente)
        pesi = torch.load(model_path, map_location="cpu", mmap=True)
        modello.load_state_dict(pesi)       # copy_ con cast, un tensore alla volta
        del pesi
        lontani = [n for n, t in list(modello.named_parameters())
                   + list(modello.named_buffers())
                   if t.device.type != torch.device(self.device).type]
        if lontani:
            raise RuntimeError(f"tensori fuori dalla GPU: {lontani[:3]}")
        return modello

if __name__ == "__main__":
    unet = UNet()
