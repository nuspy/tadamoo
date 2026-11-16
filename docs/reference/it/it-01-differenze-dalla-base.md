> Riferimento: [README](../../../README.it.md).

## Differenze dalla base

Tutto ciò che questo fork cambia, a colpo d'occhio. Il motore, il formato `.basalt`, il convertitore e l'API
OpenAI/Anthropic sono di Basalt; il resto è aggiunto sopra, e i percorsi di codice per Linux non cambiano. I dettagli di
ogni punto sono più sotto e in [docs/WINDOWS.md](../../WINDOWS.md). Il fork include Basalt originale fino al 17/10/2026.

**1. Supporto a Windows 10/11 (il motore lì non girava)**
- Build con MSVC 14.44 + CUDA 13.1 per sm_120, con protezioni contro variabili d'ambiente che cambiano la build in modo
  silenzioso.
- Un livello di piattaforma per Windows: memoria e **budget che tengono conto del commit** (Windows addebita al commit
  ogni allocazione, VRAM compresa), `VirtualLock`, arene bloccate dimensionate sul budget di memoria condivisa del WDDM,
  mappature, letture posizionali, parallele e **non bufferizzate** con una I/O completion port, una sola chiamata a
  `PrefetchVirtualMemory` per un gruppo di esperti mappati.
- Il pool di CPU segue la topologia di Windows (`GetLogicalProcessorInformationEx`), con i **core prestazionali per
  primi** sui processori ibridi, e rimette poi a posto il CPU Set dell'host.
- `basalt serve` gira in un **job object con chiusura automatica**; letture di NVML, CPU e RAM su Windows; niente
  `SO_REUSEADDR` (un secondo server su una porta usata fallisce invece di condividerla); `--no-pdl` (programmatic
  dependent launch spento a runtime); una correzione per le copie su stream legacy che gli stream non bloccanti
  potevano leggere troppo presto sul WDDM.
- L'encoder CPU delle immagini si compila con MSVC; la suite di test si compila e gira su Windows (91 test C++ su 92
  passano; l'altro richiede una CPU con AVX-512).

**2. Un'interfaccia grafica avanzata (la base aveva solo le pagine Monitor e Hardware)**
- **TaDaMoo Win+GUI** (`TaDaMoo.exe`): un'app desktop (finestra WebView2, icona nell'area di notifica, istanza
  unica) che avvia il server e apre l'interfaccia web; la stessa interfaccia funziona in qualunque browser, in inglese o
  italiano, anche sul telefono.
- **Chat** con i valori di campionamento del modello, preset, un indicatore del contesto e politiche di contesto
  (fermarsi, scorrere o far compattare al modello), output strutturato; **Impostazioni** per le preferenze dell'app,
  accanto a Monitor e Hardware della base.
- **Loader**: ogni parametro di caricamento in un posto solo, con un **piano di memoria** (VRAM, RAM e commit necessari
  contro quelli liberi ora), una **previsione di velocità**, la calibrazione, i profili di caricamento, e il caricamento
  o la ricarica del motore dentro il server in esecuzione. **Configurazione guidata** e **Machine Default**: i valori
  migliori per questo PC, salvati come profilo.
- **LOGS**: ogni richiesta come l'ha vista il modello (cosa è arrivato, il ragionamento e la risposta come scritti,
  ogni tentativo strutturato, i tempi, perché è finita) più le righe del server e del motore, tenute in memoria ed
  esportabili. Una seconda sezione, **Tracce delle sessioni**, riproduce una conversazione di Chat o Agente con le sue
  risposte registrate (non esegue nulla), la salva in JSONL con i segreti mascherati e verifica il registro di audit
  a catena di impronte della sua cartella.
- **Scarica modelli**: un catalogo di 23 file scaricabili di Qwen3.8-Flash-Next (`.basalt` pronti, GGUF da convertire,
  versioni uncensored; altri 6 sono mostrati come non compatibili), con ciò che li distingue dalla base, quanto sono vicini al modello pieno e la fonte di quella misura),
  scaricati con ripresa e controllo SHA-256.

**3. Gestione di memoria e caricamenti**
- Piani di memoria per VRAM, RAM e commit che contano anche le copie in RAM degli slot prestati del percorso del
  prompt; la RAM e il commit **misurati** a ogni caricamento reale sono conservati per modello e aggiunti ai piani
  successivi.
- La **riserva di VRAM** dipende da quale monitor pilota la GPU; il minimo di 256 MiB del motore è imposto ovunque;
  i «Valori sicuri» sono **calcolati da questo PC e dal modello** invece che fissi.
