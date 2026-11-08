# MuseTalk — copia vendorizzata

Questa cartella NON e' un clone: e' parte del repository callAPIcall, e le
modifiche che ci facciamo sono nostri commit come tutti gli altri.

## Perche'

Prima era un clone di upstream, in .gitignore e con un `.git` proprio. Due
conseguenze, entrambe brutte: qualunque nostra ottimizzazione era invisibile al
nostro repository e sarebbe sparita alla prima reinstallazione, e l'installer
clonava `--depth 1` senza fissare un commit, quindi due macchine installate a
distanza di settimane prendevano codice diverso.

## Provenienza

    origine:  https://github.com/TMElyralab/MuseTalk.git
    commit:   0a89dec45a0192b824e3cf4daf96c239440c5ed8
    data:     2025-09-26
    licenza:  MIT, Copyright (c) 2024 Tencent Music Entertainment Group

Il file LICENSE originale e' conservato qui accanto, come la licenza MIT
richiede. Le nostre modifiche sono identificabili dalla storia di git:

    git log --oneline -- avatar/MuseTalk

## Cosa NON e' stato incluso

  - `models/`  i pesi (~7 GB): li scarica l'installer dalle fonti ufficiali;
  - `assets/` e `data/`  i materiali di esempio. Il README di MuseTalk li
    dichiara "available for non-commercial research purposes only", a
    differenza del codice (MIT) e del modello addestrato ("any purpose, even
    commercially"). Non ci servono, e includerli ci legherebbe a quel vincolo.

## Aggiornare da upstream

Non c'e' un `git pull`: si confronta a mano con il commit di provenienza qui
sopra, si porta cio' che serve e si aggiorna questo file.
