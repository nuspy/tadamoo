# Prove sul modello vero per il collegamento LoRA

Richiamate da `tests/QUEUE_daytime.md`. Due parti: quello che si prepara subito, senza GPU (gli adapter di prova e
l'anello Python/C++ che oggi manca), e le prove che servono la GPU e l'utente al PC. Quando l'utente è al PC il
tempo è suo: gli adapter devono esistere già.

## Da preparare adesso, senza GPU

- **Adapter di prova veri, non scritti a mano.** Uno script legge le forme dei tensori dal pacchetto reale (la
  sezione `meta` del `.basalt`, JSON, chiave `tensors`: vedi sotto — **non** `tools/gguf_reader.py`, che apre solo
  un GGUF vero e su un `.basalt` compilato non trova il magic), ricava i nomi dei moduli PEFT invertendo la tabella
  `CK.LAYER` di `tools/convert/checkpoint.py` (o `qwen35_checkpoint.py` per il pacchetto NVIDIA), scrive cartelle PEFT
  sintetiche con quei nomi e un `config.json` base (da dove si prende: dichiaralo nello script), e chiama
  `tools/convert/lora.py`. File da generare: un adapter nullo su tutte le dieci famiglie
  (`ADAPTER_NULL.gguf`), un adapter nullo che copre solo le famiglie trovate già Q8_0 nel pacchetto reale (vedi
  sotto — oggi nessuna lo è sui due pacchetti dell'utente, quindi P2b si salta: vedi sotto), un adapter non nullo su
  tutte le dieci (`ADAPTER.gguf`, un delta piccolo rispetto ai pesi — il modello deve restare sensato — ma
  sufficiente a cambiare l'uscita), un parziale solo su `attn_q`/`attn_v` (`ADAPTER_PARTIAL_QV.gguf`), uno solo su
  `attn_qkv` (`ADAPTER_PARTIAL_QKV.gguf`), e uno con almeno un tensore esperto e un tensore router (per P8, per
  esempio un `.ffn_gate_exps.<E>` e un `.ffn_gate_inp.weight`, qualunque siano — l'adapter non deve essere coerente
  con un vero addestramento per questa prova). Si può provare fino al file GGUF senza GPU: farlo ora.
- **L'anello che manca tra Python e C++.** Oggi i test C++ (`lora_merge_test`) leggono file scritti dalla fixture
  C++, e i test Python (`tools/convert/test_lora.py`) rileggono con il lettore Python: nessun test fa leggere al
  lettore C++ un file scritto davvero da `lora.py`. Un adapter GGUF piccolo (poche centinaia di byte), scritto una
  volta da `lora.py` a partire da un ingresso fisso e messo nel repository, chiude l'anello senza processi
  incrociati e senza GPU: `test_lora.py` verifica che `lora.py` lo riproduca byte per byte dallo stesso ingresso;
  `lora_merge_test` lo apre con il lettore C++ e controlla alpha, forme, valori, quote di energia e conteggi.

### Trovare le famiglie già Q8_0 nel pacchetto reale (serve a P2b)

Un `.basalt` compilato non è un GGUF: il tipo vero di ogni tensore nativo sta nella sezione `meta` (JSON), chiave
`tensors.<nome>.type_id`, non in `dense.index` (dove i tensori nativi portano sempre lo stesso segnaposto). Letto
con `tools/model_file.py`'s `ModelFile`, non con `tools/gguf_reader.py`:

```sh
python3 -c "
import sys, json
sys.path.insert(0, 'tools')
from model_file import ModelFile
from gguf_reader import GGML_TYPES
meta = json.loads(ModelFile('MODEL.basalt').read('meta'))
for suffix in ('attn_qkv.weight', 'attn_gate.weight', 'ssm_out.weight', 'attn_q.weight', 'attn_k.weight',
               'attn_v.weight', 'attn_output.weight', 'ffn_gate_shexp.weight', 'ffn_up_shexp.weight',
               'ffn_down_shexp.weight'):
    for name, t in meta['tensors'].items():
        if name.endswith(suffix):
            print(name, GGML_TYPES.get(t['type_id'], f\"type{t['type_id']}\"))
"
```

Eseguito il 22 novembre 2026 sul pacchetto reale dell'utente (`Qwen3.8-Flash-Next-Uncensored-IQ4_XS.basalt`):
nessuna delle dieci famiglie è Q8_0 (sono IQ4_XS, Q5_K o IQ4_NL secondo la famiglia). **P2b si salta su questo
pacchetto** — ripetere il controllo se si cambia pacchetto, non assumere che resti vero.

### Trovare o generare il file base per P5 (`--kld-base`)

`basalt-bench --quality` si ferma con `the quality gate needs --kld-base FILE` senza un file `.kld` di llama.cpp.
Cercato su questo PC il 22 novembre 2026 (unità C ed E, e le cartelle dei modelli): **nessun file `.kld` esiste**.
Va generato con la ricetta di `docs/precision/builds.md` (richiede llama.cpp compilato e il GGUF BF16 di Unsloth,
non solo il pacchetto `.basalt`):

```sh
llama-perplexity -m Qwen3.8-Flash-Next-BF16-00001-of-00008.gguf -f wikitext-2-raw/wiki.test.raw \
    --kl-divergence-base flashnext-bf16-base.kld -c 512 -b 8192 -ub 8192 --chunks 100 -ngl 99 --n-cpu-moe 99 -t 16
```

**P5 è oggi non eseguibile**: va detto all'utente prima della sessione con la GPU, non scoperto a metà prova — a
meno che il file non venga generato prima (richiede llama.cpp compilato e ore di CPU/GPU per il BF16, non è un
controllo rapido).

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
| P5 | Qualità con/senza adapter nullo sul pacchetto reale | **non eseguibile oggi**: manca il file `--kld-base` (vedi sopra) | — | segnata non eseguibile; da riprovare quando il file `.kld` esiste |
| P6 | Dalla riga di log `lora: N tensori fusi, +X MiB, Y s` | avviare con `--lora ADAPTER.gguf` e senza, leggere la riga e `nvidia-smi` prima/dopo il caricamento | MiB di VRAM dei densi, posti della cache esperti persi, token al secondo (con e senza `--lora`) | i numeri che servono per la decisione D3 dell'utente (quanta VRAM in meno è accettabile) |
| P7 | Tipi misti nello stesso livello: un adapter che copre solo `attn_q`/`attn_v`, un altro solo `attn_qkv`, su prefill lungo e su un turno di verifica (`--spec` acceso) | `--lora ADAPTER_PARTIAL_QV.gguf --tokens-file data/bench/prefill.ids` su `MODEL.basalt`, poi lo stesso con `ADAPTER_PARTIAL_QKV.gguf`, poi con `--spec 8` | che il motore parta e risponda; **leggere il log per righe di ripiego** (per esempio `GDN qkv + z without dense_tc_pair`) e i token al secondo con e senza l'adapter | nessun arresto; ogni ripiego trovato nel log va registrato col suo costo in token/s — un ripiego è un costo di velocità che l'utente deve vedere, non solo un «ha funzionato» |
| P8a | Adapter con un tensore esperto e uno router, rifiuto esplicito | `bin\basalt.exe --model MODEL.basalt --lora ADAPTER_MIXED.gguf --no-lora-partial` | il messaggio d'errore | **FATTO (22 novembre 2026, senza GPU):** si ferma prima di aprire il modello, con `touches 1 router, 1 expert and 0 other tensor(s)...`; confermato anche passando dalla traduzione vera del file di configurazione (`serve.config.engine_words('no-lora-partial', True)` → `--no-lora-partial`) |
| P8b | Lo stesso adapter, fusione parziale di default | `bin\basalt.exe --model MODEL.basalt --lora ADAPTER_MIXED.gguf` (senza `--no-lora-partial`) | la riga `basalt lora: warning: ...` | **FATTO (22 novembre 2026, senza GPU):** avviso nel log (non un arresto), l'esecuzione continua oltre il caricamento dell'adapter. Resta da fare con la GPU: la riga `lora: N tensors fused` e l'uscita, contro lo stesso adapter privato dei tensori fuori famiglia |

Nota: P2/P3 restano scritte per il giorno in cui esistesse un pacchetto di prova con i densi già Q8_0 (per esempio
un pacchetto piccolo fatto apposta per i test, non il pacchetto da 120+ GiB dell'utente); sul pacchetto reale di
oggi contano solo P2b (che si salta: nessuna famiglia è Q8_0, verificato sopra) e P3b.
