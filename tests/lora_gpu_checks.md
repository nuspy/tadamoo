# Prove sul modello vero per il collegamento LoRA (serve GPU + un adapter di prova)

Richiamate da `tests/QUEUE_daytime.md`. Servono: il server fermo durante la prova (un avvio del binario `bin/basalt`
alla volta), e un adapter di prova generato da `tools/convert/lora.py` su una cartella PEFT sintetica con i nomi
veri (non un GGUF scritto a mano) — almeno quattro file: uno nullo (B = 0) su tutte le dieci famiglie
(`ADAPTER_NULL.gguf`), uno non nullo su tutte (`ADAPTER.gguf`), uno parziale solo su `attn_q`/`attn_v`
(`ADAPTER_PARTIAL_QV.gguf`) e uno parziale solo su `attn_qkv` (`ADAPTER_PARTIAL_QKV.gguf`), da generare prima di
iniziare. `data/bench/conv.ids` e `data/bench/prefill.ids` sono i prompt pretokenizzati già nel repository
(`--tokens-file`); `MODEL.basalt` è il pacchetto reale che userai. Etichetta ogni numero col limite di potenza
della GPU.

| # | Cosa si fa | Comando | Cosa si confronta | Esito atteso |
|---|---|---|---|---|
| P1 | Senza `--lora`, il binario di prima contro questo | `bin/basalt --model MODEL.basalt --tokens-file data/bench/conv.ids --greedy --max-new 64` (il binario di prima, poi questo) | le due uscite greedy | identiche: il collegamento non cambia nulla quando `--lora` non c'è |
| P2 | Adapter nullo su un pacchetto con i densi in Q8_0 | `bin/basalt --model MODEL_Q8_0.basalt --lora ADAPTER_NULL.gguf --tokens-file data/bench/conv.ids --greedy --max-new 64`, confrontato con P1 sullo stesso pacchetto | le due uscite greedy | identiche (R1: un delta nullo su una base Q8_0 copia i byte, non li ricalcola) |
| P3 | Lo stesso adapter a scala 1 e a scala -1 insieme, sullo stesso pacchetto Q8_0 | `bin/basalt --model MODEL_Q8_0.basalt --lora-scaled ADAPTER.gguf:1.0,ADAPTER.gguf:-1.0 --tokens-file data/bench/conv.ids --greedy --max-new 64`, confrontato con P1 | le due uscite greedy | identiche (la cancellazione è esatta, come nel test CPU) |
| P4 | Adapter non nullo, due avvii dello stesso comando | `bin/basalt --model MODEL.basalt --lora ADAPTER.gguf --tokens-file data/bench/conv.ids --greedy --max-new 64` (due volte) | le due uscite tra loro, poi contro P1 | le due uscite di questo punto sono identiche tra loro; diverse da P1 (l'adapter arriva davvero ai kernel) |
| P5 | Adapter nullo sul pacchetto vero dell'utente (densi non Q8_0) | `basalt-bench --quality --model MODEL.basalt --lora ADAPTER_NULL.gguf` e lo stesso senza `--lora` (basalt-bench inoltra i flag che non conosce al motore) | PPL/KLD delle due corse | dentro il rumore di un giro da 20 pezzi di `basalt-bench --quality`: misura il secondo arrotondamento, non una perdita vera |
| P6 | Dalla riga di log `lora: N tensori fusi, +X MiB, Y s` | avviare con `--lora` e senza, leggere la riga e `nvidia-smi` prima/dopo il caricamento | MiB di VRAM dei densi, posti della cache esperti persi, token al secondo (con e senza `--lora`) | i numeri che servono per la decisione D3 dell'utente (quanta VRAM in meno è accettabile) |
| P7 | Tipi misti nello stesso layer: un adapter che copre solo `attn_q`/`attn_v`, un altro solo `attn_qkv` | `bin/basalt --model MODEL.basalt --lora ADAPTER_PARTIAL_QV.gguf --tokens-file data/bench/prefill.ids --greedy --max-new 32`, poi lo stesso con `ADAPTER_PARTIAL_QKV.gguf`, poi con `--spec 8` acceso per passare dal verify | che il motore parta e risponda, su prefill lungo e su un turno di verifica | nessun arresto, nessun tipo misto respinto dai kernel fusi di decode/verify/prefill |

Note per chi fa la prova: P2/P3 hanno senso solo su un pacchetto i cui densi sono già Q8_0 (altrimenti R1 non si
applica, e un delta nullo comunque arrotonda una volta — è il caso di P5, non di P2/P3). Se il pacchetto
dell'utente non è Q8_0, P2/P3 vanno fatte su un pacchetto di prova diverso, solo per questo controllo.
