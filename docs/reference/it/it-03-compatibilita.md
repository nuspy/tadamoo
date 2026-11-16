> Riferimento: [README](../../../README.it.md).

## Compatibilità

- **Motore Basalt-Plus** (file `.basalt`, Qwen3.8-Flash-Next e il percorso Qwen3.6): solo NVIDIA Blackwell 12.x, Windows x64
  e Linux x86-64. I suoi kernel sono compilati per la compute capability 12.0, e il modulo FP4 per tutta la famiglia 12.x,
  quindi una compilazione gira su una versione minore successiva come la 12.1 del DGX Spark; su una Spark non è stata fatta
  nessuna esecuzione. CUDA 13.1 è il toolkit documentato; la 13.4 lo compila e supera i programmi di parità dei kernel, senza
  differenze di velocità misurabili sul modello provato.
- **Motore Strata** (secondo motore del Loader, installato da una cartella locale; legge anche i modelli Qwen3.6 e DeepSeek
  V4.1): NVIDIA RTX 40 (compute capability 8.9), RTX 30 (8.6) e RTX 20 (7.5), Apple (Metal), AMD (HIP), Intel (SYCL) e
  macchine a memoria unificata. Il Loader sceglie la scheda dall'hardware che rileva: RTX 30/40, Mac, AMD, Intel, Memoria
  unificata. Eseguito e verificato qui solo su una GPU NVIDIA Blackwell con un GGUF Qwen3.6 che ha lo strato MTP; le schede
  per l'altro hardware ci sono e sono provate con un server finto, ma su quelle macchine non è stata fatta nessuna
  esecuzione. Un DGX Spark (compute capability 12.1) è riconosciuto come memoria unificata e gli si offre Strata
  oltre a Basalt-Plus (una compilazione per 12.x gira su una versione minore successiva); nessuna esecuzione su una Spark è
  stata fatta. Una versione per Mac non è compilata: chi ha un Mac
  compila dal repository.

## Avviare TaDaMoo Win+GUI

```powershell
.\Avvia-TaDaMoo.ps1                                             # l'app (-gui): prima il Loader, nulla caricato
.\Avvia-TaDaMoo.ps1 -browser                                    # il server legato a questa finestra, l'interfaccia nel browser
.\Avvia-TaDaMoo.ps1 -server --config "Machine Default"          # staccato, senza interfaccia: torna quando il modello è caricato
.\Avvia-TaDaMoo.ps1 -server --model "D:\Modelli\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --port 7240 --vram-libera 2
.\Avvia-TaDaMoo.ps1 -prova                                      # tutti i controlli, nulla avviato, nulla scritto
.\Ferma-TaDaMoo.ps1 --port 7240                                 # ferma solo quel server (il suo albero di processi)
```

Ogni opzione si accetta con uno o due trattini e in maiuscolo o minuscolo (`-gui`, `--gui`, `-GUI`).