- `--listen-first` (la pagina risponde mentre il modello si carica), `--standby` (niente caricato finché non si preme
  Load nel Loader), `--relaunch-on-model-change` (caricare un altro modello riavvia il server con quello); una ricarica
  rilegge i campionamenti di default e rifiuta un valore errato prima di fermare il motore in esecuzione.
- Un primo avvio che fallisce dice il motivo del motore; un proiettore di visione che il motore rifiuterebbe spegne
  l'input di immagini, con il motivo.

**4. Modelli**
- Una **libreria di modelli** in una o più cartelle: una scansione delle sole intestazioni segna ogni modello Pronto, Da
  convertire o Non compatibile con il motivo; **conversione e verifica** in background (prima si controllano disco e
  commit).
- Un `.basalt` può ricevere un vero **profilo degli esperti senza riconvertirlo**, e il Loader dice quando ha solo
  l'ordine di riempimento semplice. `/v1/models` elenca i file pronti delle cartelle.
- **Un secondo motore, Strata**, per ciò che Basalt-Plus non esegue: altre famiglie di GPU e i modelli Qwen3.6 e DeepSeek. Il
  Loader mostra una scheda Modelli per ogni motore che il PC può eseguire, lo installa da una cartella locale o da un
  archivio (nessun download) ed esegue il server di Strata come processo figlio gestito, inoltrandogli `/v1/*`. Un solo
  motore è attivo alla volta. Dettagli e limiti: [docs/engines-protocol.md](../../engines-protocol.md). Cosa è stato
  eseguito e cosa no: [Compatibilità](it-03-compatibilita.md#compatibilità).

**5. Server e API**
- Porta predefinita **7232** (gli esempi della base non concordavano con il suo codice); `/api/settings` legge e scrive
  il file di configurazione; `/api/loader/*`, `/api/wizard/*`, `/api/profiles`, `/api/models/*` e `/api/reqlog*` per
  l'interfaccia.
- L'output strutturato (`response_format`) riprova con l'errore rimandato indietro (`structured_retries`, default 2); il
  messaggio del tentativo dice cosa non andava e dove, il testo rifiutato non viene mai rimandato e il ragionamento scorre
  mentre solo la risposta validata è trattenuta. Il limite del corpo delle richieste e i rifiuti del server arrivano
  interi al client.

**6. Script, lingue e dati**
- `Avvia-TaDaMoo.ps1` / `Ferma-TaDaMoo.ps1`: `-gui` (predefinito), `-browser`, `-server` per gli agenti, con gli stessi nomi
  di opzione di Strata; si collegano a un server in esecuzione e controllano la memoria con il piano del Loader prima di
  caricare.
- Inglese di default, italiano su richiesta, nell'app, nella pagina, negli script e negli strumenti a riga di comando; le
  impostazioni stanno nella cartella dati dell'utente, separate dalle cache.

**7. Un harness per agenti, un planner e un guscio dell'interfaccia rivisto** (`harness/`, [docs/harness.md](../../harness.md))
- Un processo separato (`python -m tadamoo -p "compito"`, oppure come agente di un editor via ACP) che parla con l'API
  pubblica del server: un ciclo di strumenti con livelli di permesso, modalità piano, hook, checkpoint dei file, registro
  di audit a catena di hash, sandbox dei comandi, broker dei segreti, contenuti non fidati recintati, mascheramento del
  contesto, memoria, skill con approvazione per hash, MCP e documenti di riferimento (blocco fisso o ricerca). Un **vault**
  tiene cifrati carte, dati personali e accessi e il modello scrive solo segnaposto che vengono riempiti all'ultimo passo (l'agente gira come il tuo stesso utente: cosa protegge e cosa no sta in `docs/harness.md`); l'agente può guidare un browser per compilare
  moduli e prenotare, e può pagare entro i limiti che imposti nella vista **Portafoglio** (automatico fino ai massimali che scegli,
  una domanda oltre); il secondo fattore di un accesso lo scegli tu. Le telefonate vere sono protette e spente di default.
- Un **planner** proattivo: piani come grafi con decisioni, riunioni e compiti passati a te, un filtro veloce degli eventi
  (nel cloud solo con il tuo consenso, nomi sostituiti) e nuova pianificazione col modello locale, in una vista **Piani**.
- Una **valutazione** (`python -m tadamoo.eval`) che riporta pass^k, costo e uso della cache, e il guscio delle pagine:
  zoom dell'interfaccia per schermi molto larghi, una tavolozza dei comandi (Ctrl+K), viste con Alt+numero e un indice
  delle impostazioni.
