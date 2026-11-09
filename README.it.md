<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/tadamoo-lockup-dark.png">
    <img src="assets/tadamoo-lockup-light.png" alt="TaDaMoo" width="480">
  </picture>
</p>

# TaDaMoo: harness per agenti AI (Win+GUI)

[English](README.md) · *Italiano*

**TaDaMoo** è un harness per agenti AI con un'app nativa per Windows. Gira su **Basalt-Plus**, il suo motore di inferenza
locale per il modello mixture-of-experts Qwen3.8-Flash-Next su GPU NVIDIA Blackwell e, con un secondo motore (Strata), su
altro hardware: NVIDIA RTX serie 40, 30 e 20, Apple silicon, AMD, Intel e macchine a memoria unificata. Sito: **[tadamoo.com](https://tadamoo.com)**
· Contatto: [a.taini@gmail.com](mailto:a.taini@gmail.com) · Repository: [github.com/nuspy/tadamoo](https://github.com/nuspy/tadamoo).

**Basalt-Plus** è un fork di **[Basalt](https://github.com/jesdga95/basalt)** di jesdga95 (licenza MIT), che a sua volta è
un fork di **[Strata](https://github.com/Niko1221/Strata)** di Niko1221 (licenza MIT). Il motore, il formato `.basalt`, il
convertitore e il server vengono da Basalt; le note di copyright dei due progetti e del codice che includono (ggml,
llama.cpp e altri) sono in [LICENSE](LICENSE) e [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES). I file del modello derivano
da Qwen3.8-Flash-Next e sono sotto la
[Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE), non sotto MIT.

Cosa aggiungono TaDaMoo e Basalt-Plus rispetto a Basalt: un port nativo del motore per **Windows 10/11** (il codice per
Linux è invariato) e quello che serve per usarlo su un desktop: un'app nativa che si apre sul Loader web, che prevede
memoria e velocità prima di caricare; una configurazione guidata che trova i valori migliori per ogni PC; i profili di
caricamento; una libreria dei modelli che li trova, li verifica e li converte; l'harness per agenti con chat, planner e
benchmark; gli script di avvio e arresto. Il README originale di Basalt, in inglese, è in fondo a
[README.md](README.md#upstream-basalt-readme).

## Differenze dalla base

Tutto ciò che questo fork cambia, a colpo d'occhio. Il motore, il formato `.basalt`, il convertitore e l'API
OpenAI/Anthropic sono di Basalt; il resto è aggiunto sopra, e i percorsi di codice per Linux non cambiano. I dettagli di
ogni punto sono più sotto e in [docs/WINDOWS.md](docs/WINDOWS.md). Il fork include Basalt originale fino al 17/10/2026.

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
  motore è attivo alla volta. Dettagli e limiti: [docs/engines-protocol.md](docs/engines-protocol.md). Cosa è stato
  eseguito e cosa no: [Compatibilità](#compatibilità).

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

**7. Un harness per agenti, un planner e un guscio dell'interfaccia rivisto** (`harness/`, [docs/harness.md](docs/harness.md))
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
  [L'harness per agenti](#lharness-per-agenti) e nel [manuale](docs/harness-manual.it.md).

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

- **Port nativo per Windows** (dettagli in [docs/WINDOWS.md](docs/WINDOWS.md)): build MSVC + CUDA 13.1, uno strato di
  piattaforma per file, mappature e letture posizionali, `VirtualLock` e limiti di memoria che tengono conto del
  **commit** (la memoria che Windows ha promesso ai programmi: ci conta ogni allocazione, VRAM compresa), arene
  bloccate secondo il budget di memoria condivisa di WDDM, letture non bufferizzate con una I/O completion port, la
  topologia della CPU con i core performance per primi sui processori ibridi, `basalt serve` in un job object che si
  chiude con lui. `--no-pdl` lancia ogni kernel senza il lancio dipendente programmatico, se un driver si comporta male.
- **L'app, TaDaMoo.exe («TaDaMoo Win+GUI»), è l'interfaccia principale.** Avvia il server senza caricare un
  modello e si apre sul Loader web nella sua finestra (WebView2): app e browser hanno esattamente le stesse
  impostazioni; un'icona vicino all'orologio tiene acceso TaDaMoo Win+GUI quando la finestra è chiusa.
- **Interfaccia web** (`/`, in inglese o in italiano, vedi [Lingua](#lingua)): Chat, Monitor, Hardware, **Carica
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

## L'harness per agenti

L'**harness** (`harness/`) è un agente che lavora sui tuoi file con il modello caricato in TaDaMoo. Legge e modifica un progetto,
esegue comandi, cerca sul web, compila moduli, tiene appunti su ciò che impara e ti chiede prima di tutto ciò che non si può
disfare. È un processo Python separato: parla col server in esecuzione tramite l'API pubblica compatibile OpenAI e non importa nulla
dal motore, quindi non ha bisogno di una GPU sua. Ciò che conserva (conversazioni, memoria, registro di audit, checkpoint,
approvazioni) sta nella cartella dati dell'utente, mai nel tuo repository; l'unica cosa che scrive nel progetto è
`.tadamoo/artifacts`, il testo completo degli output che ha dovuto accorciare.

Il riferimento è [docs/harness.md](docs/harness.md) (in inglese); una guida passo passo è
[docs/harness-manual.it.md](docs/harness-manual.it.md).

### Quattro modi di usarlo

Tutti e quattro costruiscono l'agente nello stesso punto (`tadamoo/stack.py`): ciò che uno raggiunge, lo raggiungono anche gli altri.

| Modo | Cos'è |
|---|---|
| Scheda **Chat** | Conversazioni con l'agente. Ognuna è un processo che resta vivo, con la sua cronologia salvata, quindi un messaggio non paga l'avvio. È il modo principale. |
| Scheda **Agente** | La pagina delle conversazioni, che lavora nelle cartelle che aggiungi: ogni cartella ha le sue conversazioni e un registro `.basalt`, e le modifiche dell'agente si tengono o si annullano blocco per blocco. |
| `python -m tadamoo -p "compito"` o `tadamoo-agent` | La riga di comando, per gli script: codici di uscita, `--json`, limiti. `tadamoo-agent.cmd` funziona da qualunque cartella. |
| `python -m tadamoo acp` | L'agente di un editor che parla l'Agent Client Protocol, su stdio. |

### Cosa sa fare

- **Lavorare su un progetto**: leggere, elencare, cercare, modificare e scrivere file; eseguire comandi di shell; `repo_map` (le
  definizioni di ogni file sorgente, i più rilevanti per primi) e `repo_diagram` (diagrammi degli import e delle cartelle calcolati dai file, senza modello); diagnostica e «vai alla definizione» dei language server quando ce n'è
  uno installato; eseguire frammenti di Python, JavaScript o TypeScript; affidare un compito a un sotto-agente. I file che alleghi
  (testo, codice, CSV, JSON, Word, Excel, PowerPoint, PDF, immagini; audio e video tramite il servizio callAPIcall) sono letti a
  pezzi, e i file che l'agente produce sono controllati prima di esserti offerti.
- **Usare il web**: `search_web` (DuckDuckGo, salvo un altro motore che imposti; senza chiave, e le parole di ogni ricerca vanno a
  DuckDuckGo) e `browse_url` (una pagina come righe di testo, indirizzi privati rifiutati). Con gli strumenti del browser guida Edge o
  Chrome per moduli, prenotazioni e acquisti.
- **Tenere ciò che impara**: una memoria di appunti per progetto (sotto), le regole di `AGENTS.md` lette dalla cartella verso l'alto, le
  skill e i documenti di riferimento offerti come blocco fisso o cercati.
- **Raggiungere altri strumenti**: skill, plugin e server MCP trovati su questo PC (ognuno *da approvare* finché non lo leggi e lo
  approvi, legato a un hash di ciò che hai letto); la ricerca online di quello che manca, con i candidati migliori proposti a te e
  un'installazione che non esegue mai gli script di un pacchetto; server MCP remoti con accesso OAuth; altri agenti tramite un client
  ACP e un client A2A; un consiglio di consiglieri con ruoli modificabili che discutono una domanda difficile per un numero di giri scelto, con il costo mostrato prima; un modello diverso per ogni funzione (quello caricato,
  un altro su questo PC, un servizio cloud).
- **Gestire denaro e identità**: il vault del **Portafoglio** tiene cifrati carte, dati personali e accessi; il modello scrive
  segnaposto e il valore viene inserito all'ultimo passo, solo verso i siti che la voce indica. I pagamenti seguono un limite che
  imposti tu (nessuno finché non lo imposti), una rinuncia facoltativa con massimali e una domanda oltre questi. Il secondo fattore di
  ogni accesso lo scegli tu. Le telefonate vere chiedono ogni volta e sono spente di default.
- **Pianificare**: piani come grafi (azioni, sensori, decisioni, riunioni, compiti passati a te) mostrati nella vista **Piani**; la
  Chat può leggerli e proporre modifiche, leggere cosa farebbero le proposte del planner e approvarle o rifiutarle, e ogni modifica ha il
  tuo sì. Ogni piano ha una scheda rispetto a cui si giudicano gli eventi in arrivo; la Chat ti chiede solo ciò che alla scheda manca. Una
  decisione senza uscita valida passa a te invece di saltare in silenzio. Il filtro degli eventi (locale; quello nel cloud solo con il tuo
  consenso) parte con il server, e la ripianificazione col modello locale è facoltativa.
- **Lavorare con orari propri**: routine (un prompt, una cartella, un orario; sempre in Automatica), valutazione (`python -m tadamoo.eval`,
  pass^k, costo e uso della cache), riesecuzione di una sessione registrata e l'energia usata da un compito.

### Le conversazioni della Chat

- **Una colonna di conversazioni** a sinistra della Chat: apri una precedente, eliminala (la conferma è una seconda pressione) oppure
  mettile la stella, così è elencata a parte, sopra le altre; la freccia in cima la chiude in una striscia stretta. La riga in alto ha
  a sinistra *Nuova conversazione*, *Rami* e l'etichetta della modalità, a destra il tipo di conversazione, *Ragionamento* e *Impostazioni*.
- **Modalità, con un'etichetta**: *Solo un piano*, *Sola lettura*, *Chiedi prima* o *Automatico*. Un'etichetta su due righe nella riga in
  alto dice quale è attiva (*Maiusc + TAB per cambiare modalità:* in grigio, sotto il modo in grassetto e del suo colore, come fa
  Claude Code); **Maiusc+Tab** nel campo del messaggio passa alla successiva, con una conversazione aperta cambia solo quella, e
  *Automatico* chiede le due conferme della finestra di rischio solo la prima volta, finché non chiudi l'app.
- **Livello di ragionamento** per conversazione (l'elenco *Ragionamento*, oppure `/effort`).
- **Comandi con la barra**: scrivendo `/` si apre un menu di 31: sulla pagina (`/clear`, `/mode`, `/plan`, `/copy`, `/export`,
  `/branch`, `/rewind`, `/stop`, `/diff`…), risposti dal processo della conversazione (`/status`, `/context`, `/compact`, `/tools`,
  `/hooks`, `/mcp`, `/memory`, `/permissions`, `/audit`), richieste scritte per il modello (`/init`, `/review`, `/security-review`,
  `/research`) e ogni skill trovata sul PC come `/nome testo`.
- **Sa sempre che ora è.** Prima di ogni richiesta l'agente riceve da solo, dall'harness, data, ora e fuso orario locali della
  persona, quindi non deve mai chiamare uno strumento per chiederli, e l'inizio del prompt non viene toccato (il server tiene la sua
  cache). Ogni messaggio della Chat, anche quelli tecnici (una chiamata a uno strumento, una nota), mostra a sinistra il suo orario:
  l'ora in grassetto, la data sotto, in caratteri piccoli.
- **Le fonti si aprono fuori.** Dopo una notizia o un dato preso dal web, un piccolo pulsante *fonte ↗* apre quella pagina nel browser
  predefinito del sistema (anche dopo un indirizzo scritto nel testo, e per i siti usati da una ricerca o dalla lettura di una pagina).
- **Skill personali.** Premi *Trasforma in skill* sotto una richiesta, spunta le richieste che fanno parte del compito (oppure scrivi
  `/start-learn-skill` e `/stop-learn-skill`), e l'harness conserva ciò che hai fatto come uno script Python con le parti che cambiano come
  parametri: segnate da te con `# una frase #`, o trovate dalle tue parole e dalle date. La riesegui dal bottone *Skill* o con `/skilluse`, con un
  modulo che legge le date a parole. Quasi tutto è codice semplice, quindi non costa token; il modello serve una volta per scriverla e poi solo dove
  bisogna capire il linguaggio. Una skill passa a un altro PC come un solo file `.pskill` e arriva *da approvare*. Vedi il
  [manuale](docs/harness-manual.it.md#17-skill-personali).
- **Un messaggio mentre lavora** viene indirizzato nel compito, messo in coda o trattato come urgente; **Ferma** vuol dire ferma.
- **Rami, tornare indietro, confrontare**: un albero di conversazioni, *Torna qui* su ogni richiesta (le azioni che restano avvenute
  sono elencate prima) e due continuazioni della stessa richiesta affiancate. I messaggi sono nodi immutabili condivisi, quindi un ramo non copia nulla; la vista dei nodi mostra, prima di cambiare ramo, quanto costa al modello, e *Unisci e sintetizza* fa scrivere al modello una sintesi di due rami in una nuova conversazione.
- **Una copia tutta sua** (non offerta dalla pagina Chat, che non ha una cartella di progetto da copiare): in una cartella git una conversazione può lavorare in un suo worktree, e le modifiche che scegli le
  applichi come modifiche in staging, mai come commit.
- **I guasti si gestiscono, non si nascondono**: un server occupato o irraggiungibile si riprova; un processo che muore riparte dalla
  sua cronologia; uno strumento irreversibile non viene mai rieseguito da un riavvio; dopo di che la conversazione è *bloccata* e
  aspetta te.

### Cosa controlli tu

- **Cosa può fare senza chiedere**: le quattro modalità; regole per i comandi (`shell_command:pytest*`, un divieto vince sempre, e una
  regola non copre mai un comando che ne concatena altri o reindirizza); il pulsante *Permetti sempre* in una domanda. Una risposta vale per
  quella chiamata (lo stesso strumento con gli stessi argomenti), non per lo strumento intero, e *Permetti sempre* non propone mai una regola
  larga per `python`, `powershell`, `curl` e simili (solo per quel comando preciso) né per `run_code`: una regola per questi la scrivi tu. In
  *Automatico*, un'azione irreversibile chiede comunque quando la conversazione ha letto contenuti esterni e può raggiungere un segreto
  o una voce del vault (la regola dei due). Il web si legge liberamente per le notizie; un indirizzo web, una ricerca o un modulo chiede
  solo quando la conversazione ha letto contenuti esterni, ha toccato dati privati (una credenziale salvata, un file con chiavi, un
  allegato) e l'azione arriva all'esterno. Impostazioni > Portafoglio regola quanto è rigida. Un segreto salvato non entra
  mai in un indirizzo o in una ricerca, con nessuna impostazione.
- **Hook scritti da te** (`hooks.json`): comandi o blocchi fissi su chiamate di strumenti, turni e sessione; un hook che non riesce a
  decidere blocca.
- **Checkpoint e ritorno**: l'area di lavoro viene salvata prima della prima modifica di un turno, fuori dal progetto e con limiti di
  dimensione; `python -m tadamoo.rewind`, `/rewind` e *Torna qui* su un messaggio la ripristinano. Tornare indietro non disfa un comando che ha
  eseguito, e lo dice.
- **Un registro di audit**: una riga per ogni chiamata di strumento (argomenti con hash e mascherati, la decisione, l'esito), in una
  catena di hash verificabile.
- **Limiti di tempo e Ferma**: ogni strumento ha un limite di tempo (15 minuti salvo diversa impostazione); Ferma termina un comando in
  corso e tutto ciò che ha avviato.
- **Cosa non è**: una sandbox del sistema operativo. Un comando gira con i tuoi diritti, con un ambiente ripulito, senza stdin, con un
  limite di tempo, output limitato e, su Windows, un job object. Usa *Automatico* solo in una cartella che puoi perdere.

### La memoria: cosa non andava, cosa fa adesso

La memoria è un quaderno che l'agente tiene per ogni progetto e rilegge nelle conversazioni successive. La prima versione, in pratica,
non funzionava.

**Cosa non andava.** L'harness cercava nel quaderno a ogni messaggio, ma nulla ci scriveva mai, quindi cercava sempre in un database
vuoto. Gli appunti che avrebbe trovato finivano nel messaggio di sistema, che cambia l'inizio del prompt e costringe il server a
rileggere la conversazione. E la ricerca spezzava gli identificatori come `sm_120` e voleva che ogni parola del messaggio fosse
nell'appunto.

**Cosa fa adesso.**
- Il modello ha tre strumenti. `remember` conserva un appunto breve (al massimo 2.000 caratteri, con una categoria; lo stesso testo si
  tiene una volta sola; al massimo 5.000 appunti per progetto), `recall` cerca negli appunti e `forget` ne cancella uno con il suo id.
  Le descrizioni degli strumenti dicono a cosa servono; il prompt di sistema non li nomina.
- A ogni messaggio che scrivi, l'harness cerca negli appunti con le tue parole e, se qualcuno corrisponde, ne aggiunge fino a quattro
  **dopo il tuo messaggio** come dati, segnati come non fidati. Il blocco di sistema non si tocca mai, quindi il server conserva
  l'inizio della conversazione nella cache. È l'impostazione della Chat *Iniezione → Recupero in coda* (quella predefinita). Con
  *Prefisso fisso* o *Su richiesta* non si aggiunge nulla da solo e il modello deve chiamare `recall`.
- La ricerca è a testo intero (SQLite FTS5): gli identificatori restano interi, una domanda le cui parole non stanno tutte in un
  appunto lo trova lo stesso (prima ogni parola, poi una qualunque), il migliore per primo.
- Un file per ogni cartella di progetto, nella cartella dati dell'utente: `memory\<16 cifre esadecimali>.db`. `/memory` elenca gli
  ultimi dieci appunti (o cerca, `/memory parole`) e `/status` li conta. `--no-memory` sulla riga di comando toglie i tre strumenti e il
  recupero.

**Effetti.**
- Una conversazione successiva nella stessa cartella conosce le decisioni, le convenzioni e i fatti che l'agente ha scelto di
  conservare, senza rileggere tutto il progetto, e l'inizio in cache della conversazione non si disturba quando si aggiunge un appunto.
- Costa token in fondo al contesto: fino a quattro appunti, ciascuno fino a 2.000 caratteri, a ogni messaggio che corrisponde.
- *Chiedi prima* e *Automatico* lasciano che l'agente scriva e cancelli appunti senza chiedere. *Sola lettura* chiede prima di
  scriverne uno. *Solo un piano* rifiuta `remember` e `forget` e permette `recall`.
- Ciò che restituisce `recall` è contenuto esterno: conta per la regola dei due e nessuna citazione presa da lì può confermare una
  modifica a un piano. Gli appunti aggiunti in coda dal recupero sono recintati allo stesso modo e impediscono anch'essi che una
  citazione confermi una modifica a un piano.

**Limiti.**
- **Un appunto resta solo se il modello decide di tenerlo.** Non c'è estrazione automatica, e un modello piccolo può chiamare
  `remember` di rado. Chiediglielo tu («ricorda che …»).
- **La memoria appartiene al percorso di una cartella** (il percorso completo, maiuscole ignorate). Un'altra cartella, una cartella
  rinominata o spostata, o una conversazione che lavora nella sua copia di una cartella git hanno una memoria diversa: gli appunti
  scritti in una copia restano nel file della copia, e il file di una copia scartata resta lì. Dipende da come si chiama il file; l'ho
  letto nel codice, non l'ho misurato. La scheda **Chat** non manda nessuna cartella, quindi tutte le sue conversazioni lavorano in
  `chat-workspace` dentro la cartella dati dell'utente e condividono una sola memoria; la scheda Agente, la riga di comando e l'editor
  usano la cartella che indichi, ciascuna con la sua.
- **Solo ricerca per parole.** Non c'è radice delle parole né embedding: «costruito» non trova un appunto che dice «costruire». Poiché
  la ricerca ripiega su una parola qualunque, un appunto che condivide una sola parola con il tuo messaggio può tornare anche se non
  c'entra.
- **Gli appunti sono conservati come scritti, in chiaro, non cifrati e non mascherati.** Non chiedere all'agente di ricordare una
  password o una chiave.
- **Un appunto sbagliato o ostile resta.** L'agente può essere indotto a tenere un appunto da una pagina che ha letto, e `remember` è
  un'azione reversibile che non chiede a nessuno in *Chiedi prima* o *Automatico*. Torna poi come dato che il modello ha l'ordine di
  non eseguire: il rischio si abbassa, non scompare. Gli appunti aggiunti in coda dal recupero alzano il segnale della regola dei
  due come fa `recall`.
- **Nessuna pagina per sfogliare o modificare gli appunti.** `/memory` li legge (ciascuno tagliato a 300 caratteri); per toglierne uno
  chiedi all'agente di dimenticarlo, oppure cancella il file. Nulla è condiviso tra cartelle, utenti o PC, e non c'è esportazione.

### Motori, fornitori, orchestrazione, piani, cron, ricerca web e velocità

- **Motori.** In cima al Loader c'è una scheda per motore: **Basalt-Plus** (Blackwell), **Strata** (altre GPU, eseguito come
  processo figlio gestito), **llama.cpp** (un `llama-server` gestito, con un aggiornamento che scarica solo dopo che hai
  confermato il piano), **LM Studio** e **vLLM** (server già in esecuzione su questo PC). Un solo motore è attivo alla volta,
  e le schede degli altri mostrano solo ciò che l'hardware può eseguire.
- **Un fornitore per ogni funzione.** Modelli > Fornitori (Providers) permette a ogni funzione interna di usare il proprio
  modello: Chat, l'esecutore dell'agente, il pianificatore, la compattazione del contesto, la sessione tecnica, la
  valutazione, i giudici che controllano le risposte e smistano i messaggi, e ogni membro del consiglio. Una funzione può
  usare il modello caricato, LM Studio, vLLM, llama-server, un servizio cloud (OpenAI, Claude, Gemini, Z.ai, DeepSeek, Kimi,
  Qwen, Nebius, NVIDIA, Grok e altri), qualunque server compatibile OpenAI, oppure gli strumenti a riga di comando di Claude e
  Codex. Una chiamata che esce dal PC richiede il tuo consenso e un interruttore generale.
- **Conversazioni che lavorano insieme.** Una conversazione può avere un nome, scrivere a un'altra (`session_send`), leggerne lo
  stato o avviarne una in background (`session_spawn`: regole proprie, un genitore, un limite di profondità, un tetto di figli
  vivi). Il server tiene i messaggi, conserva l'elenco di quelli mai arrivati e tratta come dato ciò che un'altra
  conversazione manda, salvo che il mittente possa dare ordini. Una conversazione può avere un budget giornaliero di token; un
  riassunto di ciò che ha detto e ricevuto permette di leggere un'esecuzione senza supervisione senza tutta la sua storia. Gli
  strumenti di orchestrazione si offrono solo con `--orchestration` o quando si trovano con `find_capability`.
- **Cron job.** Una pianificazione (espressione cron o un momento) può avviare una conversazione o consegnare il suo messaggio a
  una già esistente o a un piano. Ogni scatto ha un id scritto prima di partire, una durata di validità che salta quelli in
  ritardo e una chiave anti-duplicati; uno scatto già preso non riparte dopo un riavvio. La scheda Cron Jobs elenca gli
  ultimi risultati, esegue un job subito e mostra i messaggi mai arrivati, da rimandare o scartare.
- **Piani con più tipi di passo.** Oltre ad azioni, sensori, decisioni e unioni: attese (una durata, un momento, una condizione
  scritta come espressione o a parole), variabili, un ciclo, una domanda a te, un risultato e un log. Un passo dice che cosa
  produce, riceve i valori dei passi sopra di lui e può ripetersi finché la sua condizione di stop non vale. Attese,
  variabili, risultato e log li risolve il piano stesso, non il modello. Un'attesa scritta a parole la legge la sessione
  tecnica solo quando i valori che osserva sono cambiati, con una pausa che cresce dopo ogni «no» e si ferma a un'ora; senza
  la sessione il piano lo dice. L'orologio dei piani fa un confronto per piano e apre solo quelli in scadenza; un piano in
  pausa sta fermo. Un piano finito bene può diventare una skill personale. La pagina Piani ha una tavolozza dei tipi di
  passo (si trascinano con mouse o penna, oppure si tocca), e il log e i risultati di un piano si leggono come una
  conversazione a cui puoi rispondere.
- **Modifica a parole.** Le schede Piani e Cron Jobs accettano un'istruzione (e creano un job nuovo se non ne è scelto
  nessuno); va a una conversazione di modifica dell'harness, e una modifica sotto il livello Automatico richiede il tuo sì. Le
  schede lampeggiano, con un puntino e una nota parlata, quando è uscito qualcosa di nuovo dall'ultima volta che le hai
  aperte. I comandi di ricarica sono `/reload-mcp`, `/reload-skills` e `/reload-plugins`. La cartella che contiene le
  conversazioni si può spostare dalle Impostazioni (una copia verificata che conserva l'originale, rifiutata mentre una
  conversazione è in esecuzione).
- **Ricerca e navigazione web.** Impostazioni > Ricerca web sceglie il motore di ricerca (DuckDuckGo senza chiave per
  impostazione predefinita, SearXNG, Brave, Tavily, Exa, open-websearch) e il lettore di pagine (Firecrawl, Playwright, altri),
  locali o in cloud, con le chiavi nell'archivio delle credenziali. I programmi locali si installano senza Docker né diritti di
  amministratore, partono quando una ricerca ne ha bisogno e si fermano quando sono inattivi. Un motore scelto che non è pronto
  viene segnalato al modello invece di essere sostituito in silenzio. Una pagina arriva al modello come righe di testo senza i
  menu; il browser controlla ogni richiesta di una pagina, non solo il primo indirizzo, e può conservare una schermata come
  file della conversazione.
- **Telegram e memoria.** Un bot Telegram parla con l'agente della Chat con le stesse funzioni (long polling, elenco di utenti
  ammessi, nessun indirizzo pubblico; [docs/telegram.md](docs/telegram.md)). Impostazioni > Memorie ha un interruttore per
  imparare e uno per usare per ogni memoria, un peso per ogni sezione e le note da leggere, correggere o confermare; una
  conversazione può essere privata e dimenticata insieme alle note che ha insegnato, e la memoria personale si esporta in
  Markdown.
- **Velocità ed economia.** Nel motore: la testa di bozza della decodifica speculativa si sovrappone al lavoro intorno, una
  testa Q8_0 può essere vagliata in due fasi che mantengono i candidati migliori esatti, il commit che Windows addebita per la
  memoria della GPU viene restituito per impostazione predefinita, e due interruttori facoltativi (`--route-prior`,
  `--route-tail-skip`) scambiano un po' di esattezza con la velocità e sono nominati nell'avviso all'avvio. Nell'harness: una
  breve sessione tecnica accanto alle conversazioni si occupa dei lavori rapidi, la cache delle conversazioni tiene solo quelle
  che valgono la pena (`--conversation-cache-min-tokens`), un riassunto interrotto viene ritentato e poi sostituito da note
  fatte dal codice, il codice resta intero nelle sessioni agente e il codice vecchio nella Chat arriva al modello come
  descrizione semplice, e una risposta lunga si scrive a pezzi verificati. I numeri di velocità sono le misure del
  maintainer, con il limite di potenza della GPU dichiarato; qui non se ne cita nessuno.

### Cosa non è fatto

Isolamento di file e rete per i comandi (il job object limita solo le risorse); una prova dell'agente ACP in un editor vero (Zed, JetBrains)
e `terminal/*`; TaDaMoo come server A2A (è solo un client); i conti bancari tramite un aggregatore; una prova di Google Pay o di un modulo Stripe incorporato su un sito vero;
e una misura di quale strategia di contesto funzioni meglio (serve il modello vero). Nessuna telefonata vera è stata fatta in un test,
e Stripe e Google Pay non sono stati provati sui servizi reali. L'elenco con i motivi è in fondo a
[docs/harness.md](docs/harness.md).

## Voce e avatar

TaDaMoo sa ascoltare, parlare e mostrare un volto. Il blocco vocale sta in `voice/` (un processo Python a sé, il **servizio voce**, avviato
dall'app e fermo finché non lo chiedi) e nella scheda **Voce** e in Impostazioni > Voce. Niente viene caricato, aperto o scaricato finché non lo
scegli; ogni modello arriva dalla cartella dei modelli vocali e, dove il catalogo ne ha uno, si controlla con un hash fissato. Il riferimento è in
[docs/voice/](docs/voice/service.md) (in inglese).

- **Tre funzioni vocali**, ognuna con la sua casella: riconoscimento vocale (Whisper), sintesi vocale e avatar. Un solo comando le carica o le
  scarica e un arbitro della VRAM evita che tolgano memoria al motore ([caricamento](docs/voice/loading.md)).
- **Sintesi vocale**: Kokoro, Piper, XTTS v2, Chatterbox, CosyVoice 3, Qwen3-TTS, Orpheus 3B e Dia 1.6B, una voce per personalità, con le voci
  clonate nella cartella dei modelli ([TTS](docs/voice/tts.md), [modelli](docs/voice/models.md)).
- **Lingua parlata che ti segue**: l'avatar passa alla lingua che parli, o tiene quella che chiedi («parla francese»), con un'isteresi che
  evita i cambi per una parola sola. Il motore non cambia mai a comando vocale; se la voce non ha la lingua si ripiega su Piper
  ([lingua](docs/voice/language.md)).
- **L'avatar**: disegnato sul telefono con Wav2Lip o con le forme della bocca (senza GPU), su questo PC con MuseTalk, Wav2Lip v2, SadTalker,
  ER-NeRF e i motori personalizzati Ultralight-Digital-Human e FeatherTalk, addestrati una volta da un video della persona
  ([avatar](docs/voice/avatar.md), [motori](docs/voice/engines.md), [personalizzati](docs/voice/engines-personalised.md)).
- **Personalità**: un nome, un carattere, una voce, un avatar, una memoria a breve e a lungo termine (cifrata su disco) e un archivio di
  conoscenza. **Importa da callAPIcall** porta qui persone, video, memoria e avatar già preparato ([importazione](docs/voice/import.md)).
- **Telefono, ESP32 e desktop**: la scheda Avatar dell'app Android e un terminale ESP32 parlano con un gateway WebSocket sul PC, a pressione o
  continuo, con interruzione della voce ([gateway](docs/voice/gateway.md), [ESP32](docs/voice/esp32.md)); dettatura con scorciatoia, cronologia
  degli appunti e push-to-talk girano sul desktop ([desktop](docs/voice/desktop.md)).
- **Riunioni e telefonate**: l'assistente può entrare in Meet, Teams o Zoom come ospite dichiarato e suggerire risposte, e fare o ricevere
  chiamate con un modem GSM o col telefono, sotto un'unica regola (approvazione, dichiarazione che è un'IA, numeri a tariffa speciale
  rifiutati, registro senza trascrizioni) ([riunioni](docs/voice/meetings.md), [telefonia](docs/voice/telephony.md)).
- **Sicuro di default**: una guardia controlla host e origine di ogni richiesta (`server.allowed-hosts`, `server.allowed-origins`), la chiave
  dell'app Android sta nel Keystore e [la revisione di sicurezza](docs/voice/security-review.md) elenca ogni problema e il suo stato.

Fatto e provato con finti (nessun modello, GPU, telefono, modem o riunione vera): microfono, scorciatoie e finestrina veri, i motori sul
telefono, il firmware ESP32 (mai compilato), la camera virtuale e i motori su GPU **non sono provati** su hardware reale; ogni pagina lo dice.

## Requisiti

- **Una GPU che uno dei due motori sa eseguire.** Il motore **Basalt-Plus** richiede una **GPU NVIDIA Blackwell**
  (compute capability 10.x o 12.x: GeForce RTX serie 50, RTX PRO 6000, B200): i suoi kernel sono compilati solo per
  Blackwell, e con un'altra GPU il Loader, la configurazione guidata, lo script e l'app lo dicono prima di partire e con
  quel motore non si carica nulla. Altro hardware gira con il motore **Strata**, installato da una cartella locale: NVIDIA
  **RTX 40** (Ada), **RTX 30** (Ampere) e **RTX 20** (Turing), **Apple silicon**, **AMD**, **Intel** e macchine a **memoria
  unificata** (un solo blocco condiviso da CPU e GPU). Il Loader mostra una scheda Modelli per ogni motore che il PC può
  eseguire.
- **Windows 10 o 11 x64** (driver WDDM) o **Linux x86-64** per motore, server e interfaccia web. Script e app sono solo
  per Windows.
- RAM sufficiente per gli esperti che non entrano in VRAM (circa 50 GiB per i file a 3 bit, con gli esperti residenti).
- Python 3.10 o più recente con i pacchetti di `requirements.txt`, in un ambiente virtuale `.venv` nella cartella del
  repository (`python -m venv .venv` poi `.venv\Scripts\pip install -r requirements.txt`).
- Per l'app: .NET 8 Desktop Runtime e Microsoft Edge WebView2 Runtime (senza WebView2 l'app apre l'interfaccia web nel
  browser predefinito).
- Compilare: Windows in [docs/WINDOWS.md](docs/WINDOWS.md) (strumenti Visual Studio v143, CUDA 13.1, CMake, Ninja),
  Linux nel README originale ([Build](README.md#build)). L'app: `dotnet publish tools\windows\desktop -c Release -o
  tools\windows\desktop\publish`.

## Compatibilità

- **Motore Basalt-Plus** (file `.basalt`, Qwen3.8-Flash-Next e il percorso Qwen3.6): solo NVIDIA Blackwell, Windows x64 e Linux x86-64.
- **Motore Strata** (secondo motore del Loader, installato da una cartella locale; legge anche i modelli Qwen3.6 e DeepSeek
  V4.1): NVIDIA RTX 40 (compute capability 8.9), RTX 30 (8.6) e RTX 20 (7.5), Apple (Metal), AMD (HIP), Intel (SYCL) e
  macchine a memoria unificata. Il Loader sceglie la scheda dall'hardware che rileva: RTX 30/40, Mac, AMD, Intel, Memoria
  unificata. Eseguito e verificato qui solo su una GPU NVIDIA Blackwell con un GGUF Qwen3.6 che ha lo strato MTP; le schede
  per l'altro hardware ci sono e sono provate con un server finto, ma su quelle macchine non è stata fatta nessuna
  esecuzione. Un DGX Spark (compute capability 12.1) è riconosciuto come memoria unificata, ma l'elenco delle compilazioni di
  Strata si ferma a 12.0, quindi il Loader non gli offre ancora Strata. Una versione per Mac non è compilata: chi ha un Mac
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
Linux; ogni opzione è nel README originale, [Settings](README.md#settings---config), e in `python -m serve.server
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

## Usarlo come server (API)

Il server parla le API di OpenAI e di Anthropic a `http://127.0.0.1:<porta>` (porta predefinita **7232**):

| endpoint | API |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions: messaggi, strumenti (function calling), immagini se la visione è accesa, `response_format`, `stream` |
| `POST /v1/responses` | OpenAI Responses, **senza stato**: si manda tutta la conversazione ogni volta (`previous_response_id` e `GET /v1/responses/<id>` non sono conservati) |
| `POST /v1/messages` | Anthropic Messages: strumenti, blocchi di ragionamento (thinking), immagini, `stream` |
| `POST /v1/messages/count_tokens` | conteggio dei token di Anthropic: lo stesso prompt che leggerebbe `/v1/messages`, senza eseguirlo |
| `GET /v1/models` | il modello servito e ogni altro modello delle cartelle dei modelli che potrebbe servire (forma OpenAI o Anthropic) |
| `GET /v1/models/{id}` | uno di questi |
| `GET /health` | `{"service": "tadamoo", "loaded": true, ...}` quando il modello è caricato; `"standby": true` mentre un server avviato dall'app aspetta il Loader |

**Avviarlo per gli agenti**: `.\Avvia-TaDaMoo.ps1 -server` (torna quando il modello è caricato; negli script senza
nessuno davanti aggiungi `-riusa`), fermarlo con `.\Ferma-TaDaMoo.ps1`. `tadamoo.pid` contiene PID, porta e profilo di un
server avviato da uno script o dall'app.

**Nome del modello.** L'id del modello servito è l'`--alias` (o `"alias"` nella sezione `server` del file di
configurazione), altrimenti il nome scritto nel file del modello. Il campo `model` di una richiesta può essere
qualunque cosa (il server esegue un modello alla volta) e la risposta riporta il nome servito, con un'eccezione: un
nome che `GET /v1/models` elenca come un altro modello, non caricato, viene rifiutato (sotto).

**L'elenco dei modelli.** `GET /v1/models` elenca per primo il modello servito (`"status": {"value": "loaded"}`, o
`"unloaded"` mentre si carica alla prima richiesta), poi ogni file `.basalt` delle cartelle dei modelli che il Loader
mostra come **pronto** (un GGUF da convertire o un file che Basalt-Plus non può eseguire non è elencato), ciascuno con
`"status": {"value": "unloaded"}`. L'id di un modello così è il nome del file senza `.basalt` (con un breve hash in
coda quando due cartelle hanno lo stesso nome di file): il nome da scegliere in un client. Ogni voce ha `created` (la
data del file), `owned_by`, `display_name`, la lunghezza del contesto quando il file la dice (`context_length`; per il
modello servito `meta.n_ctx` è il contesto con cui gira, per un altro modello `meta.n_ctx_train` il più lungo con cui
è stato addestrato), `architecture.input_modalities` (`image` quando il file ha un proiettore visivo) e, solo su un
server in ascolto su 127.0.0.1, il `path` del file. Le cartelle si leggono dalle intestazioni dei file, su un thread, al
massimo ogni 30 secondi: né l'elenco né una richiesta di chat aspettano il disco; l'elenco funziona anche in standby
(nessun modello caricato: solo i modelli delle cartelle). Una richiesta che nomina un modello che l'elenco dà come non
caricato (per id, nome del file o percorso) riceve un 404 che dice come caricarlo, invece della risposta del modello
caricato: OpenAI `{"error": {"code": "model_not_found", ...}}`, Anthropic
`{"type": "error", "error": {"type": "not_found_error", ...}}`.

```sh
# forma OpenAI
curl http://127.0.0.1:7232/v1/models
# {"object": "list", "data": [
#   {"id": "Qwen3.8 Flash Next Uncensored", "object": "model", "created": 1791904651, "owned_by": "tadamoo",
#    "display_name": "Qwen3.8 Flash Next Uncensored", "status": {"value": "loaded"}, "meta": {"n_ctx": 131702},
#    "context_length": 131702, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
#    "path": "D:/Modelli/Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt"},
#   {"id": "OrcaRouter-IQ3_XXS", "object": "model", "created": 1791904651, "owned_by": "tadamoo",
#    "display_name": "OrcaRouter", "status": {"value": "unloaded"}, "context_length": 262144,
#    "meta": {"n_ctx_train": 262144}, "quant": "IQ3_XXS", "path": "D:/Modelli/OrcaRouter-IQ3_XXS.basalt", ...}]}

# forma Anthropic: con l'header anthropic-version (o x-api-key senza Authorization)
curl http://127.0.0.1:7232/v1/models -H "anthropic-version: 2023-06-01"
# {"data": [{"type": "model", "id": "Qwen3.8 Flash Next Uncensored", "display_name": "Qwen3.8 Flash Next Uncensored",
#            "created_at": "2026-10-13T15:17:31Z"},
#           {"type": "model", "id": "OrcaRouter-IQ3_XXS", "display_name": "OrcaRouter",
#            "created_at": "2026-10-13T15:17:31Z"}],
#  "has_more": false, "first_id": "Qwen3.8 Flash Next Uncensored", "last_id": "OrcaRouter-IQ3_XXS"}

# un modello, nelle due forme (un id con spazi va codificato nell'URL); altrimenti 404 model_not_found / not_found_error
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS -H "anthropic-version: 2023-06-01"

# una chat che nomina il modello non caricato
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "OrcaRouter-IQ3_XXS", "messages": [{"role": "user", "content": "ciao"}]}'
# 404 {"error": {"message": "The model 'OrcaRouter-IQ3_XXS' is not loaded (this server is serving 'Qwen3.8 Flash
#   Next Uncensored'). Load it in the web page's Loader (choose it in the model list, then Load), or start the server
#   with it: .\Avvia-TaDaMoo.ps1 --model \"D:/Modelli/OrcaRouter-IQ3_XXS.basalt\"", "type": "invalid_request_error",
#   "param": "model", "code": "model_not_found"}}
```

TODO (possibile in futuro: caricare su richiesta): il server non cambia modello da solo quando una richiesta ne nomina
un altro dell'elenco; dice solo come caricarlo. Caricare il modello chiesto alla richiesta resta per dopo.

**Chiave API.** Il server ascolta solo su 127.0.0.1 e non chiede chiavi: ai client che ne vogliono una per forza si
può dare un valore qualunque. `--api-key CHIAVE` (solo da riga di comando, mai nel file di configurazione) la rende
obbligatoria su `/v1/*`, come `Authorization: Bearer CHIAVE` o `x-api-key: CHIAVE`; serve per ascoltare su un altro
indirizzo (`--host 0.0.0.0`) e per lasciar usare il server a pagine web di altre origini. Script e app avviano il
server su 127.0.0.1 senza chiave.

**Streaming.** `"stream": true` manda eventi server-sent nel formato di ciascuna API (OpenAI `chat.completion.chunk`,
eventi Responses, Anthropic `message_start` / `content_block_delta` / ...), ragionamento compreso, come
`reasoning_content` (OpenAI) o blocchi `thinking` (Anthropic).

**Ragionamento.** Il modello ragiona per impostazione predefinita (il livello alto del template). OpenAI:
`"reasoning_effort": "none" | "low" | "medium" | "high"` (Responses: `"reasoning": {"effort": ...}`). Anthropic:
`"thinking": {"type": "disabled"}`, oppure `"enabled"` con `budget_tokens` (sotto 2048: basso, sotto 8192: medio,
altrimenti alto). `reasoning_budget_tokens: N`, su qualunque endpoint, chiude il ragionamento dopo N token e fa
rispondere il modello.

```sh
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "tadamoo", "messages": [{"role": "user", "content": "Saluta in tre lingue."}],
  "max_tokens": 512, "reasoning_effort": "low"}'

curl -N http://127.0.0.1:7232/v1/messages -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -d '{
  "model": "tadamoo", "max_tokens": 1024, "stream": true,
  "messages": [{"role": "user", "content": "Scrivi un haiku sul basalto."}]}'

curl http://127.0.0.1:7232/v1/responses -H "Content-Type: application/json" -d '{
  "model": "tadamoo", "input": "Quanto fa 17 * 23?", "reasoning": {"effort": "none"}}'
```

(In PowerShell usa `curl.exe` e metti il JSON in un file: `-d "@corpo.json"`.)

Python, con gli SDK ufficiali (`pip install openai anthropic`):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:7232/v1", api_key="none")   # chiave qualunque, salvo --api-key
r = client.chat.completions.create(model="basalt", messages=[{"role": "user", "content": "Ciao!"}],
                                   max_tokens=512, reasoning_effort="low")
print(r.choices[0].message.content)

for chunk in client.chat.completions.create(model="basalt", stream=True,
                                            messages=[{"role": "user", "content": "Conta fino a cinque."}]):
    print(chunk.choices[0].delta.content or "", end="", flush=True)

print(client.responses.create(model="basalt", input="Nomina tre rocce vulcaniche.").output_text)
```

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:7232", api_key="none")
msg = client.messages.create(model="basalt", max_tokens=1024,
                             messages=[{"role": "user", "content": "Ciao!"}])
print(msg.content[-1].text)                      # prima può esserci un blocco di ragionamento

with client.messages.stream(model="basalt", max_tokens=1024, thinking={"type": "disabled"},
                            messages=[{"role": "user", "content": "Conta fino a cinque."}]) as s:
    for text in s.text_stream:
        print(text, end="", flush=True)

print(client.messages.count_tokens(model="basalt", messages=[{"role": "user", "content": "Ciao!"}]).input_tokens)
```

**Claude Code** usa l'endpoint di Anthropic:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:7232"
$env:ANTHROPIC_AUTH_TOKEN = "none"                 # valore qualunque; la chiave data con --api-key, se c'è
$env:ANTHROPIC_MODEL = "basalt"                    # nome qualunque: il server ha un solo modello
$env:ANTHROPIC_SMALL_FAST_MODEL = "basalt"
claude
```

(Su Linux/macOS: `export ANTHROPIC_BASE_URL=http://127.0.0.1:7232` e così via.) Gli strumenti per agenti mandano prompt
lunghi: scegli nel profilo di caricamento un contesto di almeno 65.536 token, e tieni `--parallel 1` a meno che più
agenti lavorino insieme (il contesto si divide fra i posti, vedi
[`--parallel`](README.md#concurrent-requests---parallel-n)).

**Output strutturato** (`/v1/chat/completions`): `"response_format": {"type": "json_object"}` oppure `{"type":
"json_schema", "json_schema": {"name": ..., "schema": {...}}}`. La risposta viene controllata; se non è JSON valido, o
non rispetta lo schema, la richiesta si rigenera con l'errore come spiegazione, fino a `structured_retries` volte (da
0 a 5, predefinito 2; 0 lo spegne). Il nuovo tentativo è la stessa conversazione più un messaggio che dice cosa non
andava e dove (l'errore, il percorso JSON in cui il testo si è rotto, una voce scritta più volte, una sola volta, col
suo numero di ripetizioni); il testo rifiutato non viene mai rimandato indietro, così un modello che girava a vuoto non
rivede il suo giro. Solo la risposta validata resta trattenuta fino al controllo. Il ragionamento scorre mentre viene
scritto, anche quello dei tentativi rifiutati; un tentativo rifiutato è annunciato da un chunk con `delta` vuoto e
`"structured_output": {"attempt": 1, "discarded": true, "reason": "...", "retrying": true}`, la sua risposta non viene
mai inviata, e l'ultimo chunk porta solo la risposta validata, con `"structured_output": {"validated": true,
"attempts": 2, "discarded": [{"attempt": 1, "reason": "..."}]}` (una risposta senza streaming restituisce
ragionamento e risposta dell'ultimo tentativo, con lo stesso campo). `GET /v1/status` lo dichiara in
`structured_output`: `content_buffered` e `reasoning_streamed` sono veri, e `stream_buffered` (lasciato per i client
vecchi) significa che la risposta validata è trattenuta fino al controllo. `/v1/responses` con un `text.format` JSON
trasmette anche il ragionamento e controlla la risposta una volta, senza ritentare; `/v1/messages` non ha output
strutturato.

**Occupato ed errori.** Con `--parallel 1` le richieste aspettano il loro turno in ordine d'arrivo. Mentre il motore
parte, o mentre un server avviato dall'app è in attesa (nessun modello caricato), una richiesta riceve un 503 che dice
perché: riprova più tardi, o carica un modello nel Loader. Un prompt più lungo del contesto viene rifiutato con un 400
che dice la lunghezza del contesto.

## Impostazioni di caricamento, una per una

Si impostano nel Loader (ognuna con il suo motivo e la previsione dal vivo), si salvano nei profili di caricamento e
si scrivono nelle sezioni `server` / `engine` di `tadamoo_config.json`. Valgono quando il motore si (ri)carica.

| impostazione | cosa fa | come sceglierla |
|---|---|---|
| **Modello** (`server.model`) | il file `.basalt` da servire | dalla libreria dei modelli; un GGUF prima si converte |
| **Porta** (`server.port`) | la porta HTTP, predefinita 7232 | cambiala solo per due server insieme o se la 7232 è occupata |
| **Richieste in parallelo** (`server.parallel`, 1-8) | richieste servite insieme nei posti (slot) del motore | 1 per un utente o un agente (la più veloce per richiesta); di più solo per più agenti insieme. Il contesto si divide: ogni posto ha `max-context / N` |
| **Contesto intero per posto** (`server.slot-full-context`) | ogni posto ha tutto il contesto invece di una parte | costa N volte la cache KV, presa dalla cache degli esperti: lascialo spento salvo che ogni agente abbia bisogno di tutta la lunghezza |
| **Immagini** (`server.vision`) | `off`, `dynamic` (prende in prestito posti della cache degli esperti solo mentre legge un'immagine: nessuna VRAM fissa), un numero di GPU come `0` (encoder sempre su quella scheda, circa 2,2 GiB della sua VRAM; i vecchi `on` e `peer` si leggono come GPU 0 e GPU 1), `cpu` (niente VRAM, lento) | `dynamic` se il file ha un proiettore, altrimenti `off` |
| **Contesto** (`engine.max-context`) | la conversazione più lunga, in token (fino a 262.144) | il più lungo che ci sta (lo scelgono «Valori sicuri» e la configurazione guidata); uno più corto lascia più VRAM agli esperti (un po' più veloce) e chiede meno commit |
| **Cache KV** (`engine.kv`) | la precisione della memoria dell'attenzione: `fp16`, `int8`, `q4_0`, `k8v4` (chiavi INT8, valori Q4_0) | `q4_0`: un quarto della memoria di fp16, così i contesti lunghi ci stanno; `int8` o `k8v4` per un'attenzione un po' più precisa con più memoria |
| **Blocchi del prompt** (`engine.prefill`) | come si leggono i prompt lunghi: `auto` (il blocco più grande che la cache degli esperti può prestare), `auto:8192/16384/32768` (un tetto), o una misura fissa | `auto` |
| **Finestra di verifica** (`engine.spec`, 0-8) | la finestra più lunga: il drafter MTP propone fino a N-1 token (meno quando è incerto), il modello li controlla in un passaggio; 0 = spento | 7 se il file ha un drafter (la configurazione guidata misura 5, 7 e 8); il testo è lo stesso a ogni valore, cambia solo la velocità |
| **Cache degli esperti** (`engine.expert-cache`) | posti per esperti tenuti in VRAM: `auto` (tutta la VRAM che resta dopo la riserva) o un numero | `auto` |
| **Esperti residenti** (`engine.resident-experts`) | gli esperti che non sono in VRAM si copiano in RAM una volta al caricamento, invece di leggerli dal file | acceso se ci stanno in RAM tenendo libera la quota (circa 50 GiB per il file a 3 bit); spento sui PC con meno RAM, a costo di velocità |
| **VRAM da lasciare libera** (`engine.vram-reserve-mib`) | VRAM che il motore non tocca (desktop, driver, altri programmi) | vedi [VRAM da lasciare libera](#vram-da-lasciare-libera); la regola della configurazione guidata: 0,5 GB + 1,5 GB se la GPU comanda il monitor principale (circa 0,4 GB per uno secondario) |
| **Tabella PLE** (`engine.ple-io`) | la tabella n-gram per strato: `direct` (letta dall'SSD quando serve, mai in RAM), `ram` (bloccata in RAM all'avvio: RAM ma niente commit), `mmap` (una variante di prova) | `ram` se ci sta tenendo libera la quota, altrimenti `direct`; su un disco lento o meccanico meglio `ram` |
| **Thread CPU** (`engine.pool-workers`) | i thread che calcolano gli esperti che non sono in VRAM | i core performance più metà dei core efficienza, meno uno (un core resta per il server e il desktop) |
| **KV tenuta in VRAM** (`engine.kv-resident`, 0 o da 20.480 celle) | in VRAM restano solo queste celle della KV di ogni strato, tutta la K/V è bloccata in RAM e la VRAM liberata va agli esperti; un contesto che sta in queste celle non viene trasferito | contesti molto lunghi su una scheda piccola; più lento quanto più si trasferisce; non con `k8v4`. Il piano sposta il pool KV dalla cifra della VRAM a quella della RAM |
| **Estensione del contesto** (`engine.rope-scaling`: `none`, `linear`, `yarn`) | estende il contesto oltre la lunghezza addestrata | fissa per l'avvio e cambia l'output; vuoto = quella del file del modello |
| **Fattore di estensione** (`engine.rope-scale`, 1 o più) | il fattore per `linear` e `yarn` | vuoto = quello del file del modello, altrimenti 1 |
| **Base di frequenza RoPE** (`engine.rope-freq-base`, 0 o sopra 1) | la base di frequenza grezza, 0 = quella del modello | lascia vuoto se non sai il valore che vuoi; fissa per l'avvio |
| **Quota CPU degli esperti del prompt** (`engine.prefill-cpu-share`, 0-1) | la quota degli esperti del prompt trasferiti che calcola la CPU; 0 fa trasferire tutto alla GPU | 0,6 predefinito; più bassa con una CPU più debole |
| **Scambi adattivi** (`engine.adapt-swaps`) | il massimo di esperti che un giro adattivo scambia nella cache esperti della GPU principale; 0 = spenti | 96 predefinito; con gli scambi accesi l'output non è identico bit per bit da una volta all'altra |
| **Budget di RAM per gli esperti** (`engine.resident-budget-gib`, GiB) | al massimo questi GiB di esperti copiati in RAM, i più usati per primi | vuoto = automatico; la cifra di RAM del piano ne è limitata; il motore rifiuta un budget che lascia libera meno RAM del suo margine |
| **Esperti nella mappatura del file** (`engine.mmap-experts`) | nessuna copia residente in RAM: gli esperti si leggono da una mappatura del file; prevale su esperti residenti e budget di RAM | di solito più lento (dipende dalla cache di pagine del sistema); spento di default |
| **Cache esperti elastica** (`engine.vram-elastic`) | la cache degli esperti a segmenti, così `POST /v1/vram` può prestare VRAM ad altri programmi tra una richiesta e l'altra | spenta salvo che un altro programma abbia bisogno ogni tanto di VRAM |
| **Mantieni il commit della memoria GPU** (`engine.keep-vram-commit`, Windows) | tiene il commit di sistema che WDDM addebita per la memoria della GPU invece di restituirlo | spento (il commit si restituisce); attivalo se un altro programma alloca memoria GPU mentre il motore è fermo, perché il motore può bloccarsi se quel programma ne spinge fuori la VRAM |
| **File del profilo esperti** (`engine.expert-profile-save`, avanzata) | il file in cui il motore salva ciò che la cache adattiva ha imparato, all'uscita e a intervalli | vuoto = non si salva; lo legge `tools/convert/convert.py --profile` |
| **Intervallo di salvataggio del profilo** (`engine.expert-profile-save-every`, minuti, avanzata) | ogni quanto si salva il profilo; 0 = solo all'uscita | 10 predefinito |
| **Checkpoint del prompt** (`engine.prompt-cache`) | checkpoint di conversazione tenuti tra una richiesta e l'altra, circa 118 MB di RAM ciascuno per slot; 0 rilegge ogni prompt dall'inizio | 6 predefinito; il piano li conta |
| **Checkpoint ogni** (`engine.prompt-cache-every`, token) | crea un checkpoint anche ogni N token di prompt appena letti; 0 = spento | 16.384 predefinito |
| **Checkpoint del primo turno** (`engine.prompt-cache-root`, token) | crea un checkpoint alla fine del prompt di sistema quando è lungo almeno così; 0 = mai | 2.048 predefinito |
| **Conversazioni parcheggiate** (`engine.conversation-cache-mib`, MiB) | budget di RAM per le conversazioni parcheggiate, così chat separate tornano senza rileggere; 0 = spenta | 0 predefinito; il piano conta l'intero budget |
| **Conversazioni parcheggiate, al massimo** (`engine.conversation-cache-slots`) | quante conversazioni si parcheggiano per volta | 4 predefinito; 0 spegne la cache |
| **RAM libera al parcheggio** (`engine.conversation-cache-min-free-mib`, MiB) | il margine di RAM fisica che resta libero quando si parcheggia una conversazione | 2.560 predefinito |
| **Grammatica** (`server.grammar`) | tiene ogni chiamata di strumento e ogni formato di risposta JSON al suo schema dentro il motore | acceso di default; vale al prossimo Carica |

Una regola in più vale per il PC, non per il profilo: la **quota di RAM e commit lasciata libera** agli altri
programmi (15% se la configurazione guidata non ha avuto un'altra risposta, dal 5 al 50%, almeno 2 GB). Ogni piano,
«Valori sicuri» e la configurazione guidata la tengono libera, e un caricamento che farebbe crescere il file di paging
viene rifiutato.

## Impostazioni della chat (inferenza), una per una

Nel pannello delle impostazioni della chat; si salvano per client e come **preset di inferenza** con un nome,
condivisi tramite il server (il preset **Machine Default** è quello della configurazione guidata). Una richiesta porta
solo i campi impostati, così tutto quello che resta vuoto usa i **valori del modello** (da `general.sampling.*` del
GGUF d'origine, mostrati in grigio nei campi), poi quelli del motore. I client delle API mandano gli stessi campi nelle
richieste.

| impostazione | campo della richiesta | cosa fa | consiglio |
|---|---|---|---|
| **Thinking** (accanto al messaggio; effort) | `reasoning_effort` | quanto il modello ragiona prima di rispondere: High (predefinito), Medium, Low, Off | High per i problemi difficili, Low/Off per risposte rapide e chiamate di servizio (molto più veloce) |
| **Budget di ragionamento** | `reasoning_budget_tokens` | dopo N token di ragionamento il ragionamento si chiude e il modello risponde; vuoto, 0 o -1 = nessun limite | 2.000-8.000 per limitare le risposte lente; troppo basso taglia il ragionamento |
| **Token massimi in uscita** | `max_tokens` | la risposta più lunga, ragionamento compreso | vuoto per lo spazio del contesto; impostalo per risposte brevi e prevedibili |
| **Prompt di sistema** | il primo messaggio `system` | istruzioni tenute in testa a ogni conversazione | breve e preciso |
| **Stringhe di stop** | `stop` | la risposta finisce prima di una di queste stringhe | per formati che finiscono con un segnale |
| **Temperatura** | `temperature` | casualità: 0 prende sempre il token più probabile | quella del modello (1,0 per Qwen3.8-Flash-Next); 0,6-0,8 per codice più stabile |
| **Top-p** | `top_p` | sceglie solo fra i token più probabili che insieme arrivano a questa probabilità | quella del modello (0,95) |
| **Top-k** | `top_k` | sceglie solo fra i k token più probabili (1-64) | quella del modello (20) |
| **Min-p** | `min_p` | scarta i token meno probabili di questa frazione del primo | 0-0,05 |
| **Penalità di ripetizione** | `repetition_penalty` | rende meno probabili i token recenti (1 = spenta) | 1-1,1; più alta peggiora codice ed elenchi |
| **Penalità di presenza** | `presence_penalty` | una penalità fissa per ogni token già usato (0-2) | 0; fino a 1,5 se le risposte girano in tondo |
| **Penalità di frequenza** | `frequency_penalty` | una penalità che cresce a ogni uso (0-2) | 0 |
| **Finestra delle penalità** | `penalty_last_n` | quanti token recenti guardano le penalità | 64 (quella del motore) |
| **Seme** | `seed` | lo stesso seme con le stesse impostazioni ripete una risposta | vuoto per risposte varie |
| **Politica del contesto** | (pagina della chat) | cosa fa la chat quando la conversazione supera il contesto: **Ferma al limite**, **Finestra scorrevole** (toglie i turni più vecchi), **Compatta** (alla soglia il modello riassume i turni di mezzo; prompt di sistema, primo messaggio e i due turni più recenti restano parola per parola) | Compatta per le sessioni lunghe; Ferma al limite quando ogni parola deve restare |
| **Compatta al (% del contesto)** | (pagina della chat) | la percentuale del contesto che fa partire la compattazione (30-100%, predefinita 80%) | 80% |
| **Output strutturato** | `response_format` | Testo, oggetto JSON, o schema JSON (con un nome e lo schema) | per i programmi che leggono la risposta |
| **Tentativi dopo una risposta rifiutata** | `structured_retries` | quante volte una risposta JSON non valida si rigenera, con l'errore come spiegazione (0-5, predefinito 2) | 2 |

**Con quale preset si apre un client**: quello usato per ultimo, se esiste ancora; altrimenti segue il predefinito di
adesso (Machine Default, altrimenti i valori del modello). Le impostazioni cambiate a mano restano, e il pannello dice
quale preset è in uso. **Valori del modello** rimette i valori del modello; **Ripristina Machine Default** quelli della
configurazione guidata.

## Calibrazione

Il Loader prevede le velocità di risposta e di lettura del prompt di ogni piano. La previsione parte da una misura di
riferimento e la scala secondo la percentuale di esperti trovati in cache (quanti esperti il piano tiene in VRAM), la
finestra di verifica e i blocchi del prompt. **Calibra** nel Loader (che parte anche da solo al primo caricamento di un
modello) misura la configurazione in uso con tre richieste: una risposta senza ragionamento (512 token), una con
ragionamento (1.024 token) e un prompt lungo (circa 8.000 token). Il risultato va in `calibration\<modello>.json` nella
cartella dei dati, legato al modello e alle impostazioni. Da lì in poi le previsioni di quel modello partono dalla
calibrazione delle stesse identiche impostazioni, altrimenti dalla più recente del modello, invece che dal riferimento
incorporato, e il Loader dice quale ha usato. La misura della configurazione guidata è la stessa calibrazione, ripetuta
per le finestre di verifica che prova. Accanto alle velocità, il file conserva RAM e commit
presi davvero dal caricamento (vedi «Memoria misurata»).

## Lingua

Inglese come predefinito; italiano a richiesta. Una sola scelta vale per l'app, la pagina web, gli script e gli
strumenti da riga di comando, in quest'ordine: `--lang it|en` (script e app, per quell'avvio) > la variabile
d'ambiente `TADAMOO_LANG` > la scelta salvata in **Impostazioni → Lingua** nella pagina web (o con `python -m serve.lang
set it|en|auto`, conservata in `prefs.json` nella cartella dei dati) > inglese. La lingua di Windows non si usa. Le
pagine Monitor e Hardware e alcuni messaggi tecnici del server sono solo in inglese.

Per avere tutto in italiano una volta per tutte:

```powershell
.venv\Scripts\python -m serve.lang set it
```

## Misure della comunità

Righe inviate al progetto originale (`docs/community/benchmarks.json` di `jesdga95/basalt`, copiate il 2026-10-26, 25 prove), tutte prese con
`basalt-bench --community`, quindi confrontabili tra loro: velocità di lettura di un prompt da 16k token (token/s) e velocità di scrittura
(token/s), ciascuna con il limite di potenza della GPU con cui è stata fatta. Il merito è di chi le ha inviate. Le due righe in fondo sono
**le nostre misure di sviluppo** su questo fork (Windows, RTX 5090, 600 W, 127,6 GiB di RAM) e **non usano lo stesso metodo**: la scrittura è
una richiesta di chat con temperatura 0,6 e il drafter acceso, la lettura è un testo ripetuto da 13,5k token, e il modello è la versione
Uncensored di OrcaRouter della stessa architettura. Le righe della stessa fonte si confrontano tra loro; queste due con le altre solo come
ordine di grandezza.

| GPU | Potenza | RAM GiB | SO | Quant | Prompt 16k tok/s | Scrittura tok/s | Inviata da |
|---|---|---|---|---|---|---|---|
| 2x RTX 5090 32 GB | 500 W | 123 | Linux | NVFP4 | 7.740 | 461 | neuro |
| RTX 5060 Ti + RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7.741 | 438 | jesdga95 |
| RTX 5090 32 GB | 575 W | 62 | Linux | IQ3_S | 5.677 | 376 | knoopx |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_S | 8.859 | 375 | drakiss79 |
| RTX 5090 32 GB | 500 W | 62 | Linux | IQ3_XXS | 8.784 | 564 | drakiss79 |
| RTX 5090 32 GB | 475 W | 247 | Linux | NVFP4 | 9.556 | 312 | giveen |
| RTX 5090 32 GB | 500 W | 62 | Linux | NVFP4 | 9.289 | 305 | drakiss79 |
| RTX 5090 32 GB | 500 W | 191 | Windows | NVFP4 | 2.431 | 199 | HenryPDT |
| RTX 5090 32 GB | 400 W | 121 | Linux | NVFP4 | 7.732 | 317 | jesdga95 |
| RTX 5090 Laptop GPU 24 GB | 175 W | 47 | WSL2 | IQ3_XXS | 3.892 | 243 | pyj99_fr |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_S | 3.226 | 187 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_S | 3.236 | 158 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | IQ3_XXS | 3.169 | 202 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | IQ3_XXS | 2.894 | 189 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | NVFP4 | 4.072 | 131 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | NVFP4 | 3.316 | 96 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 126 | Linux | Q4_K_XL | 2.983 | 72 | fukc-gihtub |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q4_K_XL | 2.866 | 104 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q5_K_XL | 2.981 | 77 | foolio |
| RTX PRO 4000 Blackwell 24 GB | 145 W | 123 | Linux | UD-Q6_K_XL | 2.674 | 66 | foolio |
| 2x RTX 5060 Ti 16 GB | 180 W | 60 | Linux | IQ3_S | 2.370 | 161 | KimDaeHwan99 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | IQ3_S | 2.427 | 137 | jesdga95 |
| RTX 5060 Ti 16 GB | 180 W | 121 | Linux | NVFP4 | 2.687 | 92 | jesdga95 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | IQ3_S | 3.180 | 138 | lindworm-hermes2026 |
| RTX 5080 16 GB | 360 W | 88 | WSL2 | Q4_K_XL | 2.835 | 78 | lindworm-hermes2026 |
| RTX 5090 32 GB | 600 W | 127,6 | Windows | IQ3_XXS (questo fork, metodo nostro) | 7.319 | 179 (esatta) / 195 (rilassata 0,3) | questo fork |
| RTX 5090 32 GB | 600 W | 127,6 | Windows | IQ4_XS (questo fork, metodo nostro) | 5.206 | 100 (esatta) / 117 (rilassata 0,3) | questo fork |


## Licenza

Il codice nuovo e modificato di questo fork è rilasciato sotto la **TaDaMoo Win+GUI Community and Commercial License** (vedi [LICENSE](LICENSE)): uso gratuito per privati e per aziende con fatturato lordo annuo consolidato inferiore a 10 milioni di dollari USD (licenza commerciale enterprise richiesta a partire da $10M). Il codice preesistente upstream (Strata, Basalt originale, ggml) rimane sotto le rispettive licenze MIT (vedi [THIRD_PARTY_LICENSES](THIRD_PARTY_LICENSES)).