| opzione | significato |
|---|---|
| *(nessuna)* = `-gui` | apre `TaDaMoo.exe` passandogli `--config`, `--model`, `--port` e `--vram-libera`; il browser **non** si apre, l'indirizzo si stampa e resta disponibile. Senza l'app compilata lo script dice come compilarla e usa `-browser` |
| `-browser` | il server legato a questa finestra (Ctrl+C o chiudere la finestra ferma server e motore e libera la RAM), l'interfaccia aperta nel browser |
| `-server` | il server da solo, staccato, senza app né browser; lo script torna quando `/health` dice che il modello è caricato. Per gli agenti; lo ferma `Ferma-TaDaMoo.ps1` |
| `--config "<profilo>"` | il profilo di caricamento; senza, il profilo predefinito (sotto) |
| `--model "<file .basalt>"` | il modello da servire, con il percorso completo |
| `--port N` | la porta; vince su quella del profilo (predefinita 7232) |
| `--vram-libera GB` | la VRAM da lasciare libera per gli altri programmi, in GB (`1.5` o `"1,5"`), al posto di quella del profilo; `--vram-reserve-mib N` è lo stesso in MiB |
| `-converti` | con `-browser` / `-server`, quando il modello è un GGUF da convertire: mostra il piano, chiede, converte, poi serve il file nuovo (nell'app: **Converti…** nel Loader) |
| `-riusa` / `-riavvia` | con `-server`, la risposta data in anticipo quando un server è già acceso con altri parametri di caricamento: riusarlo, o fermarlo e riavviarlo con i nuovi (sotto) |
| `--lang it\|en` | la lingua dei messaggi per questo avvio (imposta anche `TADAMOO_LANG` per il server e l'app che avvia) |
| `-prova` | tutti i controlli, nulla avviato; `-browser` / `-server` lavorano su una copia del file di configurazione |
| `-file FILE` | il file di configurazione (predefinito `tadamoo_config.json` accanto allo script; l'app ne crea uno senza modello in un'installazione nuova) |

`-Sfondo` e `-SoloServer`, i vecchi nomi, funzionano ancora come `-server` e stampano una nota. `Ferma-TaDaMoo.ps1`
accetta `--port N` e `--file FILE` allo stesso modo.

**Quando un server è già acceso** (uno per installazione, su qualunque porta):

- `-gui` apre l'app, che chiede **Collegati al server in esecuzione** o **Cambia modello** (ferma solo l'albero di
  processi di quel server, poi si sceglie nel Loader).
- `-browser` apre il browser su quello; un `--model` o `--config` diverso non si applica, e lo script lo dice.
- `-server` risolve i parametri di caricamento chiesti (`--config`, altrimenti il profilo predefinito; `--model`;
  `--vram-libera`; `--port`) esattamente come per un avvio nuovo e li confronta con quelli del server acceso (file del
  modello, ogni impostazione di caricamento, porta).
  - **Uguali**: stampa indirizzo, modello e profilo e torna (codice d'uscita 0).
  - **Diversi**: elenca le differenze (`engine.max-context: 65536 -> 131072`) e chiede **Riusa il server avviato** o
    **Riavvia con i nuovi parametri**: in una finestra quando c'è un desktop, altrimenti nella console (`R` / `A`).
    Senza risposta entro **60 secondi** (si cambiano con `TADAMOO_ASK_TIMEOUT_S`), o senza nessuno a cui chiedere (un
    agente con l'input rediretto), il server acceso **si riusa**: nessun server viene fermato senza una risposta.
    Riavviare ferma solo l'albero di processi di quel server (`Ferma-TaDaMoo.ps1 --port N`) e lo riavvia sulla stessa
    porta, se `--port` non ne chiede un'altra.
  - **`-riusa`** e **`-riavvia`** danno la risposta in anticipo, per agenti e attività pianificate; con `-prova` lo
    script dice soltanto cosa farebbe.

**Quale modello parte** con `-browser` / `-server`: il primo impostato fra: `--model`; il modello salvato nel profilo
di caricamento; il **modello predefinito del server** (scelto in **Impostazioni → Modello predefinito per il
server**); il modello già nel file di configurazione. Se nessuno è impostato lo script si ferma con codice 1 e dice
come sceglierne uno. Il controllo della memoria usa lo stesso piano del Loader; un avvio che farebbe crescere il file
di paging viene rifiutato, come una GPU che il motore non supporta (codice 1).

**Il profilo di caricamento predefinito**: quello scelto con **Rendi predefinito** nel Loader, altrimenti **Machine
Default** dopo che la configurazione guidata l'ha salvato, altrimenti **Valori sicuri**.

Direttamente, su Windows o Linux: `bin\basalt.exe serve --config tadamoo_config.json [--port N]` (`bin/basalt serve` su
Linux; ogni opzione è nel README originale, [Settings](../upstream-basalt.md#settings---config), e in `python -m serve.server
--help`).

### L'app

`tools\windows\desktop\publish\TaDaMoo.exe` (finestra e icona: «TaDaMoo Win+GUI»; il nome del file è diverso da
quello del motore, `basalt.exe`, di proposito; le stesse opzioni dello script: `--config`, `--model`, `--port`,
`--vram-libera`, `--file`, `--lang`). All'apertura avvia il server nel suo job (finisce con l'app) **senza caricare un
modello** e mostra il **Loader** web:

1. la prima volta, le cartelle dei modelli (**Sfoglia…** apre la finestra di Windows per scegliere una cartella; sono
   proposti i posti abituali trovati su questo PC);
2. il modello, il suo stato, **Verifica** e **Converti…**;
3. finché questo PC non ha un Machine Default, viene proposta la **configurazione guidata** (si può saltare, o
   spegnere);
4. il profilo di caricamento e ogni valore, la VRAM da lasciare libera, la previsione, poi **Carica**.

Caricare un modello diverso da quello con cui il server è partito lo salva e riavvia il server con quello; la pagina si
ricollega da sola. Chiudere la finestra la nasconde nell'icona vicino all'orologio: **Apri**, **Ricarica con altri
parametri…** (il Loader), **Avvia solo server (per gli agenti)…** (come `Avvia-TaDaMoo.ps1 -server`, al posto del server
dell'app), **Spegni**. Se il server non parte (una porta occupata, una build mancante), l'app mostra il motivo, la fine
del registro e un pannello di avvio nativo come riserva. Con il server acceso la stessa interfaccia è a
`http://127.0.0.1:7232/` in qualunque browser.

### Configurazione guidata e Machine Default

**Configurazione guidata** nel Loader (anche `python -m serve.loader wizard` per gli agenti, sotto) trova i valori
migliori per questo PC; l'app distribuita su altri PC la usa per adattarsi a ogni macchina. Perché: i valori giusti
dipendono da VRAM, RAM, limite di commit, core e da quale scheda mostra il desktop. Valori fissi sprecherebbero
velocità su un PC grande o non si caricherebbero su uno piccolo (o farebbero crescere il file di paging). La
configurazione guidata calcola ogni valore da quello che rileva, dice perché, e può verificarlo con una misura vera.

1. **Questo PC**: RAM, limite di commit e quanto è usato, file di paging (quelli gestiti da Windows possono crescere:
   TaDaMoo Win+GUI non li fa mai crescere), core performance ed efficienza, la GPU NVIDIA, e le schede video con quella
   che mostra il **monitor principale**. Un PC può comandare il monitor da un'altra scheda (una GPU integrata, per
   esempio) e tenere la GPU NVIDIA per il calcolo; la GPU può avere anche un monitor **secondario**, acceso o spento.
   La configurazione mostra cosa ha rilevato e chiede di confermarlo o correggerlo.
2. **Cosa lasciare agli altri programmi**: la quota di RAM e commit (15% proposto, dal 5 al 50%, mai meno di 2 GB;
   diventa la regola di questo PC per ogni piano, si cambia poi in **Impostazioni → Memoria e avvio**) e la VRAM: 0,5 GB per il driver, più 1,5 GB quando la GPU comanda il
   monitor principale (o circa 0,4 GB per uno secondario), arrotondati per eccesso a 256 MiB. Quello che gli altri
   programmi usano ora sulla GPU si mostra, non si somma: la cache degli esperti si dimensiona sulla VRAM libera al
   momento del caricamento, che lo lascia a loro.
3. **Il modello** e i suoi valori per la chat.
4. **I valori calcolati**, con i motivi e la previsione: cache KV q4_0, prefill auto, il drafter a spec 7
   quando il file ce l'ha, esperti residenti e tabella PLE in RAM quando ci stanno, i thread della CPU dal processore,
   il contesto più lungo che ci sta.
5. **Misura** (facoltativa, circa 5-10 minuti, solo con meno del 75% del limite di commit in uso): i valori si
   caricano, la memoria vera dopo il caricamento si confronta con il piano (un contesto più corto, poi niente esperti
   residenti, finché è sopra), si calibra la velocità e si provano
   le finestre massime di verifica 5, 7 e 8 (si fissano all'avvio del motore: un ricaricamento ciascuna); si tiene la più
   veloce (a parità, la finestra più piccola) e si mostrano tutti i risultati.
6. **Salva**: il profilo di caricamento **Machine Default** (con il modello; diventa il predefinito) e il preset della
   chat **Machine Default** (i valori di campionamento del modello). **Ripristina Machine Default** li rimette: nel
   Loader accanto a **Ripristina valori sicuri**, e nelle impostazioni della chat accanto a **Valori del modello**.

Senza domande, per gli agenti (le risposte rilevate e proposte, salvo quelle date; la misura richiede il Loader):

```powershell
.venv\Scripts\python -m serve.loader wizard --model "D:\Modelli\Qwen3.8-Flash-Next-IQ3_XXS.basalt" `
    --monitor none --free-share 0.15 --vram-libera 2 --save        # --json per un solo oggetto JSON
```

**Le impostazioni della chat** restano di ogni client: l'app e ogni browser hanno le loro. Un client si apre con il
preset usato per ultimo, se esiste ancora; altrimenti segue il predefinito di adesso (Machine Default, altrimenti i
valori del modello). Le impostazioni cambiate a mano restano, e il pannello dice quale preset è in uso.

### Modelli: cartelle, conversione, verifica

**Impostazioni → Cartelle dei modelli** contiene una o più **cartelle dei modelli** (il Loader le chiede la prima
volta), per esempio quella dei tuoi file `.basalt` e quella dei download di LM Studio; **Impostazioni → Modello
predefinito per il server** è il modello che gli script e l'app avviano senza `--model`. Il Loader sceglie fra questi
il modello da caricare. Ogni `.basalt` e `.gguf` al loro interno viene descritto dalla sola
intestazione e messo in cache; nessuno legge i pesi. Ogni modello ha uno stato:

- **Pronto**: un file `.basalt` che il motore può aprire. **Verifica** controlla lo SHA-256 di ogni sezione in
  sottofondo.
- **Da convertire**: un insieme GGUF di Qwen3.8-Flash-Next con tutte le parti presenti e formati degli esperti che i
  kernel della GPU coprono (per esempio IQ3_XXS, IQ3_S, Q2_0, Q8_0 e le quantizzazioni UD di Unsloth). **Converti…**
  mostra prima una finestra con la dimensione del file nuovo, il disco e il suo spazio libero; parte solo dopo quella
  conferma, con spazio sufficiente e il commit sotto il 75% del limite. Il proiettore per le immagini (`mmproj`) nella
  stessa cartella entra nel file; il drafter MTP viene da una cartella già sul disco o si scarica dal checkpoint Qwen
  (`mtp_fetch.py`). I valori di campionamento consigliati dal GGUF d'origine (`general.sampling.*`) diventano i
  predefiniti della chat del file; per i file convertiti prima, il server li legge dalla copia dell'intestazione
  d'origine conservata nel file.
- **Non compatibile**: tutto il resto, con il motivo (un'altra architettura, formati degli esperti senza kernel come
  gli esperti Q2_K statici o i down Q4_0, una parte mancante, un download non finito). I file del proiettore e del
  drafter sono elencati come allegati del loro modello.

Lo stesso da un terminale: `python -m serve.models list | json | dirs | set-dirs DIR ... | default --set FILE |
resolve [--model FILE] [--profile NOME] | convert --model GGUF [--out DIR]`.

### Download dei modelli

Il pannello **Scarica modelli** del Loader offre Qwen3.8-Flash-Next nelle versioni che girano su Basalt-Plus: 23 file da
Hugging Face (e altri 6 mostrati come non compatibili, con il motivo). Ogni scheda dice che cos'è il file e in cosa
differisce dal modello base, la dimensione, la quantizzazione e quanto è vicino al modello pieno con la **fonte di
quella misura** (la misura di Basalt-Plus contro Q8_0, quella degli autori, quella di Strata o nessuna: i riferimenti
sono diversi e non vanno mescolati). Il selettore dei modelli mostra lo stesso suggerimento di una riga per un modello
che conosce.

- **File `.basalt` pronti** (nessuna conversione; `jesdga/Qwen3.8-Flash-Next-Basalt`: Q2_0, IQ3_XXS, IQ3_S,
  UD-Q4_K_XL, Q8_0; e un uncensored di terzi) si caricano appena il download finisce.
- **Insiemi GGUF** (ISTA-DASLab GSQ-RCO IQ2_XS e il Coder, Swift 1.5, Unsloth UD, gli uncensored di SC117 e di
  OrcaRouter) si scaricano con il proiettore di visione e poi compaiono come **Da convertire**; la conversione è
  quella descritta sopra, con la sua conferma. La cartella dei GGUF si può poi cancellare.
- Le versioni **uncensored** sono segnalate e portano l'avviso dell'autore. Il repository di OrcaRouter è **ad accesso
  controllato**: il pannello spiega come avere un account Hugging Face, accettare i termini e creare un token di sola
  lettura, e prende il token solo per la sessione (si leggono anche `HF_TOKEN` e il file scritto da
  `huggingface-cli login`). TaDaMoo non salva, non mostra e non registra mai un token, e lo invia solo a Hugging Face,
  mai lungo un redirect verso un altro host.
- **Un download parte solo dal suo piano**: i file a una revisione fissata con dimensioni e SHA-256 come li dà
  Hugging Face, il disco e il suo spazio libero, l'accesso e la licenza da accettare (Qwen Community License 1.0, più
  la Swift Open License per i file Swift; i repository che si dichiarano Apache-2.0 sono trattati con la licenza del
  modello base). I file sono scritti come `.part` e rinominati solo quando lo SHA-256 corrisponde; un download
  interrotto o fermato riprende da dove si era fermato.
- **Non offerti**: Unsloth UD-Q5_K_XL, UD-Q6_K_XL e UD-IQ1_S, e gli IQ2_XXS e Q2_K/Q3_K di OrcaRouter, perché i loro
  formati degli esperti non hanno un kernel in Basalt-Plus; e `Qwen3.8-35B-A3B`, che non è un modello Qwen ufficiale ma una
  distillazione della community in un'altra architettura (Qwen3.6-35B-A3B: 40 strati, dimensione 2048, 256 esperti)
  che il motore, scritto per le forme di Flash-Next, non può eseguire.

Le voci "Non provato con Basalt-Plus" hanno formati degli esperti che gli elenchi del motore accettano ma che non sono mai
stati eseguiti con Basalt-Plus. Il catalogo è stato letto dai repository il 17/10/2026 (`serve/catalog.py`); le dimensioni
si rileggono quando si prepara un download. Le rotte (solo da questo PC e dalla pagina del server): `GET
/api/models/catalog | download | hf`, `POST /api/models/download-plan | download | download-cancel | hf-token`.

### Convertitore di modelli (Modelli > Convertitore di modelli)

Converte un set GGUF in un file `.basalt` con le scelte che decidono come si comporterà: il **profilo degli esperti** (quello trovato accanto al
modello, nessuno, un file o quello di un altro modello), il **drafter** (il livello originale di Qwen, nessuno, oppure il livello affinato
sull'output del modello stesso che hanno i pacchetti ufficiali, letto dal pacchetto con richieste a intervalli e non scaricato intero), il
**proiettore delle immagini** (quello della cartella, nessuno o un file) e l'arrotondamento a BF16. Il piano mostra il comando, i passi, la
dimensione e il disco, e rifiuta una scelta sbagliata prima di scrivere qualsiasi cosa.

Accanto, ogni modello pronto si può **provare** in un server a parte con le stesse impostazioni per tutti (così i numeri si confrontano): la
memoria occupata al caricamento (RAM, commit, VRAM) e il tempo di caricamento, la velocità di scrittura con campionamento esatto e con bozze
rilassate 0,5, 0,3 e 0,2 (e senza drafter), la velocità di lettura del prompt, una richiesta con immagine e da 100 a 300 domande di matematica
(gsm8k) valutate sul numero finale. La prova richiede la GPU: un modello caricato viene prima scaricato, e il controllo del Loader deve dire che
il modello ci sta. Una prova già fatta con le stesse impostazioni viene **proposta per il riuso** (un dialogo chiede, passo per passo: riusa o
rifai); i risultati restano nella cartella dei dati dell'utente (`bench`) con le impostazioni e il limite di potenza con cui sono stati misurati,
e il risultato di qualità è condiviso dai file con gli stessi pesi. Il confronto mostra i modelli spuntati e i dati pubblicati dei pacchetti
ufficiali come riferimento, come **stella** (ogni asse va dal peggiore al migliore dei modelli mostrati, il bordo esterno è sempre il migliore),
**barre** (un pannello per ogni misura, valori in punta), **punti** (due misure una contro l'altra, una terza come dimensione) o **tabella**;
ogni segno ha gli stessi numeri nel testo al passaggio, e un modello conserva il suo colore.

### VRAM da lasciare libera

**VRAM da lasciare libera** (nel Loader e nella configurazione guidata; `engine.vram-reserve-mib`,
salvata nei profili) è la VRAM che il motore non tocca, per desktop, browser, driver e altri programmi. Con la cache
degli esperti su `auto`, tutto il resto contiene esperti. Spostare il controllo ricalcola, dal vivo: la VRAM che resta
per gli esperti, quanti esperti ci stanno (per esempio «13.066 di 24.576 esperti in VRAM (53%)»), la percentuale
attesa di esperti trovati in cache, le velocità previste di risposta e di lettura del prompt, e il commit che il
caricamento richiede.

- **Il compromesso**: più VRAM libera vuol dire meno esperti sulla GPU (un po' più lento, perché quelli mancanti li
  calcola la CPU) e, su Windows, meno commit (la VRAM ci conta). Meno VRAM libera è più veloce, finché il desktop o un
  altro programma non resta a corto.
- **Come scegliere**: la regola della configurazione guidata qui sopra; la pagina dice quali programmi usano la GPU e
  avvisa quando resta meno di 1 GB libero su una GPU che comanda il monitor principale. Lasciane di più mentre giochi o
  monti video sulla stessa GPU.

**Memoria misurata.** Dopo ogni caricamento vero il server confronta RAM e commit presi davvero (le letture del sistema
prima e dopo) con il piano degli stessi valori, e conserva la differenza con le calibrazioni del modello; da lì in poi
i piani di quel modello aggiungono quello che era in più (mostrato come «misurato all'ultimo caricamento»).

### «Valori sicuri»: calcolati, non fissi

Il profilo incorporato si calcola ogni volta dall'hardware trovato e dal modello scelto, con le stesse regole di ogni
piano e della configurazione guidata: la quota di RAM e commit di questo PC resta libera; la riserva di VRAM è quella
della configurazione guidata (o la sua regola, con 1 GB per il monitor finché la configurazione non conferma dov'è:
1,5 GB in tutto); la cache KV è q4_0 e il contesto il più lungo che ci sta (fino a 262.144); il drafter è acceso (spec 7)
solo se il file ce l'ha, le immagini solo se ha un proiettore; esperti residenti e tabella PLE in RAM solo se
ci stanno; i thread della CPU sono i core performance più metà dei core efficienza, meno uno (su un processore senza
core efficienza, i core fisici meno uno). Ogni valore ha il suo motivo nel Loader. Per esempio, su una RTX 5090 (32 GB,
1 GB usato da altri programmi) con 128 GB di RAM e un processore con 8 core performance e 16 efficienza, il file a 3 bit
dà: 262.144 token, q4_0, spec 7, cache esperti auto, esperti residenti, 1,5 GB lasciati liberi, 15 thread
CPU, immagini dynamic.

### Dove stanno le impostazioni

Per utente, mai nel repository, nella **cartella dei dati dell'utente**: `%APPDATA%\TaDaMoo\` su Windows,
`~/Library/Application Support/TaDaMoo/` su macOS, `$XDG_DATA_HOME/tadamoo/` (`~/.local/share/tadamoo/`) altrove, oppure
`TADAMOO_DATA`. Contiene `profiles.json` (profili di caricamento, preset di inferenza, il profilo predefinito),
`models.json` (le cartelle dei modelli, il modello predefinito del server, l'ultima cartella di uscita), `machine.json`
(le risposte della configurazione guidata: quota di RAM e commit, VRAM da lasciare libera, dov'è il monitor),
`calibration\<modello>.json` (velocità e memoria misurate), `prefs.json` (la lingua) e `desktop.json` dell'app. Le cache
che si possono ricostruire restano in `%USERPROFILE%\.cache\tadamoo\` (`~/.cache/tadamoo/`, o `TADAMOO_CACHE`): la
scansione delle intestazioni, i registri dei lavori di conversione e verifica, i file del tokenizer e delle immagini
estratti da un modello, il registro del motore. Le versioni precedenti tenevano le impostazioni nella cartella della
cache: la prima volta vengono copiate (non spostate) nella cartella dei dati. `tadamoo_config.json` accanto agli script
lo scrivono loro e il Loader dal profilo scelto, e non è tracciato da git.