- Nella Chat: **Maiusc+Tab** cambia la modalità, mostrata in un'etichetta accanto a *Nuova conversazione*; un messaggio che inizia
  con `/` è un comando (30, skill comprese); il livello di ragionamento appartiene alla conversazione; e la **memoria** adesso
  conserva davvero gli appunti tra una conversazione e l'altra, con effetti e limiti scritti per esteso. Tutto è in
  [L'harness per agenti](it-02-lharness-per-agenti.md#lharness-per-agenti) e nel [manuale](../../harness-manual.it.md).

## Come funziona

- **Un modello.** Basalt-Plus esegue Qwen3.8-Flash-Next (architettura GGUF `qwen4exp`): 48 strati, 512 esperti per strato,
  10 attivi per token. A 3 bit i soli esperti occupano circa 50 GiB, più di quanti ne tenga una GPU da 32 GB.
- **Esperti su tre livelli.** Una **cache degli esperti in VRAM** ne tiene quanti ne entrano (`--expert-cache auto`:
  tutta la VRAM che resta dopo la riserva), riempita secondo un profilo d'uso, così i più usati stanno sulla GPU; una
  **seconda GPU** può fare da livello in più (`--gpu 0,1`); il resto lo calcola la **CPU** (kernel AVX2/AVX-512). Con
  `--resident-experts` gli esperti che non sono in VRAM vengono copiati in RAM una volta sola, invece di essere letti
  dal file quando servono. L'instradamento è sbilanciato: una cache con un terzo degli esperti risponde già a circa il
  98% delle richieste.
- **Il file `.basalt`.** Un file per modello: una tabella di 64 KiB di sezioni con nome (ognuna con il suo SHA-256),
  poi pesi, tokenizer, template della chat, drafter, profilo degli esperti e proiettore per le immagini. Lo costruisce
  `tools/convert/convert.py` dai file GGUF, copiando i tensori byte per byte (nessuna riquantizzazione).
- **Il drafter MTP.** Lo strato di predizione multi-token del modello propone i token successivi e il modello
  principale li verifica in un solo passaggio; il testo è identico a quello senza drafter, perché ogni token lo decide
  la verifica. `spec 7` è una finestra di verifica di 7 token (fino a 6 proposte).
- **Prestito per il prompt.** Un prompt lungo si legge a blocchi grandi, i cui buffer sono presi in prestito dagli
  ultimi posti della cache degli esperti per la durata del prompt e poi riempiti di nuovo (`--prefill auto`).

## Cosa cambia questo fork

- **Port nativo per Windows** (dettagli in [docs/WINDOWS.md](../../WINDOWS.md)): build MSVC + CUDA 13.1, uno strato di
  piattaforma per file, mappature e letture posizionali, `VirtualLock` e limiti di memoria che tengono conto del
  **commit** (la memoria che Windows ha promesso ai programmi: ci conta ogni allocazione, VRAM compresa), arene
  bloccate secondo il budget di memoria condivisa di WDDM, letture non bufferizzate con una I/O completion port, la
  topologia della CPU con i core performance per primi sui processori ibridi, `basalt serve` in un job object che si
  chiude con lui. `--no-pdl` lancia ogni kernel senza il lancio dipendente programmatico, se un driver si comporta male.
- **L'app, TaDaMoo.exe («TaDaMoo Win+GUI»), è l'interfaccia principale.** Avvia il server senza caricare un
  modello e si apre sul Loader web nella sua finestra (WebView2): app e browser hanno esattamente le stesse
  impostazioni; un'icona vicino all'orologio tiene acceso TaDaMoo Win+GUI quando la finestra è chiusa.
- **Interfaccia web** (`/`, in inglese o in italiano, vedi [Lingua](it-04-usarlo-come-server-api.md#lingua)): Chat, Monitor, Hardware, **Carica
  modello** (il Loader) e Impostazioni. Ogni impostazione sta in una sola scheda:
  - **Loader**: tutto ciò che si decide al caricamento del modello (il modello, contesto massimo, cache KV, lettura del
    prompt, drafter MTP e la sua finestra, cache degli esperti, esperti residenti, **VRAM da lasciare libera**, tabella
    PLE, thread CPU, slot paralleli, immagini, porta), i profili di caricamento e il **profilo predefinito** (★,
    Machine Default, Valori sicuri: è qui che stanno i valori di caricamento predefiniti), la configurazione guidata,
    la calibrazione e Carica.
  - **Impostazioni**: le preferenze dell'app: lingua, cartelle dei modelli, **modello predefinito per il server**, quota
    di RAM e commit lasciata agli altri programmi, se proporre la configurazione guidata all'avvio. Una riga
    *Parametri di caricamento* mostra cosa gira adesso (per esempio "256K · q4_0 · spec 7 · 4 GB di VRAM liberi") e il
    profilo che corrisponde, con **Apri il Loader** per cambiarlo.
  - **Chat → Impostazioni** (il pannello della chat): i valori di inferenza (campionamento, ragionamento, contesto
    pieno, output strutturato) e i loro preset. **Monitor** e **Hardware** mostrano soltanto.
  - Il Loader modifica ogni impostazione del motore, mostra VRAM, RAM e commit che il piano richiede accanto a quelli
    liberi ora (una quota di RAM e del limite di commit resta sempre libera per gli altri programmi, il 15% se non ne
    scegli un'altra; il file di paging non deve mai crescere) e la velocità prevista; carica il modello, o ricarica il
    motore dentro il server acceso. La calibrazione misura le velocità vere per modello e impostazioni.
  - La **configurazione guidata** trova i valori migliori per questo PC e li salva come **Machine Default** (sotto).
  - **Profili di caricamento** (impostazioni del motore, porta, modello) e **preset di inferenza** (impostazioni della
    chat), condivisi da interfaccia, script e app; «Valori sicuri» è un profilo incorporato **calcolato per questo PC e
    il modello scelto**.
  - Chat con i **valori di campionamento del modello** (dal GGUF d'origine), **politiche di contesto** (ferma al
    limite, finestra scorrevole, o compattazione fatta dal modello) e **output strutturato** (`response_format` JSON /
    JSON Schema), controllato e rigenerato con l'errore come spiegazione (`structured_retries`, predefinito 2).
  - **Libreria dei modelli**: una o più cartelle, una scansione delle sole intestazioni che segna ogni modello
    *Pronto*, *Da convertire* (un GGUF supportato) o *Non compatibile* (con il motivo), conversione e verifica in
    sottofondo, e il modello predefinito del server.
- **Server**: porta predefinita 7232, `--listen-first` (la pagina risponde mentre il modello si carica), `--standby`
  (la pagina risponde e non si carica nulla finché non premi Carica nel Loader), `--relaunch-on-model-change` (caricare
  un altro modello chiude il server con codice d'uscita 75, così chi l'ha avviato lo riavvia con quel modello),
  `/api/settings`, `/api/loader/*`, `/api/wizard/*`, `/api/profiles`, `/api/models/*`.
- **Script** `Avvia-TaDaMoo.ps1` / `Ferma-TaDaMoo.ps1` con gli stessi nomi di opzione di Strata: `-gui` (il predefinito:
  l'app), `-browser`, `-server` (per gli agenti), `--model`, `--config "<profilo>"`, `--port`, `--vram-libera GB`,
  `-riusa` / `-riavvia`, `--lang it|en`.

### Cosa è cambiato dalla versione precedente di questo README

- **Un solo insieme di opzioni** per script e app (`-gui` predefinito, `-browser`, `-server`, `-prova`, `-converti`,
  `--config`, `--model`, `--port`, `--vram-libera`), con gli stessi nomi di Strata; i vecchi `-Sfondo` / `-SoloServer`
  funzionano ancora.
- **Prima l'app**: l'app avvia il server **in attesa** (standby: nulla caricato) e si apre sul Loader web; le cartelle
  si scelgono con la finestra di Windows; un pannello nativo fa da riserva quando il server non parte. Caricare un altro
  modello riavvia il server con quello (codice 75) e la pagina si ricollega.
- **Valori predefiniti veri**: la chat parte dai **valori di campionamento del modello** (dal GGUF d'origine); i valori
  di caricamento vengono dalle regole collaudate di «Valori sicuri», calcolate per questo PC e modello.
- **Configurazione guidata e Machine Default** (profilo di caricamento + preset della chat), con i pulsanti
  **Ripristina**, una versione da riga di comando per gli agenti, e l'ordine dei predefiniti: scelto a mano > Machine
  Default > Valori sicuri.
- **Monitor principale o secondario**: la riserva di VRAM dipende da se la GPU NVIDIA comanda il monitor principale
  (1,5 GB in più), uno secondario (circa 0,4 GB) o nessuno; quello che usano gli altri programmi si mostra, non si somma.
- **Memoria misurata**: RAM e commit presi davvero da un caricamento si conservano per modello e si sommano ai piani
  successivi.
- **Cartelle dei dati**: le impostazioni in `%APPDATA%\TaDaMoo\` (copiate una volta da `~\.cache\tadamoo`, mai
  spostate).
- **Nome**: l'app si chiama **TaDaMoo Win+GUI** (`TaDaMoo.exe`, icona nuova); motore, API, file e script tengono i
  loro nomi.
- **`-server` con un server già acceso** chiede se riusarlo o riavviarlo (`-riusa`, `-riavvia`, 60 s, senza risposta
  si riusa).
- **Inglese come lingua predefinita**, italiano a richiesta, per app, pagina, script e strumenti da riga di comando.
- **Conversione e verifica** dalla libreria dei modelli (GGUF → `.basalt`, controllo SHA-256), in sottofondo.
