"""Dai materiali del wizard agli asset dell'avatar, per i modi A e C.

L'elaborazione sta sul PC (GPU e strumenti, e una volta per persona vale per
tutti i telefoni), non sul dispositivo. Produce, sotto `avatar/persone/<nome>/`:

  poster.jpg        il ritratto, come oggi
  wav2lip/face.png  il volto RADDRIZZATO ritagliato a 96x96
  wav2lip/box.json  come reincollare la bocca generata sul poster
  visemes/<V>.png   (modo C) una forma di bocca per viseme, dalla sola foto (avatar_visemes.py)

La posizione della bocca si indica A MANO: sul PC, col mouse, si segnano i due
angoli della bocca sul poster. Da quei due punti si ricava tutto ciò che serve —
il centro della bocca, la sua larghezza e l'inclinazione (roll) della testa — e
si costruisce un ritaglio 96x96 raddrizzato: Wav2Lip lavora su un volto dritto
(come in training) e la bocca generata si reincolla ruotata alla giusta
inclinazione. Il rilevamento automatico dei landmark si e' rivelato non
abbastanza preciso su foto reali; due clic lo sono sempre.

Il box.json descrive la composizione senza ambiguita': `inv` e' la matrice
affine 2x3 che mappa il volto 96x96 generato -> pixel del poster; il client la
applica con setTransform e ridisegna la sola regione `mouth`.

Alla creazione, prima che l'utente segni la bocca, si mette un default
grossolano (volto con Haar, bocca alla posizione tipica): l'avatar e' subito
usabile e poi si raffina a mano.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

import cv2
import numpy as np

from . import avatar_visemes

log = logging.getLogger("callbridge.avatar_prep")

FACE_SIZE = 96
# La bocca (angolo-angolo) occupa circa questa frazione del lato del ritaglio,
# e il suo centro sta a questa frazione di lato SOTTO il centro del ritaglio.
# Ricavati dai ritagli tipici di Wav2Lip; il resto lo aggiusta l'utente a mano.
_BOCCA_FRAZ_LARG = 0.20
_BOCCA_SOTTO_CENTRO = 0.13


def _asset_da_bocca(img: np.ndarray, p1, p2) -> tuple[np.ndarray, dict]:
    """Volto 96x96 raddrizzato + composizione, dai due angoli della bocca.

    `p1`, `p2`: angoli della bocca (in pixel del poster), in qualunque ordine.
    Il ritaglio si dimensiona e si posiziona in modo che la bocca cada dove
    Wav2Lip se l'aspetta (centrata in orizzontale, poco sotto il centro).
    """
    H, W = img.shape[:2]
    p1 = np.array(p1, dtype=float)
    p2 = np.array(p2, dtype=float)
    dvec = p2 - p1
    roll = math.degrees(math.atan2(dvec[1], dvec[0]))
    mc = (p1 + p2) / 2.0                       # centro bocca
    mw = float(np.hypot(dvec[0], dvec[1])) or 1.0
    size = mw / _BOCCA_FRAZ_LARG               # lato del ritaglio quadrato
    ang = math.radians(roll)
    up = np.array([math.sin(ang), -math.cos(ang)])   # "su" del volto (verso la fronte)
    # Il centro del ritaglio sta SOPRA la bocca, così la bocca cade nella metà
    # inferiore del 96x96 (quella che Wav2Lip maschera e rigenera).
    Fc = mc + up * (_BOCCA_SOTTO_CENTRO * size)

    # Mfull: poster(orig) -> face 96x96 (raddrizza di roll attorno a Fc, centra,
    # scala). Fc e' il perno della rotazione, quindi resta fisso.
    R = cv2.getRotationMatrix2D((float(Fc[0]), float(Fc[1])), roll, 1.0)

    def _M33(m):
        return np.vstack([m, [0, 0, 1]])
    s = FACE_SIZE / size
    T2 = np.array([[1, 0, -Fc[0]], [0, 1, -Fc[1]], [0, 0, 1]])
    S = np.array([[s, 0, 0], [0, s, 0], [0, 0, 1]])
    T3 = np.array([[1, 0, FACE_SIZE / 2], [0, 1, FACE_SIZE / 2], [0, 0, 1]])
    Mfull = T3 @ S @ T2 @ _M33(R)
    face = cv2.warpAffine(img, Mfull[:2, :], (FACE_SIZE, FACE_SIZE))

    # Regione bocca nel 96x96: i due angoli portati nel volto, con margine.
    pf = (Mfull[:2, :2] @ np.array([p1, p2]).T).T + Mfull[:2, 2]
    mx0, mx1 = float(pf[:, 0].min()), float(pf[:, 0].max())
    myc = float(pf[:, 1].mean())
    larg = mx1 - mx0
    mgx = larg * 0.18
    halfh = larg * 0.55                        # le labbra si estendono su/giù
    mb = [max(0.0, mx0 - mgx), max(0.0, myc - halfh),
          min(float(FACE_SIZE), mx1 + mgx), min(float(FACE_SIZE), myc + halfh)]
    mouth = {"x": round(mb[0], 2), "y": round(mb[1], 2),
             "w": round(mb[2] - mb[0], 2), "h": round(mb[3] - mb[1], 2)}

    Minv = np.linalg.inv(Mfull)[:2, :]         # face96 -> poster
    Cc = (np.linalg.inv(Mfull) @ np.array([FACE_SIZE / 2, FACE_SIZE / 2, 1]))[:2]
    # bbox assiale del ritaglio (retrocompat col vecchio formato)
    half = size / 2
    corners = np.array([[-half, -half], [half, -half], [half, half], [-half, half]])
    rot = np.array([[math.cos(-ang), -math.sin(-ang)],
                    [math.sin(-ang), math.cos(-ang)]])
    wc = (rot @ corners.T).T + Cc
    bx0, by0 = float(wc[:, 0].min()), float(wc[:, 1].min())
    bx1, by1 = float(wc[:, 0].max()), float(wc[:, 1].max())

    box = {
        "ver": 2,
        "inv": [round(float(v), 6) for v in
                (Minv[0, 0], Minv[0, 1], Minv[0, 2],
                 Minv[1, 0], Minv[1, 1], Minv[1, 2])],
        "mouth": mouth,
        "angle_deg": round(roll, 2),
        "crop": {"cx": round(float(Cc[0]), 1), "cy": round(float(Cc[1]), 1),
                 "size": round(size, 1)},
        # i due angoli segnati, così l'editor li può ridisegnare per correggerli
        "bocca": {"p1": [round(float(p1[0]), 1), round(float(p1[1]), 1)],
                  "p2": [round(float(p2[0]), 1), round(float(p2[1]), 1)]},
        "x": int(max(0, bx0)), "y": int(max(0, by0)),
        "w": int(min(W, bx1) - max(0, bx0)), "h": int(min(H, by1) - max(0, by0)),
        "poster_w": W, "poster_h": H,
    }
    return face, box


def _bocca_default(img: np.ndarray) -> tuple:
    """Due angoli di bocca plausibili, prima che l'utente li segni.

    Volto frontale con Haar (già in cv2): la bocca sta circa a due terzi
    dell'altezza del volto, larga poco più di un terzo. Se non trova un volto,
    mette la bocca al centro del poster: un punto di partenza da correggere.
    """
    H, W = img.shape[:2]
    casc = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades,
                     "haarcascade_frontalface_default.xml"))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    facce = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                  minSize=(80, 80))
    if len(facce):
        x, y, fw, fh = (int(v) for v in max(facce, key=lambda f: f[2] * f[3]))
        cx, cy = x + fw / 2, y + 0.68 * fh
        mw = 0.35 * fw
    else:
        cx, cy = W / 2, H * 0.62
        mw = 0.12 * W
    return (cx - mw / 2, cy), (cx + mw / 2, cy)


def prepara_da_bocca(poster: Path, dest: Path, p1, p2) -> dict:
    """Genera gli asset del modo A dai due angoli della bocca segnati a mano."""
    img = cv2.imread(str(poster))
    if img is None:
        raise ValueError("poster non leggibile")
    face, box = _asset_da_bocca(img, p1, p2)
    out = dest / "wav2lip"
    out.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out / "face.png"), face)
    (out / "box.json").write_text(json.dumps(box), encoding="utf-8")
    log.info("avatar_prep: bocca a mano -> %s (roll %.1f°)", out, box["angle_deg"])
    return {"modo": "wav2lip", "box": box, "visemes": avatar_visemes.write_sprites(img, p1, p2, dest)}


def prepara_wav2lip(poster: Path, dest: Path) -> dict:
    """Asset del modo A con un default grossolano (da raffinare a mano).

    Solleva ValueError se il poster non è leggibile. Non solleva se non trova un
    volto: mette comunque una bocca al centro, che l'utente sposterà.
    """
    img = cv2.imread(str(poster))
    if img is None:
        raise ValueError("poster non leggibile")
    p1, p2 = _bocca_default(img)
    face, box = _asset_da_bocca(img, p1, p2)
    out = dest / "wav2lip"
    out.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out / "face.png"), face)
    (out / "box.json").write_text(json.dumps(box), encoding="utf-8")
    log.info("avatar_prep: default -> %s", out)
    return {"modo": "wav2lip", "metodo": "default", "box": box, "visemes": avatar_visemes.write_sprites(img, p1, p2, dest)}


def completa(dest: Path) -> None:
    """Makes what the photo allows and the folder lacks: the Wav2Lip face and the viseme sprites. A mouth the person marked is kept.

    Raises ValueError when there is no readable poster."""
    poster = dest / "poster.jpg"
    img = cv2.imread(str(poster))
    if img is None:
        raise ValueError("poster non leggibile")
    box_file = dest / "wav2lip" / "box.json"
    if (dest / "wav2lip" / "face.png").is_file() and box_file.is_file():
        if not (dest / avatar_visemes.FOLDER / avatar_visemes.FRAME_FILE).is_file():
            marks = json.loads(box_file.read_text(encoding="utf-8")).get("bocca") or {}
            avatar_visemes.write_sprites(img, marks["p1"], marks["p2"], dest)
        return
    prepara_wav2lip(poster, dest)


def prepara(persona_dir: Path) -> dict:
    """Prepara tutti gli asset possibili dai materiali già presenti.

    Dal poster: il volto per Wav2Lip e gli sprite dei visemi, con la bocca di
    default (da raffinare a mano).
    """
    poster = persona_dir / "poster.jpg"
    fatti: dict = {}
    if poster.is_file():
        try:
            fatti["wav2lip"] = prepara_wav2lip(poster, persona_dir)
        except ValueError as e:
            fatti["wav2lip_errore"] = str(e)
    return fatti
