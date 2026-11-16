> Riferimento: [README](../../../README.it.md).

## L'harness per agenti

L'**harness** (`harness/`) è un agente che lavora sui tuoi file con il modello caricato in TaDaMoo. Legge e modifica un progetto,
esegue comandi, cerca sul web, compila moduli, tiene appunti su ciò che impara e ti chiede prima di tutto ciò che non si può
disfare. È un processo Python separato: parla col server in esecuzione tramite l'API pubblica compatibile OpenAI e non importa nulla
dal motore, quindi non ha bisogno di una GPU sua. Ciò che conserva (conversazioni, memoria, registro di audit, checkpoint,
approvazioni) sta nella cartella dati dell'utente, mai nel tuo repository; l'unica cosa che scrive nel progetto è
`.tadamoo/artifacts`, il testo completo degli output che ha dovuto accorciare.

Il riferimento è [docs/harness.md](../../harness.md) (in inglese); una guida passo passo è
[docs/harness-manual.it.md](../../harness-manual.it.md).

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
  [manuale](../../harness-manual.it.md#17-skill-personali).
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
  `chat-workspace` dentro la cartella dati dell'utente e condividono una sola memoria; la scheda Agentic coding, la riga di comando e l'editor
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
  aperte. I comandi di ricarica sono `/reload-mcp`, `/reload-skills` e `/reload-plugins`. Le skill si installano (dalla lista inclusa nel programma, da una cartella, da un
  indirizzo GitHub o da una pagina web), si attivano o disattivano e si rimuovono in Impostazioni > Skill o con `/install-skill`. Il lavoro aperto dell'agente (lavori lunghi con i loro sotto-compiti, tenuti nella cartella del progetto e cancellati a lavoro finito) sta nella scheda Compiti e in `/tasks`. La cartella che contiene le
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
  ammessi, nessun indirizzo pubblico; [docs/telegram.md](../../telegram.md)). Impostazioni > Memorie ha un interruttore per
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
[docs/harness.md](../../harness.md).

## Voce e avatar

TaDaMoo sa ascoltare, parlare e mostrare un volto. Il blocco vocale sta in `voice/` (un processo Python a sé, il **servizio voce**, avviato
dall'app e fermo finché non lo chiedi) e nella scheda **Voce** e in Impostazioni > Voce. Niente viene caricato, aperto o scaricato finché non lo
scegli; ogni modello arriva dalla cartella dei modelli vocali e, dove il catalogo ne ha uno, si controlla con un hash fissato. Il riferimento è in
[docs/voice/](../../voice/service.md) (in inglese).

- **Tre funzioni vocali**, ognuna con la sua casella: riconoscimento vocale (Whisper), sintesi vocale e avatar. Un solo comando le carica o le
  scarica e un arbitro della VRAM evita che tolgano memoria al motore ([caricamento](../../voice/loading.md)).
- **Sintesi vocale**: Kokoro, Piper, XTTS v2, Chatterbox, CosyVoice 3, Qwen3-TTS, Orpheus 3B e Dia 1.6B, una voce per personalità, con le voci
  clonate nella cartella dei modelli ([TTS](../../voice/tts.md), [modelli](../../voice/models.md)).
- **Lingua parlata che ti segue**: l'avatar passa alla lingua che parli, o tiene quella che chiedi («parla francese»), con un'isteresi che
  evita i cambi per una parola sola. Il motore non cambia mai a comando vocale; se la voce non ha la lingua si ripiega su Piper
  ([lingua](../../voice/language.md)).
- **L'avatar**: disegnato sul telefono con Wav2Lip o con le forme della bocca (senza GPU), su questo PC con MuseTalk, Wav2Lip v2, SadTalker,
  ER-NeRF e i motori personalizzati Ultralight-Digital-Human e FeatherTalk, addestrati una volta da un video della persona
  ([avatar](../../voice/avatar.md), [motori](../../voice/engines.md), [personalizzati](../../voice/engines-personalised.md)).
- **Personalità**: un nome, un carattere, una voce, un avatar, una memoria a breve e a lungo termine (cifrata su disco) e un archivio di
  conoscenza. **Importa da callAPIcall** porta qui persone, video, memoria e avatar già preparato ([importazione](../../voice/import.md)).
- **Telefono, ESP32 e desktop**: la scheda Avatar dell'app Android e un terminale ESP32 parlano con un gateway WebSocket sul PC, a pressione o
  continuo, con interruzione della voce ([gateway](../../voice/gateway.md), [ESP32](../../voice/esp32.md)); dettatura con scorciatoia, cronologia
  degli appunti e push-to-talk girano sul desktop ([desktop](../../voice/desktop.md)).
- **Riunioni e telefonate**: l'assistente può entrare in Meet, Teams o Zoom come ospite dichiarato e suggerire risposte, e fare o ricevere
  chiamate con un modem GSM o col telefono, sotto un'unica regola (approvazione, dichiarazione che è un'IA, numeri a tariffa speciale
  rifiutati, registro senza trascrizioni) ([riunioni](../../voice/meetings.md), [telefonia](../../voice/telephony.md)).
- **Sicuro di default**: una guardia controlla host e origine di ogni richiesta (`server.allowed-hosts`, `server.allowed-origins`), la chiave
  dell'app Android sta nel Keystore e [la revisione di sicurezza](../../voice/security-review.md) elenca ogni problema e il suo stato.

Fatto e provato con finti (nessun modello, GPU, telefono, modem o riunione vera): microfono, scorciatoie e finestrina veri, i motori sul
telefono, il firmware ESP32 (mai compilato), la camera virtuale e i motori su GPU **non sono provati** su hardware reale; ogni pagina lo dice.

## Requisiti

- **Una GPU che uno dei due motori sa eseguire.** Il motore **Basalt-Plus** richiede una **GPU NVIDIA Blackwell** della
  famiglia 12.x (compute capability 12.x: GeForce RTX serie 50, RTX PRO 6000 Blackwell, DGX Spark): i suoi kernel sono
  compilati solo per quella famiglia, e con un'altra GPU il Loader, la configurazione guidata, lo script e l'app lo dicono
  prima di partire e con quel motore non si carica nulla. Le Blackwell da data centre (10.x, B200) sono rifiutate: i loro
  kernel non sono compilati. Altro hardware gira con il motore **Strata**, installato da una cartella locale: NVIDIA
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
- Compilare: Windows in [docs/WINDOWS.md](../../WINDOWS.md) (strumenti Visual Studio v143, CUDA 13.1, CMake, Ninja),
  Linux nel README originale ([Build](../upstream-basalt.md#build)). L'app: `dotnet publish tools\windows\desktop -c Release -o
  tools\windows\desktop\publish`.
