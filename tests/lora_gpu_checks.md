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
  con un vero addestramento per questa prova). Per P2b, un adapter nullo sul solo tensore Q8_0 trovato per nome nel
  pacchetto GSQ-RCO-IQ3_S (vedi sotto). `ADAPTER_NULL.gguf` serve anche per P9. Si può provare fino al file GGUF
  senza GPU: farlo ora.
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

Il risultato è per singolo tensore (`blk.N....weight`), non per famiglia: due layer della stessa famiglia possono
avere tipi diversi. L'adapter nullo di P2b va costruito sui tensori che risultano Q8_0 per nome esatto, non su
un'intera famiglia assunta uniforme.

Eseguito il 22 novembre 2026 sui tre pacchetti presenti su questa macchina:
- `E:\Modelli\basalt\v2\Qwen3.8-Flash-Next-Uncensored-IQ4_XS.basalt` (quello che l'utente usa oggi): nessun tensore
  delle dieci famiglie è Q8_0 (IQ4_XS, Q5_K o IQ4_NL secondo la famiglia). **P2b si salta su questo pacchetto.**
- `E:\Modelli\basalt\v2\Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.basalt`: quasi tutto Q4_K/Q5_K/Q6_K/IQ4_XS/IQ4_NL, **ma
  un solo tensore è Q8_0** (`ffn_down_shexp.weight` di un layer, 1 su 48) — l'unico confronto disponibile oggi
  contro il modello senza adapter su un pacchetto `qwen4exp` reale: vedi P2b.
- `E:\Modelli\basalt\v2\Qwen3.6-35B-A3B-NVFP4.basalt` **non serve a P2/P3**, anche se tutti i suoi densi sono Q8_0:
  è un altro modello (`qwen35moe`), con un percorso del motore tutto suo (`src/qwen35`, deciso in
  `src/program/cli/startup.cpp` prima che `configure.cpp` — dove vive `load_lora` — esista). `--lora` su quel
  pacchetto non arriva mai a `lora_merge_q8_0`: in esecuzione diretta cade nel ramo `else` finale del parser di
  `q36_engine.cu` ed esce con errore 2 (`usage: --model GGUF ...`); nel percorso del server (`--serve`, come lo
  lancia davvero l'utente) cade invece nel ramo che ignora i flag che quel motore non ha, con un avviso nel log e
  nessun arresto — l'adapter non si applica e nessun messaggio arriva al server oltre quella riga. Resta un fatto
  da dire all'utente (sezione sotto), non un modo per provare P2/P3: su quel pacchetto «uscite identiche» sarebbe
  vera per forza, perché la fusione non gira affatto.

Il pacchetto `Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt` nominato in `basalt_config.json` **non esiste più su
questo disco** (cercato il 22 novembre 2026, solo in `E:\Modelli\basalt\`): se serve per un'altra prova, va
rigenerato o il percorso in `basalt_config.json` va corretto — non è stato ricostruito qui.

### P5, ritirata: perché, e cosa farci se un giorno si vuole il numero

`basalt-bench --quality` si ferma con `the quality gate needs --kld-base FILE` senza un file `.kld` di llama.cpp,
che richiede llama.cpp compilato e il GGUF BF16 intero di Unsloth (molto disco, una corsa lunga) — cercato su
questo PC il 22 novembre 2026 (unità C ed E, cartelle dei modelli): **nessun file `.kld` esiste**. L'utente ha
deciso (22 novembre 2026) di fare il merge senza quel numero: coerente con la sua decisione D6 (parità sui tensori
più adapter nullo, nessuna misura di qualità promessa in `docs/precision/features.md`). Se un giorno servirà
davvero, la ricetta è in `docs/precision/builds.md`:

```sh
llama-perplexity -m Qwen3.8-Flash-Next-BF16-00001-of-00008.gguf -f wikitext-2-raw/wiki.test.raw \
    --kl-divergence-base flashnext-bf16-base.kld -c 512 -b 8192 -ub 8192 --chunks 100 -ngl 99 --n-cpu-moe 99 -t 16
```

## Le prove sul modello vero (serve GPU + l'utente al PC)

`MODEL.basalt` è il pacchetto reale che userai (IQ3_XXS o IQ4_XS: densi in tipi misti, non Q8_0). "Il binario di
prima" **non** è `bin/basalt.exe` dell'albero principale così com'è: `master` su questa macchina si muove in
fretta per lavoro indipendente da questo (oltre 50 commit non-LoRA tra il punto in cui questo branch è partito e
oggi, 22 novembre), quindi il binario dell'albero principale non è un confronto pulito — la prima volta che l'ho
provato ha dato un'uscita diversa che sembrava una regressione e non lo era. Il binario di prima si compila da
`git merge-base master worktree-lora-convert` in un worktree a parte (per esempio
`git worktree add .claude/worktrees/lora-baseline <quell'hash>`, build pulita lì); "questo" è `bin/basalt.exe` di
questo worktree. Confrontati così, senza `--lora`, danno la stessa uscita identica — vedi P1.

**Tutte le prove di identità vanno fatte con gli stessi flag del motore che usi davvero** — `--kv`,
`--expert-cache`, `--prefill`, `--resident-experts` dal tuo config reale — non solo con
`--tokens-file`/`--greedy`/`--max-new` di questa tabella: con flag diversi cambia la disposizione degli esperti e
con essa gli ultimi bit, e un confronto tra due disposizioni diverse non prova nulla sul collegamento LoRA.
**Eccezione trovata il 22 novembre: `--spec 7` (il valore vero del tuo config) non è deterministico da solo** —
due avvii dello stesso identico binario, stessi flag, stesso prompt, `--greedy`, danno uscite diverse a partire da
un certo token (confermato due volte). `--spec 2` invece è stabile (due avvii identici, ripetuto). Le prove che
chiedono byte identici (P1, P2b, P3b, P4, P9) vanno fatte con `--spec 2`, non `--spec 7`: è l'unico modo di avere
un confronto che significhi qualcosa, al costo di allontanarsi dal tuo config reale su questo solo flag. P6 e P7
(VRAM/velocità, tipi misti) possono e devono usare `--spec 7` vero, perché non chiedono byte identici.
`data/bench/conv.ids` e `data/bench/prefill.ids` sono i prompt pretokenizzati già nel repository. Etichetta ogni
numero col limite di potenza della GPU.

| # | Cosa si fa | Comando | Cosa si confronta | Esito atteso |
|---|---|---|---|---|
| P1 | Binario di prima (dal merge-base, non dall'albero principale) contro questo, senza `--lora` | `bin\basalt.exe --model MODEL.basalt --tokens-file data/bench/conv.ids --greedy --max-new 64 --spec 2 <gli altri tuoi flag reali>` coi due binari | le due uscite greedy | **FATTO (22 novembre 2026, pacchetto IQ4_XS, i tuoi flag reali tranne `--spec 2`): identiche.** Prima prova sbagliata (confronto contro l'albero principale non al merge-base) scartata, non un esito |
| P2 | *Non eseguibile su questa macchina*: serve un pacchetto `qwen4exp` con tutti i densi già Q8_0, e non ce n'è uno (il `.basalt` Q8_0 presente, NVFP4, è un altro modello e non passa dal collegamento LoRA: vedi sopra) | — | — | **si salta e si scrive il perché**; P2b è il confronto reale che resta |
| **P2b** | **Prova principale di questo gruppo** (non facoltativa): sul pacchetto `qwen4exp` reale che ha un tensore Q8_0 (`GSQ-RCO-IQ3_S`, il layer di `ffn_down_shexp` trovato sopra), un adapter nullo che copre solo quel tensore per nome | come P1 ma con `--lora ADAPTER_NULL_Q8TENSOR.gguf` su `Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S.basalt` | contro lo stesso comando senza `--lora`, sullo stesso pacchetto; e la riga di log deve dire `+0.0 MiB` | **FATTO (22 novembre 2026): identiche, e la riga di log dice `+0.0 MiB` esattamente.** Limite confermato: copre un solo tensore su 640 colonne, quindi il percorso `native_mmvq`, non `dense_tc` |
| P3 | *Non eseguibile*, stesso motivo di P2 | — | — | si salta |
| P3b | Sul pacchetto reale, qualunque esso sia | `--lora-scaled ADAPTER.gguf:1.0,ADAPTER.gguf:-1.0` su `MODEL.basalt` | **non contro P1** — contro `--lora ADAPTER_NULL.gguf` sullo stesso `MODEL.basalt` | **FATTO (22 novembre 2026): identiche** — e la riga di log dice `lora: 10 tensors fused, +52.8 MiB over the pack's own types` come per l'adapter nullo, confermando che i byte fusi sono gli stessi nei due casi |
| P4 | Adapter non nullo, due avvii dello stesso comando | `--lora ADAPTER.gguf` su `MODEL.basalt` (due volte) | le due uscite tra loro, poi contro P1 | **FATTO (22 novembre 2026): le due uscite sono identiche tra loro**, e diverse da P1 fin dal primo token (`33 2308...` contro `760 5839...`) — l'adapter arriva davvero ai kernel, in modo deterministico |
| **P9** | Prova di tenuta sul pacchetto reale dell'utente (senza P2/P3/P5, nulla provava finora "fusione giusta" contro "fusione sbagliata ma deterministica" su un pacchetto non Q8_0): adapter nullo su tutte le dieci famiglie (ogni denso coperto passa a Q8_0) | i quattro prompt pretokenizzati di `data/bench` (`conv`, `prefill`, `prose`, `struct`), uscita greedy, con e senza `--lora ADAPTER_NULL.gguf` | le due uscite per ciascuno dei quattro prompt | **FATTO (22 novembre 2026).** `prefill` e `struct`: uscite identiche. `conv` e `prose`: uscite diverse dal secondo arrotondamento (come previsto su un pacchetto non Q8_0), ma **entrambe decodificate a mano e lette per davvero**: `conv` dà due continuazioni coerenti su B-tree contro LSM-tree, `prose` due aperture coerenti di un saggio sull'Odissea — nessuna frase senza senso in nessuno dei quattro casi |
| P5 | Qualità con/senza adapter nullo sul pacchetto reale | **ritirata (decisione dell'utente, 22 novembre 2026)** | — | nessun numero: `docs/precision/features.md` resta com'è (parità sui tensori più adapter nullo, nessuna misura di qualità promessa). Generare il file `--kld-base` costerebbe llama.cpp compilato, il GGUF BF16 intero e una corsa lunga, per un numero che non serve alla decisione |
| P6 | Dalla riga di log `lora: N tensori fusi, +X MiB, Y s` | avviare con `--lora ADAPTER.gguf` e senza, leggere la riga e `nvidia-smi` prima/dopo il caricamento | MiB di VRAM dei densi, posti della cache esperti persi, token al secondo (con e senza `--lora`) | **FATTO (22 novembre 2026, `--spec 7` vero): densi 1619,5→1672,3 MiB (+52,8); cache esperti 6609→6594 posti, 16,39→16,35 GiB; decodifica 63,5→55,6 tok/s (circa 12,5% più lenta — i tensori coperti passano tutti a Q8_0, più pesante da leggere di IQ4_XS/Q5_K)** — i numeri per la decisione D3 |
| P7 | Tipi misti nello stesso livello: un adapter che copre solo `attn_q`/`attn_v`, un altro solo `attn_qkv`, su prefill lungo e su un turno di verifica (`--spec` acceso) | `--lora ADAPTER_PARTIAL_QV.gguf --tokens-file data/bench/prefill.ids` su `MODEL.basalt`, poi lo stesso con `ADAPTER_PARTIAL_QKV.gguf`, con `--spec 7` vero | che il motore parta e risponda; **leggere il log per righe di ripiego** (per esempio `GDN qkv + z without dense_tc_pair`) e i token al secondo con e senza l'adapter | **FATTO (22 novembre 2026): nessun arresto in nessuno dei due casi.** L'unico avviso nel log (`fallback: glue off for the layer's experts (types 23 / 20)`) è lo stesso in entrambi i casi ed è degli esperti, non del collegamento LoRA — nessun ripiego causato da LoRA trovato |
| P8a | Adapter con un tensore esperto e uno router, rifiuto esplicito | `bin\basalt.exe --model MODEL.basalt --lora ADAPTER_MIXED.gguf --no-lora-partial` | il messaggio d'errore | **FATTO (22 novembre 2026, senza GPU):** si ferma prima di aprire il modello, con `touches 1 router, 1 expert and 0 other tensor(s)...`; confermato anche passando dalla traduzione vera del file di configurazione (`serve.config.engine_words('no-lora-partial', True)` → `--no-lora-partial`) |
| P8b | Lo stesso adapter, fusione parziale di default | `bin\basalt.exe --model MODEL.basalt --lora ADAPTER_MIXED.gguf` (senza `--no-lora-partial`) | la riga `basalt lora: warning: ...` | **FATTO (22 novembre 2026, senza GPU):** avviso nel log, non un arresto |

Nota: P2 e P3 restano scritte per il giorno in cui esistesse un pacchetto `qwen4exp` di prova con i densi già
Q8_0; su questa macchina non girano. P2b (sul `GSQ-RCO-IQ3_S`) e P9 (sul pacchetto reale dell'utente) sono insieme
la prova di tenuta che oggi sostituisce P2/P3/P5 sul pacchetto che l'utente usa davvero.

**Tutte le prove di questo file sono state eseguite per davvero il 22 novembre 2026, con l'utente al PC e la GPU
libera.** Adapter generati da `tools/convert/gen_test_adapters.py` (E3-6), non scritti a mano. Il collegamento
LoRA è verificato end-to-end: dal convertitore Python, attraverso il lettore C++, fino ai kernel della GPU vera.
