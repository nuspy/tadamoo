# Prove sul modello vero per il collegamento LoRA

Richiamate da `tests/QUEUE_daytime.md`. Due parti: quello che si prepara subito, senza GPU (gli adapter di prova e
l'anello Python/C++ che oggi manca), e le prove che servono la GPU e l'utente al PC. Quando l'utente è al PC il
tempo è suo: gli adapter devono esistere già.

## Da preparare adesso, senza GPU

- **Adapter di prova veri, non scritti a mano.** Uno script legge le forme dei tensori dal pacchetto reale (sezione
  meta del `.basalt`, con `tools/gguf_reader.py`'s `GGUFFile`), ricava i nomi dei moduli PEFT invertendo la tabella
  `CK.LAYER` di `tools/convert/checkpoint.py` (o `qwen35_checkpoint.py` per il pacchetto NVIDIA), scrive cartelle PEFT
  sintetiche con quei nomi e un `config.json` base (da dove si prende: dichiaralo nello script), e chiama
  `tools/convert/lora.py`. File da generare: un adapter nullo su tutte le dieci famiglie
  (`ADAPTER_NULL.gguf`), un adapter nullo che copre solo le famiglie trovate già Q8_0 nel pacchetto reale (vedi
  sotto — serve per P2b, può restare vuoto se nessuna famiglia lo è), un adapter non nullo su tutte le dieci
  (`ADAPTER.gguf`, un delta piccolo rispetto ai pesi — il modello deve restare sensato — ma sufficiente a cambiare
  l'uscita), un parziale solo su `attn_q`/`attn_v` (`ADAPTER_PARTIAL_QV.gguf`) e uno solo su `attn_qkv`
  (`ADAPTER_PARTIAL_QKV.gguf`). Si può provare fino al file GGUF senza GPU: farlo ora.
- **L'anello che manca tra Python e C++.** Oggi i test C++ (`lora_merge_test`) leggono file scritti dalla fixture
  C++, e i test Python (`tools/convert/test_lora.py`) rileggono con il lettore Python: nessun test fa leggere al
  lettore C++ un file scritto davvero da `lora.py`. Un adapter GGUF piccolo (poche centinaia di byte), scritto una
  volta da `lora.py` a partire da un ingresso fisso e messo nel repository, chiude l'anello senza processi
  incrociati e senza GPU: `test_lora.py` verifica che `lora.py` lo riproduca byte per byte dallo stesso ingresso;
  `lora_merge_test` lo apre con il lettore C++ e controlla alpha, forme, valori, quote di energia e conteggi.

### Trovare le famiglie già Q8_0 nel pacchetto reale (serve a P2b)

```sh
python3 -c "
import sys; sys.path.insert(0, 'tools')
from gguf_reader import GGUFFile
f = GGUFFile('MODEL.basalt')
for suffix in ('attn_qkv.weight', 'attn_gate.weight', 'ssm_out.weight', 'attn_q.weight', 'attn_k.weight',
               'attn_v.weight', 'attn_output.weight', 'ffn_gate_shexp.weight', 'ffn_up_shexp.weight',
               'ffn_down_shexp.weight'):
    for t in f.find(suffix):
        print(t.name, t.type_name)
"
```

Le righe con `type_name` uguale a `Q8_0` sono le famiglie su cui P2b ha senso; se non ce n'è nessuna, P2b si salta
(scriverlo nel rapporto, non ometterlo in silenzio).

### Trovare o generare il file base per P5 (`--kld-base`)

`basalt-bench --quality` si ferma con `the quality gate needs --kld-base FILE` senza un file `.kld` di llama.cpp.
Verificare PRIMA se esiste già su questa macchina da una misura precedente (per esempio un file chiamato come
`flashnext-bf16-base.kld`, citato in `docs/precision.md`) — cercarlo, non assumere che manchi. Se manca davvero, si
genera con la ricetta di `docs/precision/builds.md` (richiede llama.cpp compilato e il GGUF BF16 di Unsloth, non
solo il pacchetto `.basalt`):

```sh
llama-perplexity -m Qwen3.8-Flash-Next-BF16-00001-of-00008.gguf -f wikitext-2-raw/wiki.test.raw \
    --kl-divergence-base flashnext-bf16-base.kld -c 512 -b 8192 -ub 8192 --chunks 100 -ngl 99 --n-cpu-moe 99 -t 16
```

Se non è pronto in tempo per la sessione con l'utente, **P5 va segnata come non eseguibile e detto all'utente prima
di cominciare**, non scoperto a metà prova.

## Le prove sul modello vero (serve GPU + l'utente al PC)

`MODEL.basalt` è il pacchetto reale che userai (IQ3_XXS o IQ4_XS: densi in tipi misti, non Q8_0). "Il binario di
prima" è `bin/basalt.exe` dell'albero principale (`E:\Projects\basalt`, compilato da `master` prima di questo
lavoro); "questo" è `bin/basalt.exe` di questo worktree (`worktree-lora-convert`) — ogni binario esegue il server
dell'albero in cui è stato compilato, non quello in cui lo lanci (vedi memoria del progetto). **Tutte le prove di
identità vanno fatte con gli stessi flag del motore che usi davvero** — `--kv`, `--expert-cache`, `--prefill`,
`--spec`, `--resident-experts` dal tuo config reale — non solo con `--tokens-file`/`--greedy`/`--max-new` di questa
tabella: con flag diversi cambia la disposizione degli esperti e con essa gli ultimi bit, e un confronto tra due
disposizioni diverse non prova nulla sul collegamento LoRA. `data/bench/conv.ids` e `data/bench/prefill.ids` sono i
prompt pretokenizzati già nel repository. Etichetta ogni numero col limite di potenza della GPU.

| # | Cosa si fa | Comando | Cosa si confronta | Esito atteso |
|---|---|---|---|---|
| P1 | Binario di prima contro questo, senza `--lora` | `bin\basalt.exe --model MODEL.basalt --tokens-file data/bench/conv.ids --greedy --max-new 64 <i tuoi flag reali>` coi due binari | le due uscite greedy | identiche: il collegamento non cambia nulla quando `--lora` non c'è |
| P2 | *Solo se esiste un pacchetto di prova con tutti i densi già in Q8_0* (il pacchetto reale non lo è: 120+ GiB per costruirne uno apposta, non farlo per questa sola prova) | come P1 ma con `--lora ADAPTER_NULL.gguf` su quel pacchetto di prova | contro P1 sullo stesso pacchetto di prova | identiche (un delta nullo su un blocco già Q8_0 è una copia di byte, non un nuovo arrotondamento); se non c'è un pacchetto così, **si salta e si scrive il perché**, non si inventa un esito |
| P2b | Sul pacchetto reale: adapter nullo che copre *solo* le famiglie trovate Q8_0 sopra | come P1 ma con `--lora ADAPTER_NULL_Q8FAM.gguf` su `MODEL.basalt` | contro P1 sullo stesso `MODEL.basalt`; e la riga di log deve dire `+0.0 MiB` | identiche, stesso motivo di P2 ma sul pacchetto vero; se nessuna famiglia è Q8_0 nel pacchetto reale, **si salta e si scrive il perché** |
| P3 | Stesso limite di P2 (serve un pacchetto con densi già Q8_0) | `--lora-scaled ADAPTER.gguf:1.0,ADAPTER.gguf:-1.0` su quel pacchetto di prova | contro P1 sullo stesso pacchetto di prova | identiche (la cancellazione è esatta, come nel test CPU); salta se non c'è un pacchetto così |
| P3b | Sul pacchetto reale, qualunque esso sia | `--lora-scaled ADAPTER.gguf:1.0,ADAPTER.gguf:-1.0` su `MODEL.basalt` | **non contro P1** — contro `--lora ADAPTER_NULL.gguf` sullo stesso `MODEL.basalt` | identiche: i byte fusi sono gli stessi nei due casi (lettura, segno della scala e somma fino ai kernel), e questo confronto vale su qualunque pacchetto, non solo uno Q8_0 |
| P4 | Adapter non nullo, due avvii dello stesso comando | `--lora ADAPTER.gguf` su `MODEL.basalt` (due volte) | le due uscite tra loro, poi contro P1 | le due uscite di questo punto sono identiche tra loro; diverse da P1 (l'adapter arriva davvero ai kernel) |
| P5 | Qualità con/senza adapter nullo sul pacchetto reale — *solo se il file `--kld-base` è disponibile (vedi sopra)* | `basalt-bench --quality --model MODEL.basalt --kld-base BASE.kld --lora ADAPTER_NULL.gguf` e lo stesso senza `--lora` | PPL/KLD delle due corse | dentro il rumore di un giro da 20 pezzi: misura il secondo arrotondamento su un pacchetto non Q8_0, non una perdita vera. Se il file base non c'è, **prova segnata non eseguibile**, detto all'utente prima |
| P6 | Dalla riga di log `lora: N tensori fusi, +X MiB, Y s` | avviare con `--lora ADAPTER.gguf` e senza, leggere la riga e `nvidia-smi` prima/dopo il caricamento | MiB di VRAM dei densi, posti della cache esperti persi, token al secondo (con e senza `--lora`) | i numeri che servono per la decisione D3 dell'utente (quanta VRAM in meno è accettabile) |
| P7 | Tipi misti nello stesso livello: un adapter che copre solo `attn_q`/`attn_v`, un altro solo `attn_qkv`, su prefill lungo e su un turno di verifica (`--spec` acceso) | `--lora ADAPTER_PARTIAL_QV.gguf --tokens-file data/bench/prefill.ids` su `MODEL.basalt`, poi lo stesso con `ADAPTER_PARTIAL_QKV.gguf`, poi con `--spec 8` | che il motore parta e risponda; **leggere il log per righe di ripiego** (per esempio `GDN qkv + z without dense_tc_pair`) e i token al secondo con e senza l'adapter | nessun arresto; ogni ripiego trovato nel log va registrato col suo costo in token/s — un ripiego è un costo di velocità che l'utente deve vedere, non solo un «ha funzionato» |

Nota: P2/P3 restano scritte per il giorno in cui esistesse un pacchetto di prova con i densi già Q8_0 (per esempio
un pacchetto piccolo fatto apposta per i test, non il pacchetto da 120+ GiB dell'utente); sul pacchetto reale di
oggi contano solo P2b e P3b.
