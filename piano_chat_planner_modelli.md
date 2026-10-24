# PIANO — Chat, Planner ed Esecutore, Modelli e Loader, e ciò che manca delle specifiche

**Stato: proposta. Nulla di quanto descritto qui è stato implementato.** Si basa sul ramo `feature/agent-moon` al commit
`8b8f7f76`. Estende `the_way_to_the_moon.md` (§8) e `conquer_of_the_moon.md` senza sostituirli.
Le dimensioni sono taglie relative (S/M/L/XL), senza date: come in §8 delle specifiche, una data senza base è peggio di nessuna.

**Avanzamento (2026-10-24).** Fasi 0 e 1 fatte nel codice (commit `87a14762`, `fd252e1f`, `1193e5c1`), con una riserva:
- **Fase 0.** Il test di contratto della superficie esterna c'è e fallisce se compare una rotta nuova. Nel farlo ho trovato che
  `/v1/load`, `/v1/unload`, `/v1/vram` e le rotte `/api/*` si proteggono solo contro l'`Origin` di un sito estraneo (`_own_page`): un'app
  locale che non manda `Origin` le raggiunge, con o senza chiave. **Deciso dall'utente: load e unload non devono essere possibili da un
  client.** Ora esistono solo come `POST /api/control/load` e `/api/control/unload`, riservate a questo PC e alla pagina propria e che
  chiedono l'intestazione `X-Basalt-Control` con il segreto che il server scrive all'avvio in `control-<porta>.token` nella cartella dei
  dati; `/v1/load`, `/v1/unload`, `/load` e `/unload` rispondono 404 (il test di contratto lo verifica). `Ferma-Basalt.ps1` legge il
  segreto. **Restano aperti** `/v1/vram` (documentato per i programmi che prestano VRAM) e le altre rotte `/api/*` di scrittura: stessa
  decisione da prendere (vedi §7).
  Il test intermittente non si è riprodotto in 12 esecuzioni (3 in parallelo sotto carico comprese) e resta non identificato.
  I 7 fallimenti di `serve/tests` sono differenze di Windows contro Linux (separatori di percorso, un eseguibile non Win32,
  quattro prove del server che si aspettano un modello predefinito) e non dipendono dalle mie modifiche.
- **Fase 1.** Revisione e origine di ogni modifica, `plan:update_meta`, archivio recuperabile, e la pagina Piani con cronologia,
  dettagli del piano, archivia (due pressioni) e ripristina. Provata in Edge a 1280 e 3600 px, in italiano e inglese.
- **Prove col modello vero** (modello libero, limite di potenza 600 W): una corsa dell'agente riuscita in 7 secondi con argomenti validi;
  una modifica di due righe di un file di 45 righe riuscita, ma fatta con `sed` dalla shell e quindi **non registrata** nel Monitor:
  corretto confrontando i file di testo prima e dopo ogni comando (commit `73d8301c`), poi riprovato dal vivo e annullato un solo
  blocco dal Monitor con il file su disco coerente. La valutazione `core` k=5 e le altre voci della coda sono in corso o in attesa.

**Come leggerlo.** §1 le decisioni già prese, §2 l'architettura, §3 lo stato verificato, §4 le fasi di lavoro,
§5 ciò che resta delle specifiche, §6 le misure, §7 le decisioni aperte. Ciò che non ho verificato è marcato *(non verificato)*.

---

**Avanzamento (2026-10-24, seconda parte).** Fatto in questa sessione, oltre alle Fasi 0 e 1:
- **Chat, riquadro del ragionamento** (`f44fb543`): impostazione "Mostra il ragionamento" (per client, fuori dai preset); riquadro alto 20 righe che scorre, aperto mentre il modello ragiona e chiuso a fine ragionamento, salvo che chi legge lo abbia aperto, chiuso o scorso.
- **Chat, risposte tagliate senza ragionamento** (`e829fede`): diagnosi sul log e sulla conversazione vera. Il server si ferma solo quando il modello emette la fine del turno (`<|im_end|>` o `<|endoftext|>`), anche a metà parola (4–49 token). Sulla cronologia pulita 1 caso su 48; una risposta già tagliata nella cronologia fa salire i tagli al 20–40% e, con più tagli, al 100%; un messaggio brevissimo come "ua" ne provoca 8 su 12; col ragionamento acceso 0 su 3. Non è né la cache né il parsing della pagina. **Rimedio nella Chat** (avviso, "Chiedi di nuovo" e risposta tagliata non più rimandata al modello, `2999d428`). **Prova con `spec: 0` fatta**: senza bozze (`draft_n` 0) i tagli restano uguali (9 su 10 sulla cronologia rovinata, 3 su 10 con il primo taglio dentro, 12 su 12 dopo "ua"), quindi **non è la decodifica speculativa**. A temperature 0 la stessa richiesta si ferma identica a metà parola (`(dall'ingle`, 26 token, 4 prove su 5): il token di fine turno (`<|im_end|>` 248046, `<|endoftext|>` 248044) è il più probabile in quel punto. Vietandolo con `logit_bias` le risposte proseguono coerenti (11 su 11). **Causa**: probabilità spuria del token di fine turno a metà parola in questo modello (IQ3_XXS, riaddestrato), non un difetto del parsing né della pagina. Una penalità sul token riduce i tagli senza danneggiare le risposte di una parola (−5: 3 su 8 invece di 8 su 8 sulla cronologia rovinata; risposte esatte 23 su 24), ma non li elimina: **non attivata**, vedi §7.
- **Client di callAPIcall per audio e video** (`78e86d27`): invio a blocchi, attesa, trascrizione, cancellazione; contratto letto dal servizio vero, servizio finto nei test.
- **Decisioni sugli allegati** (N) e disegno della Fase 4b.
- **Fase 2, parte fatta** (`76b0a67e`, `ea6da452`, `aff74406` e il commit dell'annullamento): processo di sessione dell'harness (`python -m harness session`, protocollo a righe JSON, storia salvata a ogni turno e il messaggio su disco prima che il modello lo veda); nuovi tentativi con attese 2, 5, 15, 30, 60 s e blocco che aspetta la persona; azione irreversibile mai rieseguita dopo un arresto (nota nella conversazione); gestore nel server con supervisore (processo morto, muto o senza progressi, ripartenza dalla storia, tetto di 3 ripartenze in 10 minuti, scadenza per inattività, ripresa dopo il riavvio del server) e rotte `/api/agent/sessions`; risposta a pezzi dal client al processo; modalità "solo domanda"; annullamento che interrompe anche la risposta in corso. **Provato col modello vero**: due messaggi di seguito (il secondo usa il primo), testo a pezzi, annullamento in 0,0 s, storia salvata, chiusura pulita. Test: 17 nuovi nella suite del server e 33 in `tests`, con 8 mutazioni uccise.
  **Resta della Fase 2**: la politica di contesto unica (la compattazione dell'agente al posto di quella della Chat), i controlli di salute con domanda a pulsanti nella Chat (la domanda esiste come evento `blocked`; i pulsanti sono della Fase 4), il collegamento ai passi di piano (`plan:human_fallback`, Fase 3), e la prova con il processo ucciso a metà turno sul server reale (coda di giorno).
- **Verifica di allineamento ai cinque livelli di comprensione** (§10), con le fasi nuove **2c** (pipeline per turno) e **3b** (capacità ad albero, ricerca a richiesta, analisi visiva).

## 1. Decisioni dell'utente che il piano rispetta

| # | Decisione | Conseguenza |
|---|---|---|
| A | Il server LLM per le app esterne offre **solo** le interfacce OpenAI (Chat Completions e Responses) e Anthropic, per il **modello caricato da Basalt**, come ora, e nient'altro. | Nessun proxy verso altri provider, nessuna chiave per app, nessun instradamento tramite il campo `model`. Un test di contratto blocca la superficie (Fase 0). |
| B | La **Chat** è l'interfaccia principale con l'harness: conosce gli scopi, crea, modifica e cancella piani in autonomia interfacciandosi al planner. | La Chat diventa una conversazione a più turni sopra il ciclo dell'agente, con gli strumenti del planner (Fasi 2–4). |
| C | La vista **Piani** è solo l'interfaccia specializzata per la gestione manuale (CRUD) dei piani, non l'ingresso principale. | Chat e vista Piani lavorano sullo stesso archivio (Fase 1). |
| D | Il **loader di Qwen3.8-Flash-Next** resta attivo e a sé stante: da lì si caricano e si avviano i pesi; ogni componente sceglie quale modello usa. | Scheda **Modelli** con due sottoschede (Providers, Qwen Loader) e una tabella delle funzioni (Fase 6). |
| E | **llmswitch** entra in Basalt come libreria con queste scelte: dipendenza `requests` aggiunta; registro **dedicato a Basalt** con importazione una tantum da quello condiviso; **abbonamenti nel primo rilascio**; **Jev** (TypeSafe AI) come tipo di provider, accanto a quelli di chat. | Fase 6. Restano da definire i dettagli elencati in §7. |
| F | Regole permanenti: nessuna chiamata reale senza consenso, nessun push o pubblicazione, decisioni di rilascio (licenza, contenuti, nomi, versioni, pacchetti) chieste prima. | I test con modello vero e con telefonate restano nelle code `tests/QUEUE_daytime.md` e `tests/QUEUE_live_calls.md`. |
| G | **Ogni** operazione del planner chiesta dalla Chat (creare, modificare, segnare, archiviare) va confermata dall'utente: con una sua dichiarazione diretta oppure con una domanda dell'harness. | Fase 3. |
| H | **Pulsanti in linea nella Chat** per le domande (conferme del planner e domande a scelta multipla, come in Claude Code), insieme alla risposta libera: l'utente preme o scrive. Il pulsante ha come `value` la risposta e, premuto, la invia subito alla chat. | Fasi 3 e 4. |
| I | La scheda **Agente non si riduce**: è subito dopo la Chat (ordine e scorciatoie invariati) ed è l'interfaccia più completa al planner; la Chat è la via più automatica. La vista **Piani** resta il dettaglio manuale. | Fase 4. |
| J | **Canvas, council e Mermaid si fanno** (ultimi, con la misura A/B del council). | Fase 7e. |
| K | Sottoschede della scheda Modelli: **Providers** e **Qwen Loader**. | Fase 6. |
| M | **Schemi di contesto** nelle impostazioni di inferenza: iniezione, riscrittura, potatura, riassunto, rami e versionamento, ciascuno con casella e selettore della strategia, e descrizioni (cosa fa, vantaggi, svantaggi, quando si usa) per ogni schema e ogni strategia. Dove ci sono più strategie si implementano le più efficaci, scelte con la misura. | Fase 2b. |
| L | **Processo a vita lunga** per ogni conversazione, con gestione degli errori, nuovi tentativi per errori e passi falliti, e, a incapacità verificata di procedere, avviso all'utente con richiesta di intervento. | Fase 2. |
| N | **Allegati**: primo rilascio testi, dati e immagini; audio e video tramite callAPIcall (client esteso, non un trascrittore nuovo); i tetti sono quelli delle impostazioni di inferenza; un file prodotto ha il pulsante in linea per salvarlo; una skill mancante si propone di installarla. | Fase 4b. |
| O | **Capacità cercate a richiesta, non caricate nel prompt**: skill, plugin e MCP installati dall'utente si rilevano da soli, si ordinano ad **albero per capacità** (con etichette e ricerca) e arrivano al modello solo quando servono. **Analisi visiva** proposta nelle impostazioni: soluzione integrata oppure una voce dell'albero. | Fasi 3b e 4b. *Richiesta e disegno: da confermare (§7).* |
| P | **Cinque livelli di comprensione** (letterale, semantico, contestuale, vincoli, operativo) come **lista di controllo** dell'harness, non come standard: comprendere, pianificare e recuperare sono capacità distinte, attivabili in fasi separate e controllabili dall'applicazione. Le **migliori pratiche** per documenti e contesto (fonti in §9) valgono per gli allegati e per gli schemi di contesto (M). | Fasi 2c, 2b, 4b; verifica in §10. |
| Q | **Pipeline per turno in modo completo come predefinito** (scelta dell'utente, 24/10): scheda dei requisiti, verifica deterministica **e** giudizio sui vincoli qualitativi, con un solo nuovo tentativo. Il modo leggero resta selezionabile. | Fase 2c. |
| R | **Il giudice si sceglie per caso d'uso, dalle impostazioni**: Jev si **attiva da Impostazioni** (chiave e consenso) e per **ciascun caso d'uso** la scelta può essere diversa: *spento*, *modello caricato* o *Jev*. Casi d'uso iniziali: triage degli eventi del planner, giudizio sui vincoli qualitativi (2c), pertinenza di estratti per la conoscenza; altri si aggiungono allo stesso modo. | Fasi 2c e 6 (llmswitch, tipo "giudice"). |

**Vocabolario** *(deciso)*: l'**harness** è l'insieme che la **Chat** usa per lavorare; contiene il **Planner** (scopi, piani, triage Jev,
riesame System 2) e l'**Esecutore** (il ciclo con gli strumenti: file, shell, ricerca, wikibridge, MCP). `harness/` è anche il nome del pacchetto Python.

---

## 2. Architettura target

```
 Pagina (WebView2)                    Server di Basalt (127.0.0.1)                 Processi
 ─────────────────                    ────────────────────────────                 ────────
 Chat  ──── /api/agent/sessions ───►  sessioni di conversazione  ──────────────►   harness (Python, separato)
 (ingresso principale)                  │ storia, approvazioni, corse                │  ciclo dell'agente
                                        │                                            │  strumenti del planner
 Piani (CRUD manuale) ─ /api/plans ─►  PlanService / PlanStore  ◄────────────────────┘  strumenti di lavoro
                                        │   (un solo archivio, revisioni)               permessi, audit, checkpoint
                                        ▼
                                      motore: UN modello residente  ◄──── /v1/chat/completions (127.0.0.1)
                                        ▲
 App esterne ──── /v1/chat/completions, /v1/responses, /v1/messages ─────┘   (solo questo, per il modello caricato)
```

Principi:
1. **Una sola porta per le app esterne**, con le sole tre famiglie di API. Le rotte `/api/*` restano della pagina propria e di loopback.
2. **Il planner è un servizio autonomo** (triage, riesame) che può **svegliare la Chat** con una notifica; la Chat è l'interlocutore, non il contrario.
3. **Un archivio dei piani**, con revisione: le modifiche a mano e quelle della Chat non si pestano i piedi.
4. **I permessi stanno nella struttura**, non nel prompt: le azioni del planner hanno classi di effetto come gli altri strumenti.
5. **Una GPU, un modello**: la concorrenza non è gratuita (§4.23 delle specifiche); la Chat ha la precedenza.

---

## 3. Stato verificato (commit `8b8f7f76`)

| Elemento | Stato | Dove |
|---|---|---|
| Superficie esterna | `POST /v1/chat/completions`, `/v1/responses`, `/v1/messages`, `/v1/messages/count_tokens`, `GET /v1/models`; controllo `/v1/status`, `/v1/load`, `/v1/unload`, `/v1/vram` riservato alla pagina propria | `serve/handler.py` |
| Chat | chiama **direttamente** `v1/chat/completions` e `api/chat/context`; non nomina l'harness | `serve/web/chat.js` |
| Politica di contesto della Chat | `stop`, `rolling`, `compact` | `serve/chatctx.py` |
| Ciclo dell'agente | la conversazione **continua tra una `run()` e l'altra** (`self.messages`); `reset()` la azzera. La CLI e `AgentRunner` lanciano però un solo obiettivo per processo | `harness/agent_loop.py`, `harness/headless.py`, `serve/agent_runs.py` |
| Chiamata al modello | `chat_completion` non in streaming; `on_event` dà il testo per turno, non a pezzi | `harness/client.py`, `harness/agent_loop.py` |
| Strumenti | registro con classi di effetto (`PURE`, `REVERSIBLE`, `IRREVERSIBLE`) e marca `untrusted` | `harness/tools/registry.py` |
| Planner | `PlanService`: lista, creazione, lettura, **cancellazione (non esposta da HTTP)**, eventi (`plan:update_node`, `mark`, `rewind`, `propose_diff`, `approve`, `reject`, `human_fallback`), triage, riesame, flusso SSE | `serve/plans.py`, `harness/planner/bridge.py`, `serve/handler.py` |
| Scopi | il piano ha `target_type` (costante / occasionale); non ho trovato un oggetto "scopo" separato *(non verificato a fondo)* | `harness/planner/dag.py` |
| Vista Piani | crea, aggiunge passi, modifica e segna passi, propone/approva/rifiuta, rewind, risponde al fallback umano, "rimuovi" *(non verificato se di passo o di piano)* | `serve/web/plans.js` |
| Scheda Agente | un modulo (cartella, obiettivo, modalità), riga per strumento, approvazioni, riassunto, "Torna indietro" | `serve/web/agent.js`, `serve/agent_runs.py` |
| Loader | `Supervisor.reload`, `--standby`, profili di caricamento, preset di inferenza (`system`, `stop`, `policy`, `threshold`, `effort`, `reasoning_budget_tokens`, `max_tokens`, formato), Machine Default, scarico per inattività | `serve/supervisor.py`, `serve/profiles.py`, `serve/frontend.py` |
| Modello non caricato | una richiesta che nomina un modello elencato ma non caricato riceve 404 con l'invito a caricarlo dal Loader | `serve/model_list.py` |
| llmswitch (`C:\projects\llmswitch`) | registro di provider (locali, a chiave, abbonamenti, CLI), regola della VRAM, livelli di ragionamento con dialetti, consumatori (`chat`, `copilot`, `embed`) senza modello, preset o limiti propri; dipende da `requests`; salva il registro senza scrittura atomica né blocco tra processi | README, `providers.py`, `reasoning.py` |

---

## 4. Le fasi

Ordine: **0 → 1 → 2 → 2b → 3 → 4 → 5**; la **6** parte dopo la decisione su llmswitch; la **7** si intercala secondo le risorse; la **8** precede ogni rilascio.
Ogni fase si chiude con commit piccoli (`area: cosa fa`, mai amend né force-push, mai push), test, e la voce corrispondente nelle code dei test di giorno quando serve il modello vero.

### Fase 0 — Debiti noti e contratto esterno (S)

- Identificare il test intermittente della suite `tests` (fallito una volta, poi tre passaggi puliti): ripetere in ciclo, isolare, correggere o documentare.
- Classificare i 7 fallimenti di `serve/tests` presenti anche su un HEAD pulito: preesistenti, o correggibili.
- **Test di contratto della superficie esterna** (vincolo A): elenca le rotte `/v1` raggiungibili da un'app esterna e fallisce se ne compare una nuova; verifica che `model` diverso da quello caricato dia l'errore attuale e che nessuna rotta `/api/*` di scrittura risponda a un chiamante non loopback.
- *Accettazione*: due esecuzioni consecutive della suite senza fallimenti nuovi; il contratto fallisce in una prova di mutazione (rotta aggiunta di nascosto).

### Fase 1 — Planner: CRUD completo e coerenza (M) · *non dipende da altro*

- **Cancellazione del piano** come archiviazione recuperabile: rotta HTTP, `PlanStore` che sposta in archivio, vista Piani con doppia conferma; `plan:rewind` resta l'annullamento.
- **Revisione del piano** (`rev`): ogni evento che cambia lo stato la incrementa; una proposta o una modifica porta la revisione su cui si basa, e se è vecchia viene rifiutata con un messaggio che dice di rileggere.
- **Origine** di ogni modifica (`actor`: `user`, `chat`, `planner`) registrata nell'evento e mostrata nella vista Piani.
- Modifica di titolo, `target_type` e scheda di un piano esistente *(da verificare cosa manca)*.
- Notifica dei cambiamenti dalla vista a chi guarda (già c'è SSE): la Chat la userà in Fase 3.
- *Test*: modifica concorrente (due revisioni), cancellazione e recupero, origine nell'evento, rotta senza chiave rifiutata, mutazione sul controllo di revisione.
- *Accettazione*: il CRUD manuale è completo dalla pagina (crea, leggi, modifica, archivia, ripristina) a 1024 e 3600 px, con tastiera, contrasto ≥ 4,5:1.

### Fase 2 — Sessioni di conversazione dell'harness (L)

Oggi il ciclo sa continuare una conversazione, ma nessun processo lo fa vivere tra un messaggio e l'altro.

- **Servizio di sessioni** nel server (estende `AgentRunner`, rotte `/api/agent/sessions`, solo pagina propria): crea, invia un messaggio, leggi la storia, ferma, riprendi, chiudi.
- **Scelta di progetto** *(decisa, decisione L)*: **un processo a vita lunga per conversazione**, con protocollo a righe JSON, storia **salvata a ogni turno**, scadenza per inattività e ripartenza dalla storia se il processo muore. Motivi: l'avvio a ogni messaggio (import, server MCP, catalogo skill) si sentirebbe in una Chat che è l'ingresso principale; si conservano connessioni MCP e permessi di sessione; il planner può spingere un avviso. Il sottoprocesso per turno resta il ripiego. La misura 1 (§6), fattibile senza GPU, serve a dimensionare scadenze e ripartenze, non a rimettere in discussione la scelta.
- **Errori, nuovi tentativi e intervento dell'utente** (decisione L). Un **supervisore** nel server sorveglia ogni sessione (battito del processo, tempo massimo per turno) e distingue:
  - **processo morto o bloccato**: riparte dalla storia salvata;
  - **errore del modello o del server** (timeout, 5xx, motore occupato o non caricato): nuovo tentativo con attesa crescente; prima di ritentare controlla lo stato (modello caricato, server raggiungibile);
  - **strumento fallito**: lo vede il modello, che può cambiare approccio; il supervisore non lo ritenta alla cieca;
  - **passo di un piano fallito**: ritento secondo la regola del passo (tentativi massimi nel passo stesso, `plan:mark` al fallimento).
  Regole comuni, tutte configurabili con valori di partenza prudenti:
  - **un tetto ai tentativi e alle ripartenze** in una finestra di tempo, per non girare a vuoto;
  - **mai un nuovo tentativo automatico di un'azione `IRREVERSIBLE`** (una telefonata, un pagamento, una cancellazione): prima si guarda che cosa è successo davvero, come già prescrive la coda delle chiamate reali;
  - ogni tentativo e il suo motivo restano nell'audit e nel pannello.
- **Incapacità verificata di procedere.** Dopo i tentativi, il supervisore **verifica** (controlli di salute: modello, server, spazio, permessi, strumento che fallisce sempre allo stesso modo). Se non si può procedere, **avvisa** nella Chat con una domanda a pulsanti ("Riprovo", "Salto questo passo", "Ferma", "Apri il registro") e con un segno nella scheda Agente, che sopravvive alla chiusura della pagina; per un passo di piano riusa la richiesta di intervento umano già presente (`plan:human_fallback`). Non si decide mai al posto dell'utente: la sessione aspetta la risposta.
- **Storia salvata** nella cartella dei dati utente, con la stessa disciplina di byte-stabilità del prefisso (la ripartenza non deve invalidare la cache del motore).
- **Risposta a pezzi** (streaming): `chat_completion` con flusso e un evento di testo parziale, mostrato dalla pagina.
- **Un'unica politica di contesto**: la compattazione dell'agente (TokenJuice, blocchi fissi, errori mai troncati) sostituisce quella della Chat; `chatctx.py` resta per le app esterne.
- **Modalità "solo domanda"**: la sessione senza strumenti, per le risposte semplici, così un saluto non diventa un ciclo.
- *Test*: sessione a più turni contro `MockEngine` e un sottoprocesso vero; ripresa dopo un arresto; stop a metà turno; nessun segreto nella storia; il prefisso resta identico tra un turno e il successivo. **Guasti iniettati**: processo ucciso a metà turno, motore che risponde 5xx e poi si riprende, motore che non si riprende, strumento che fallisce sempre; azione `IRREVERSIBLE` che non viene mai ripetuta; tetto dei tentativi rispettato; la richiesta all'utente compare con i pulsanti e la sessione aspetta.
- *Accettazione*: due messaggi di seguito, il secondo si riferisce al primo; l'arresto interrompe entro un tempo dichiarato; la storia sopravvive al riavvio del server; un guasto temporaneo si risolve da solo e uno permanente arriva all'utente con una domanda, mai in silenzio e mai con un'azione irreversibile ripetuta.

### Fase 2b — Schemi di contesto (prompt engineering) nelle impostazioni di inferenza (L) · *dopo la Fase 2*

Sei schemi, ciascuno con una casella di attivazione e, dove esistono più strategie, un selettore accanto (decisione M).
Vivono nei **preset di inferenza** (chiave nuova `context_patterns` accanto a `policy` e `threshold`, che restano leggibili e
vengono mappati sugli schemi) e valgono per le funzioni **interne** (Chat, Esecutore, Planner), per preset. Alle **app esterne**
non si applica nulla in più: per loro restano solo i parametri `policy` e `threshold` della richiesta (decisione A).
L'ordine delle trasformazioni è fisso: **iniezione → riscrittura → potatura → riassunto**; il versionamento le avvolge e il
ramo è un'operazione della sessione, non una trasformazione. Tutto spento di partenza, tranne ciò che oggi già avviene.

| Schema | Strategie offerte | Esiste già | Effetto sulla cache del prefisso |
|---|---|---|---|
| **Iniezione** (aggiungere informazioni) | `prefisso fisso` (regole, `AGENTS.md`, catalogo skill, documenti di riferimento); `recupero in coda` (RAG da wikibridge, memoria, `kb_search`, dopo il messaggio dell'utente); `su richiesta` (il modello chiama lo strumento) | sì: blocchi fissi, `retrieved`, `kb_search` | prefisso fisso: costruito una volta; coda: nessuno; su richiesta: nessuno |
| **Riscrittura** (istruzioni più efficaci) | `normalizza con modello fisso` (ruolo, obiettivo, vincoli, formato; senza chiamate al modello); `proposta del modello con revisione` (il modello propone, tu vedi il confronto e approvi) | no | solo la prima volta |
| **Potatura** (togliere rumore) | `maschera le osservazioni vecchie` (segnaposto che dice dove ritrovarle); `togli il rumore` (righe doppie, spazi, HTML in testo, JSON ridotto); `più vecchi per primi` (rolling); `per pertinenza` (BM25 sulle sole parole, senza embedding) | in parte: TokenJuice, `rolling` | rompe il prefisso dal punto potato |
| **Riassunto** (comprimere lo storico) | `strutturato dal modello` (fatti, decisioni, domande aperte, codice); `incrementale` (aggiorna il riassunto a ogni soglia); `estrattivo` (testa, coda e righe chiave, senza modello) | sì: `compact`, TokenJuice | rompe il prefisso una volta |
| **Rami di contesto** (alternative in parallelo) | `copia a un punto` (sequenziale, i rami si confrontano); `candidati in parallelo` (N risposte con prefisso condiviso sugli slot, poi scegli tu o un giudice); `rami del piano` (le alternative del riesame) | no (c'è `harness/swarm`) | prefisso condiviso: nessuno |
| **Versionamento** (tracciare le modifiche) | `istantanee per turno` (contesto con hash a catena, confronto e ritorno); `versioni con nome` (prompt di sistema e preset, con differenze e ripristino) | in parte: audit e punti di ripristino dei file | nessuno |

- **Scelta delle strategie «più efficaci»**: non per sentito dire. Ogni strategia ha un insieme di valutazione in `harness/eval`
  (conversazioni lunghe: pass^k, token, cache, errori di richiamo) e il valore di partenza è quello che misura meglio; le altre
  restano selezionabili. Dove la specifica ha già un ordine (mascheramento prima del riassunto, §4.1) si rispetta.
- **Descrizioni accanto a ogni casella e a ogni strategia**, raggiungibili con tastiera e tocco (pulsante «i» che apre una
  descrizione ancorata, non solo al passaggio del mouse). Ognuna ha **quattro parti**: *che cosa fa*, *vantaggi*, *svantaggi*,
  *quando si usa*; più, per le strategie, l'effetto sulla cache. I testi sono nel catalogo in italiano e inglese, e un test
  controlla che nessuno schema o strategia ne manchi.
- **Catalogo unico** (`GET /api/context/patterns`, schema dei parametri compreso) da cui la pagina costruisce la tabella: aggiungere
  una strategia non richiede toccare la pagina.
- **Una strategia che rompe il prefisso lo dice** nella descrizione e nel Monitor (perdita di cache misurata, non stimata).
- *Test*: per ogni strategia, ingresso e uscita deterministici dove possibile; l'ordine fisso; la cache del prefisso invariata
  quando lo schema è spento; un invito ostile dentro un documento iniettato non cambia le istruzioni; nessuna strategia scrive
  fuori dalla sessione; il confronto di versione mostra esattamente ciò che è cambiato.
- *Accettazione*: ogni casella accende e spegne il suo effetto in una conversazione vera; ogni selettore cambia la strategia;
  tutte le descrizioni sono presenti; i numeri delle valutazioni sono etichettati con il limite di potenza della GPU.

### Fase 2c — Pipeline per turno: scheda dei requisiti, contesto e verifica (M) · *dopo la Fase 2; misurare prima (§6)*

Dalla lista dei cinque livelli (P). Oggi un turno è **una sola inferenza** che il ciclo ripete con gli strumenti; nulla controlla che i vincoli siano rispettati. Non si aggiungono chiamate al modello per ogni turno: si separa ciò che il software sa fare con certezza.

- **Scheda dei requisiti** (livelli 1, 2 e 4): per ogni richiesta l'harness tiene una scheda in `session state`: azione, contenuto, formato, **vincoli misurabili** (lunghezza, lingua, elenco, nome file), pubblico e scopo. I vincoli misurabili si estraggono con regole deterministiche (numeri, unità, formati); scopo e pubblico li propone il modello **nella stessa inferenza** che pianifica (un solo JSON), senza una chiamata in più. La scheda si fissa nel contesto e resta tra un turno e l'altro.
- **Costruzione del contesto** (livello 3), in codice: scopi e piani attivi (Fase 3), memorie e wikibridge (già in `retrieved_message`, ma oggi solo in headless e ACP: portarlo alla Chat), manifesto degli allegati (4b), schemi di contesto (2b). Ordine fisso, prefisso stabile per la cache del server, in coda ciò che cambia.
- **Verifica** (livello 4): prima di consegnare, controlli **deterministici** sulla scheda (parole, formato, lingua, file prodotto che si riapre) e, dove non bastano, un **giudizio** del modello o di Jev (tipo "giudice" in llmswitch). Se un controllo fallisce, **un solo** nuovo tentativo con l'elenco dei vincoli violati; poi si dice all'utente che cosa non è rispettato. Il ciclo non si ferma più soltanto "perché il modello non chiama strumenti".
- **Pianificazione e scelta degli strumenti** (livello 5): resta al modello e al planner; **esecuzione, permessi e verifica restano all'applicazione** (già vero: permessi, sandbox, punti di ripristino, audit).
- **Modi** (decisione Q: **completo è il predefinito**; *leggero* resta nel selettore delle impostazioni di inferenza, con le descrizioni a quattro parti di 2b):
  - *Completo*: 1) scheda dei requisiti; 2) controlli deterministici; 3) **un giudizio** sui vincoli che non si misurano (tono, pubblico, "senza ripetere", coerenza con il materiale precedente), dato da il provider scelto per questo caso d'uso nelle impostazioni (decisione R): Jev, se attivato con chiave e consenso, oppure il modello caricato con un prompt a parte e una griglia di risposte fissa (sì/no per vincolo, con la frase che lo prova); 4) se manca qualcosa, **un solo** nuovo tentativo con l'elenco dei vincoli non rispettati; 5) la risposta finale dice che cosa non è stato rispettato, se resta qualcosa.
  - *Leggero*: scheda e controlli deterministici, nessuna chiamata in più.
  - **Confini del modo completo** (perché il costo resti prevedibile): il giudizio si chiama **solo se la scheda ha vincoli qualitativi**; al massimo **un giudizio e un nuovo tentativo** per risposta; nessun giudizio sui turni di sola conversazione senza vincoli; il giudizio non vede le istruzioni dell'utente come dato da eseguire; il tempo in più e le chiamate in più si mostrano nell'elenco di ciò che è stato fatto (Fase 4). Il giudice è un **tipo di provider** in llmswitch (decisione E).
  - **Rischi dichiarati**: il giudice può sbagliare nei due sensi; un nuovo tentativo può peggiorare la risposta. Per questo si misura sempre: lo stesso gruppo di compiti con completo, leggero e senza verifica (§6); se il completo non porta un miglioramento misurato, si torna al leggero come predefinito.
- *Test*: scheda estratta da frasi con vincoli (parole, formato, lingua); verifica che fallisce e produce un solo secondo tentativo; con `MockEngine`; nessuna chiamata in più in modo leggero (si conta). *Misura*: tasso di vincoli rispettati con completo, leggero e senza verifica sullo stesso gruppo di compiti, più chiamate e secondi in più (§6).

### Fase 3 — Strumenti del planner per la Chat e regole di autonomia (L)

- **Strumenti** registrati nel ciclo, con classe di effetto e marca di provenienza:

  | Strumento | Effetto | Regola (decisione G) |
  |---|---|---|
  | `plan_list`, `plan_get` | `PURE` | libero (sola lettura) |
  | `plan_create`, `plan_propose_change`, `plan_mark`, `plan_archive` | `REVERSIBLE` | **sempre confermati dall'utente**, in qualunque modalità di permesso, compresa `auto` |
  | `ask_user` | `PURE` | pone una domanda con opzioni (vedi sotto) |

- **Come si dà la conferma.** Due vie, entrambe strutturali:
  1. **Dichiarazione diretta:** la chiamata porta la citazione letterale delle parole dell'utente che chiedono l'operazione; il sistema verifica che la citazione sia nell'ultimo messaggio dell'utente e che dopo quel messaggio non sia entrato contenuto non fidato (pagina, file, risposta di strumento). Se i controlli passano, l'operazione parte. È un freno ai casi comuni, non una prova (una citazione banale lo supera), perciò ogni operazione eseguita compare subito nella conversazione con l'esito e un pulsante "Annulla" (l'archiviazione è recuperabile, `plan:rewind` esiste).
  2. **Domanda dell'harness:** se manca la dichiarazione, **è il sistema, non il modello,** a comporre la domanda dai dati reali della chiamata ("Archiviare il piano X?") con i pulsanti Conferma e Annulla; il modello non può descrivere male ciò che sta per accadere.
- **Domande con pulsanti (`ask_user`)**: domanda, opzioni con `label` e `value`, risposta libera sempre disponibile (come in Claude Code). Premuto, il pulsante **invia subito il `value` come messaggio dell'utente**; se l'utente scrive, la domanda si chiude. Solo un clic o la tastiera producono un messaggio dell'utente: il modello non può simulare una conferma. La pagina mostra il testo che verrà inviato e il `value` ha un tetto di lunghezza (un'etichetta "Sì" con un `value` diverso sarebbe un inganno). Si estende l'`ApprovalBoard` e `ServerApprover` già esistenti.

- **Consapevolezza degli scopi**: un riassunto **breve e stabile** dei piani attivi (titolo, tipo, stato, passi aperti), ricalcolato solo quando cambia lo stato del planner; la Chat legge il dettaglio con `plan_get`. Un riassunto che cambia a ogni turno annullerebbe il prefisso in cache.
- **Il planner sveglia la Chat**: un evento che blocca un piano, una proposta di riesame o una richiesta di fallback umano diventano un messaggio della sessione ("il piano X è bloccato perché…") alla prima occasione; nessun messaggio mentre un turno è in corso.
- **Esecutore agganciato ai passi**: un passo di tipo azione si esegue come corsa, con l'identificativo del piano e del passo nel pannello dei compiti; è la Chat a segnare il passo come riuscito o fallito dopo aver letto l'esito (non in automatico nella prima versione).
- **Test avversariali** (estendono la suite di §4.8): un risultato di ricerca, un file o una pagina che chiedono di cancellare o riscrivere un piano **non devono** produrre un'azione senza approvazione; il tentativo si vede nell'audit.
- **Insieme di valutazione `planner_chat`** (`harness/eval/sets/`): crea un piano da una richiesta; modifica un passo; riepiloga lo stato; non crea piani per un saluto; non cancella su istruzione dentro una pagina web.
- *Accettazione*: la suite avversariale passa; nessuna operazione del planner parte senza conferma (dichiarazione verificata o domanda dell'harness); il riassunto dei piani non cambia il prefisso tra due turni senza modifiche (misurato).

### Fase 3b — Capacità ad albero, ricerca a richiesta e analisi visiva (L) · *dopo la Fase 3; richiesta dell'utente (O)*

**Il problema, misurato sul tuo PC.** Nei plugin installati ci sono **282 file `SKILL.md`** (173 nomi distinti, 3,2 MB di testo); elencarli con nome e descrizione nel prompt costerebbe, a stima, circa 17.000 token a ogni chiamata (da misurare), e `SkillCatalog.format_catalog_prompt` fa proprio questo. Con gli MCP si somma lo schema di ogni strumento, che oggi arriva intero a ogni turno (`ToolRegistry.schemas()`).

**Disegno**
1. **Rilevamento automatico, a sola lettura.** Fonti: `~/.claude/skills`, `<area di lavoro>/.claude/skills` e `.agents/skills`, i plugin in `~/.claude/plugins` (le loro `skills/*/SKILL.md` e il loro `.mcp.json`), i server MCP in `~/.claude.json` e nel `.mcp.json` del progetto e in `%APPDATA%\Claude\claude_desktop_config.json`. Si leggono **nomi, descrizioni e comandi, mai variabili d'ambiente né chiavi**. Una cartella che finisce in `.disabled` è spenta. Pulsante "Rileva di nuovo" e rilevamento all'avvio.
2. **Nulla si attiva da solo.** Le voci trovate compaiono come "da approvare": per le skill vale l'`ApprovalStore` esistente (approvazione legata all'impronta del testo letto); un server MCP è codice e parte solo dopo l'approvazione.
3. **Albero delle capacità** (`<dati utente>/capabilities/index.json`, indice generato; `skills.md` ne è la vista leggibile, vedi sotto). Radici di esempio: *visione* (analisi: OCR, grafici, schermate, foto; generazione), *documenti* (lettura, scrittura), *web* (ricerca, navigazione), *codice*, *audio e video* (trascrizione con callAPIcall), *conoscenza* (wikibridge, memoria), *comunicazione* (telefono, posta). Ogni voce: id, nome, tipo (integrata, skill, plugin, MCP), origine, riga di descrizione, **etichette**, nodo dell'albero, stato (da approvare, approvata, spenta).
4. **Etichettatura.** Si usano le etichette dichiarate (`metadata.tags` o `category` nel front matter, se ci sono); in loro assenza, regole a parole chiave sulla descrizione (per la visione: immagine, screenshot, OCR, grafico, diagramma, foto, visual) e punteggio BM25; la correzione a mano dell'utente (sposta una voce) ha la precedenza e si conserva in un file a parte.
5. **Nel prompt solo l'albero in cima**: i nomi delle radici con il numero di voci (poche righe, a stima circa 150 token) e lo strumento `find_capability(query, categoria?, tipo?)`, che restituisce al più 5 voci con id, riga e nodo. La ricerca è locale: **FTS5/BM25** (già usato dalla memoria) con etichette e nodo come campi; un secondo passo con incorporamenti è opzionale e non nel primo rilascio. Lettura del corpo con `load_skill`, come oggi.
6. **Cache del server.** Lo strumento scoperto **non entra nel blocco degli strumenti** (cambierebbe il prefisso e farebbe rileggere il prompt): la sua descrizione arriva in coda alla conversazione e si usa con un solo strumento generico `call_capability(id, argomenti)` che passa dal gestore dei permessi.
7. **Analisi visiva nelle impostazioni** (scheda Impostazioni, sezione "Analisi visiva"): un elenco a discesa alimentato dal nodo *visione/analisi* dell'albero. Prima voce **"Integrata: il proiettore del modello"** con il suo stato (oggi **spenta** sul tuo PC: `vision: off` in `basalt_config.json`; accenderla chiede il riavvio), poi le voci rilevate (skill, plugin, MCP) con origine e stato di approvazione, infine "Nessuna". La scelta alimenta lo strumento `analyze_image(allegato|percorso, domanda)`, che instrada all'integrata, alla skill (ne carica il corpo) o allo strumento MCP. Con l'integrata si aggiunge un **ritaglio e ingrandimento** dell'immagine (le prove di Anthropic mostrano un miglioramento costante quando il modello può ingrandire una zona).
8. **`skills.md`**: **non esiste** né nel repository né sul tuo PC. Si genera da `index.json` in `<dati utente>/capabilities/skills.md`, con una sezione per radice (tra cui "Visual analysis"), per poterlo leggere e riordinare; se intendevi un altro file, indicami dove sta.

*Test*: rilevamento su una cartella finta con skill, plugin e `.mcp.json` (nessuna chiave letta: prova con una chiave finta che non compare mai nell'indice); `.disabled` ignorato; voce "da approvare" mai eseguita; `find_capability` trova la skill giusta tra 300 finte; il prompt iniziale resta sotto un tetto di token (si conta); `call_capability` rifiutato per voce non approvata. *Misura*: token del prompt con elenco piatto e con albero sui 282 file reali; precisione dei primi 5 risultati su 20 richieste scritte a mano (§6).

### Fase 4 — La Chat come ingresso principale (L)

- **Pagina Chat** su sessioni: scelta "con strumenti" / "solo domanda", risposta a pezzi, **elenco di ciò che è stato fatto** (strumenti usati, esito, piani toccati con collegamento alla scheda Agente e alla vista Piani), **domande e conferme con pulsanti in linea** insieme alla risposta libera (decisioni G e H), pulsante di arresto.
- **Scheda Agente (decisione I): non si riduce.** Resta subito dopo la Chat, con ordine e scorciatoie invariati. Diventa il **cruscotto più completo del planner**: piani attivi con passi e stato, corse, approvazioni e domande in sospeso, punti di ripristino ("Torna indietro"), avvisi del planner, e la possibilità di avviare e correggere. La Chat è la via più automatica allo stesso insieme di dati.
- **Vista Piani**: resta la gestione manuale di dettaglio (grafo, modifica dei passi), sullo stesso archivio (Fase 1).
- Tavolozza Ctrl+K con i nuovi comandi; stringhe in italiano e inglese.
- **La Chat senza strumenti** (oggi) resta raggiungibile come modalità, con le app esterne che continuano a usare `chatctx.py`.
- *Test*: audit con Edge headless (`tools/web_audit.mjs` aggiornato), controlli statici (`serve/tests/test_web_static.py`), nessun `innerHTML`, tutte le stringhe tradotte, nessuno scroll orizzontale da 1024 px in su.
- *Accettazione*: dalla sola Chat si crea un piano, lo si modifica, se ne avvia un passo, si approva un'azione e si legge il resoconto, senza aprire altre schede; **da verificare dal vivo con Qwen** (coda di giorno).

### Fase 4b — Allegati nella Chat (XL) · *decisioni dell'utente prese; dopo la Fase 4 per gli strumenti*

Oggi la Chat accetta solo immagini (`chat.js`, `attachments`).

**Decisioni dell'utente**
- **Primo rilascio: testi, dati e immagini.** Audio e video passano da **callAPIcall**, che li gestisce già: Basalt non porta dentro trascrizione né elaborazione video; si **estende il client esistente** (`harness/integrations/call_api_client.py`).
- **Tetti**: nessun tetto proprio degli allegati. Valgono quelli delle **impostazioni di inferenza**: finestra di contesto, politica di superamento (ferma, finestra scorrevole, compatta) e token massimi in uscita. Ciò che non entra nel contesto si legge a pezzi con gli strumenti.
- Un risultato che è un file ha un **pulsante in linea per scaricarlo e salvarlo in locale** (finestra di salvataggio di sistema come predefinito). Un formato che serve una skill non installata: la Chat **propone di installarla**.
- *Supposizione da confermare*: per testi, dati e immagini l'estrazione è **locale**; nulla dell'utente va a servizi esterni.

**Come funziona (disegno)**
1. **Deposito**: `POST /api/attachments` (solo da questo PC e dalla pagina propria) salva in `<dati utente>/attachments/<conversazione>/<id>/` l'originale (mai modificato) e una scheda (nome, famiglia riconosciuta dal contenuto e non dal tipo dichiarato, dimensione, impronta). Niente nel `localStorage`: la conversazione conserva solo il riferimento.
2. **Testi e dati letti direttamente** (txt, md, csv, tsv, json, codice, log): entrano nel contesto così come sono, con nome e dimensione. Se superano ciò che il contesto permette (conteggio del server, `/api/chat/context`), entra l'inizio e il resto si legge con lo strumento `read_attachment` (intervalli di righe o di caratteri).
3. **Formati che chiedono estrazione**: docx e xlsx sono archivi OOXML, leggibili in locale con la sola libreria standard (nessuna dipendenza); pdf richiede una libreria o una skill. Ogni estrattore è una voce del catalogo delle skill: se manca, la Chat dice quale serve e **chiede il permesso di installarla**; senza il sì, il file resta allegato ma illeggibile, e lo si dice.
4. **Immagini**: percorso attuale (`image_url`). Con il modello avviato senza visione (`"vision": "off"`) la Chat lo dice invece di far ignorare l'immagine in silenzio.
5. **Audio e video con callAPIcall** (client già esteso): `start_media_analysis` (invio a blocchi, mai in memoria intera) → `wait_media_analysis` (la Chat mostra fase e percentuale) → `media_transcript` → `format_transcript` ("[mm:ss] Speaker: testo"). Il testo entra come qualsiasi altro allegato di testo; il riassunto del servizio è un di più, non una fonte.
   - Il servizio fa **un lavoro alla volta**: se risponde "occupato" la Chat lo dice e riprova; se non risponde, propone di avviarlo. Dal video prende solo il suono (ffmpeg): i fotogrammi non vengono letti.
   - **Privacy**: il servizio è locale (127.0.0.1), ma il suo passo di riassunto usa il cervello che l'utente ha scelto in callAPIcall. Richiesta di estensione a quel progetto, da fare solo se l'utente vuole: un campo `analyze=0` per saltare il riassunto e restituire solo la trascrizione. **Non modificato**: è un altro repository.
6. **File prodotti**: lo strumento `write_output_file` scrive in `<dati utente>/outputs/<conversazione>/`; la Chat mostra il pulsante "Scarica" che porta a `GET /api/outputs/<id>` con `Content-Disposition: attachment`. Nel browser si usa il selettore di salvataggio quando c'è, altrimenti il download normale. In `BasaltWinGUI.exe` serve un **ponte WebView2** (`DownloadStarting` con finestra "Salva con nome" come predefinito): modifica dell'eseguibile, da verificare di giorno.
7. **Sicurezza**: tutto ciò che esce da un file è **dato non fidato** (può contenere istruzioni per il modello); gli estrattori e le skill girano dal gestore dei permessi e dalla sandbox dell'harness, non dal browser; l'invio a callAPIcall è un'azione di rete verso un servizio locale, con il suo Bearer.

**Strategie per i documenti nel prompt (migliori pratiche, fonti in §9)**
- **Documenti in alto, domanda in fondo.** Nel turno dell'utente gli allegati vengono prima, poi la richiesta: le prove di Anthropic danno fino al 30% di qualità in più per ingressi lunghi con più documenti. Vale anche per la cache: ciò che precede non cambia nei turni successivi.
- **Ogni documento in un blocco con i suoi dati**: `<documents><document index="1"><source>nome</source><document_content>…</document_content></document></documents>`, con in più tipo, dimensione, intervallo mostrato e se è **troncato** (`shown="1-400 of 5200 lines"`). Così il modello non confonde le fonti e sa che cosa non ha visto.
- **Citare prima di rispondere.** Per documenti lunghi (oltre 20.000 token) la richiesta chiede prima le citazioni pertinenti (`<quotes>`) e poi la risposta; opzione nelle impostazioni, attiva da sola oltre soglia. Per le registrazioni, le citazioni portano l'istante `[mm:ss]`.
- **Riferimenti leggeri e lettura a richiesta** (*just in time*): nel contesto entra il **manifesto** (id, nome, tipo, dimensione, indice dei titoli o schema delle colonne, prime righe) e il contenuto si legge con `read_attachment` (intervalli) e `search_attachment` (BM25 sulle parti). Per i dati tabellari: schema, prime righe e statistiche, non tutte le righe; i calcoli si fanno con uno strumento, non nella testa del modello.
- **Intero o a pezzi**: la scelta tra caricare tutto e recuperare parti riusa `knowledge.choose_strategy` (oggi per i documenti di riferimento): se il totale sta in una frazione del contesto si carica intero, altrimenti si recuperano le parti con intestazione di contesto.
- **Dato, non istruzione**: il contenuto di un file è racchiuso come dato non fidato (`<untrusted_data>`) e il prompt di sistema lo ripete; l'estrazione e le skill passano dai permessi.
- **Con il passare dei turni**: un allegato vecchio si sostituisce con il suo manifesto (potatura per mascheramento, già nello schema di contesto 2b), mai riassunto in prosa; le citazioni e gli identificativi si conservano alla lettera.
- **Immagini**: prima del testo della richiesta, ciascuna con un'etichetta (`Immagine 1: nome`), risoluzione limitata e conteggio dei token (oggi stima fissa di 1.280 in `chatctx.py`); per i dettagli, ritaglio e ingrandimento. Con la visione spenta la Chat lo dice, non ignora l'immagine.
- **File prodotto**: dopo la scrittura si **riapre e si verifica** (si apre, ha ciò che era richiesto) prima di offrire il pulsante di salvataggio (Fase 2c).

**Ordine di lavoro**
- **4b-1** (senza strumenti, vale già nella Chat attuale): deposito, testi e dati diretti, immagini con avviso sulla visione, contatore del contesto che include gli allegati.
- **4b-2** (dopo la Fase 3): `read_attachment`, estrattori docx/xlsx (libreria standard), catalogo delle skill con proposta di installazione (pdf).
- **4b-3**: audio e video con callAPIcall nella Chat (avanzamento visibile, "occupato", "non avviato").
- **4b-4**: file prodotti, pulsante "Scarica", ponte WebView2.
- *Fatto*: il **client di callAPIcall per audio e video** con servizio finto nei test (contratto letto dal servizio vero: `api_analysis.py`).
- *Test*: un file per famiglia con il `MockEngine`; prova di iniezione dentro un pdf e un docx; file troppo grande per il contesto; servizio occupato e spento; salvataggio con la finestra di sistema (coda di giorno).
- *Accettazione*: allegare un pdf, uno xlsx e una registrazione, chiedere una sintesi e una modifica, scaricare il file modificato con la finestra di salvataggio.

### Fase 5 — Una GPU, più chiamanti (M) · *prima misurare*

- Misure di §6 (perdita di cache alternando funzioni, tempo di caricamento a freddo, costo di avvio per turno).
- **Priorità nella coda del motore**: Chat > riesame del planner > corse in background; i subagenti restano sequenziali finché la misura non dimostra un guadagno.
- `prompt-cache` portata almeno al numero di funzioni locali attive, così alternarle non cancella le cache *(da misurare)*.
- *Accettazione*: una corsa lunga dell'agente non fa attendere la Chat oltre un limite dichiarato; i numeri sono etichettati con il limite di potenza della GPU.

### Fase 6 — Modelli, Providers e Qwen Loader (XL) · *decisioni principali prese (E, K); parte in parallelo alle Fasi 2–4*

**Perimetro (vincolo A):** riguarda solo le funzioni **interne** di Basalt. Le app esterne vedono solo il modello caricato; per loro esiste al più il **preset di inferenza di default**, che una richiesta può comunque sovrascrivere.

1. **Scheda Modelli** con due sottoschede (decisione K): **Providers** (in alto la tabella delle funzioni, sotto i provider e i modelli, con una striscia di stato comune: che cosa è caricato, VRAM, chi lo usa) e **Qwen Loader** (la scheda Loader attuale, spostata).
2. **Funzioni**: Chat, Esecutore, Planner (riesame), filtro Jev, compattazione del contesto, valutazione, App esterne (solo preset; rotta bloccata sul modello caricato). Ognuna ha rotta, preset (ragionamento, budget, `max_tokens`, comportamento a contesto pieno, schemi di contesto della Fase 2b), classe di dati e priorità. Precedenza: richiesta, funzione, provider, modello.
2b. **Jev (TypeSafe AI) come tipo di provider di llmswitch.** Non è un provider di chat: risponde a **domande tipizzate** (scelta, punteggio, probabilità `noul`) su `POST /v1/systemone` con il modello `jev-latest`. Quindi llmswitch riceve una capacità nuova, **"giudizio"**, accanto a "chat" e "embedding", e un consumatore "giudizio" non può essere puntato a un provider di sola chat (e viceversa). Regole che restano come ora:
   - la funzione che lo usa è il **filtro Jev** del planner (triage); le soglie (0,80 sul `noul`), il consenso al cloud, la pseudonimizzazione e i campi della scheda restano nelle impostazioni del planner, non in llmswitch;
   - **solo estratti**, mai documenti o pagine intere (il consumo è in token d'ingresso);
   - la chiave sta nel gestore credenziali e non lascia questo PC, né nei log né nella pagina;
   - il tetto di spesa per pagamento (già confermato) vale anche qui;
   - se il servizio manca o la chiave non c'è, il triage ricade sul comportamento locale già esistente, con l'origine segnalata (`source`).
   Il triage oggi chiama Jev con un client suo (`harness/planner/triage.py`): passa a llmswitch mantenendo gli stessi test, compresa la risposta reale registrata in `tools/jev_batch_extremes_results.json`.
3. **Interfaccia stretta sul loader** (stato, "assicura caricato con questo profilo", "è inattivo?", scarico): il loader resta a sé stante.
4. **Caricamento automatico** quando una funzione sceglie Qwen:

   | Situazione | Comportamento |
   |---|---|
   | già caricato col profilo giusto | si usa |
   | memoria vuota | si carica col profilo della rotta, altrimenti Machine Default; le richieste aspettano e la pagina dice per chi |
   | caricato con un altro profilo | **si usa com'è** con un avviso; ricarica solo se la funzione ha la spunta "può ricaricare" **e** il motore è inattivo (un ricaricamento perde la conversazione) |
   | un altro grande modello occupa la VRAM | rifiuto spiegato (regola `fits_in_vram` di llmswitch) |
   | scarico | mai di default; per inattività solo se impostato |

5. **Livelli di ragionamento**: tabella di traduzione tra i 7 livelli di llmswitch e `effort` (nessuno, basso, medio, alto) più `reasoning_budget_tokens`.
6. **Guardia sui dati**: una funzione che tratta dati personali (planner, memoria) non può avere una rotta cloud senza consenso esplicito, come già per il filtro Jev; i ripiegamenti vanno solo verso rotte altrettanto o più locali.
7. **Tetto di spesa** per provider a pagamento (l'utente ha già confermato il tetto per pagamento e la soglia Jev 0,80), applicato dal chiamante interno.
8. **Chiavi** nel gestore credenziali di Windows e non in un JSON in chiaro. Il **registro è dedicato a Basalt** (decisione E): llmswitch viene puntato a un file nella cartella dei dati utente di Basalt, con scrittura atomica; si può **importare una volta** dal registro condiviso (copia, non riferimento). Le scelte delle funzioni rimandano ai provider per nome.
9. **Abbonamenti e provider a riga di comando nel primo rilascio** (decisione E), con tre cautele:
   - **una porta per fornitore**: si abilita solo dopo che l'utente ha letto le condizioni, come i connettori di pagamento, con un avviso nella pagina; il README di llmswitch segnala il rischio con i termini di servizio e il testo di licenza va visto con l'avvocato (Fase 8);
   - **capacità dichiarate**: un provider a riga di comando (Codex, Claude Code) è un agente, non un completamento: serve alle funzioni di testo (riassunti, compattazione) e non al ciclo dell'Esecutore;
   - i token di accesso stanno nel gestore credenziali, mai nei log.
9b. **Dipendenza `requests`** aggiunta a Basalt (decisione E), con le sue note di licenza in `THIRD_PARTY_LICENSES`; compare nel pacchetto di installazione (Fase 8).
10. **Confronto con dati**: `harness.eval` esegue lo stesso insieme di compiti su due rotte e dice quale conviene (pass^k, costo, cache).
- *Test*: ogni riga della tabella di caricamento; il contratto della Fase 0 invariato; chiave mai nei log né nella pagina; precedenza dei parametri.
- *Accettazione*: cambiando la rotta di una funzione da una sola tabella cambia il modello che quella funzione usa, e le app esterne continuano a vedere solo il modello caricato.

#### Impostazioni dei giudici (decisione R)
Sezione **Giudici** nelle impostazioni: una riga per caso d'uso con un elenco *Spento / Modello caricato / Jev*. **Jev** è disponibile solo dopo aver inserito la chiave (che resta sulla macchina, mai in log né commit) e dato il consenso, che vale **per il caso d'uso** e si può revocare. A Jev vanno **solo estratti** (mai file né pagine intere), come deciso per la conoscenza; il triage e la pertinenza usano le domande tipizzate (scelta, punteggio, probabilità). Un caso d'uso senza Jev attivato ricade sul modello caricato o si spegne, e l'interfaccia lo dice. Il costo in token verso Jev si conta e si mostra.

### Fase 7 — Ciò che resta delle specifiche (intercalata)

| ID | Lavoro | Taglia | Chi lo sblocca |
|---|---|---|---|
| 7a | **Modifiche trattenute fino al via**: oggi si applicano e poi si annullano per blocco; tenerle in sospeso finché non sono approvate | M | codice |
| 7b | **ACP in un client vero** (Zed o JetBrains): prova con diff e permessi; poi `fs/*`, `session/load`, reporting verso i pannelli | M | utente al PC, per la prova |
| 7c | **MCP**: OAuth e `subscriptions/listen` | M | codice (OAuth, prima verifica delle specifiche) |
| 7d | **LSP e diagnostica**; **worktree paralleli**; fork e ripresa della sessione; time-travel della conversazione (distinto dal rewind dei file) | L | codice |
| 7e | **Canvas a nodi con ramificazioni**, council con misura A/B, Mermaid: **si fanno** (decisione J), ultimi nell'ordine, con alternativa lineare accessibile e misura A/B del council prima di dichiararlo utile | L | codice, poi prova dal vivo |
| 7f | **Misure vere**: sottoinsieme di SWE-bench Pro o Terminal-Bench, BFCL, baseline con altri harness (Claude Code, OpenCode, Goose) sul server di Basalt, metriche di cache e TTFT dal registro delle richieste | XL | utente al PC con GPU libera |
| 7g | **Isolamento reale di file e rete (D5)**: utente Windows dedicato e regole del firewall | L | utente (amministratore, installatore) |
| 7h | **Connettori di pagamento reali**, uno alla volta dopo la lettura delle condizioni del fornitore; documentare i limiti noti del livello 4 | L | utente, legale |
| 7i | Prodotti separati (assistente, accesso remoto, servizio di sistema) | XL | decisione D7 già presa: dopo l'harness |

### Fase 8 — Prima di qualunque rilascio (S)

- Ricostruire `dist/stage` (contiene codice vecchio).
- `requests` (deciso: aggiunta): inserirla nei requisiti e nel pacchetto, con le note di licenza.
- Licenza (D3): attuazione dopo il testo dell'avvocato e la mappa di titolarità; scadenza e revisione legale del livello 4.
- Versioni, nomi, pacchetto: decisioni dell'utente.
- `docs/` aggiornati per le fasi chiuse; `docs/precision.md` invariato (nessuna modifica di motore).

---

## 5. Coda dei test di giorno: voci da aggiungere

Alle 8 voci di `tests/QUEUE_daytime.md` si aggiungono, quando le fasi relative sono chiuse:
- Fase 2/4: una conversazione vera di più turni con Qwen, con strumenti e con "solo domanda".
- Fase 3: `planner_chat` con Qwen, e la prova avversariale con contenuti ostili veri.
- Fase 5: perdita di cache alternando Chat ed Esecutore; tempo di caricamento a freddo.
- Fase 6: cambio di funzione da una rotta all'altra con Qwen da caricare, e rifiuto quando la VRAM è occupata.

Nessuna di queste si lancia senza l'utente al computer.

---

## 6. Da misurare prima di costruire (nulla è stato misurato)

1. **Costo di avvio di un sottoprocesso per turno** (Fase 2), per scegliere tra processo per turno e processo a vita lunga.
2. **Perdita di cache** quando due funzioni si alternano sul motore, con uno, due e tre slot (Fase 5).
3. **Tempo di caricamento a freddo di Qwen** dal profilo di caricamento e da Machine Default (Fase 6).
4. **Latenza della risposta a pezzi** attraverso il server (Fase 2).
5. **Quanti passi di planner la Chat usa davvero** su richieste semplici (Fase 3): serve a decidere quando la modalità "solo domanda" basta.

Ogni numero porta l'etichetta del limite di potenza della GPU; le valutazioni sono prove di sviluppo, non numeri da citare.

---

**Misure aggiunte (24/10)**
- **Arresti precoci senza ragionamento**: tasso su 100 richieste con e senza decodifica speculativa (`spec: 0` contro `spec: 7`, a parità di cronologia e parametri) e con parametri di campionamento diversi. Serve un riavvio del server con `spec: 0`.
- **Pipeline per turno**: quota di vincoli rispettati con e senza verifica; chiamate e secondi in più.
- **Capacità**: token del prompt con elenco piatto e con albero (282 file reali); precisione dei primi 5 risultati di `find_capability`.
- **Allegati**: token e qualità con documento intero, a pezzi e con citazioni prima.

## 7. Decisioni aperte (all'utente)

Le decisioni di §1 sono prese. Restano:

1. **Nuovi tentativi**: i valori di partenza (numero di tentativi, attese, finestra delle ripartenze) li propongo io in Fase 2 e li correggi con i dati.
2. **llmswitch, dettagli**: scarico per inattività (proposta: mai di default); quali abbonamenti e provider a riga di comando nel primo rilascio, e in che ordine si abilitano; quali domande tipizzate di Jev oltre al triage (ad esempio pertinenza di estratti per la conoscenza).
3. **Benchmark pubblico** da usare (SWE-bench Pro o Terminal-Bench) e quali altri harness mettere a confronto.
4. **Isolamento D5, connettori di pagamento, licenza, contenuti del rilascio**: decisioni dell'utente, non del codice.
5. **`/v1/vram` e le rotte `/api/*` di scrittura** (impostazioni, profili, piani, agente, giornali): chiudere anche queste ai client con lo
   stesso segreto (la pagina lo riceverebbe come cookie `HttpOnly` quando carica `/`, senza che il suo codice lo legga) oppure
   lasciarle come sono. `/v1/vram` serve ai programmi che prestano VRAM: chiuderla li interrompe.
6. **Allegati, tetti** (decisi: quelli delle impostazioni di inferenza) e **estrazione locale** per testi, dati e immagini (supposta): confermare.
7. **callAPIcall, riassunto**: chiedere a quel progetto un campo `analyze=0` (solo trascrizione) perché il testo dell'utente non passi dal suo cervello? Altro repository: non si tocca senza un sì.
8. **Ponte WebView2 per il salvataggio** (modifica di `BasaltWinGUI.exe`): da fare nella 4b-4.

9. **Analisi visiva e capacità** (O): confermare il disegno della Fase 3b (rilevamento a sola lettura delle fonti elencate, nulla attivo senza approvazione, albero con ricerca BM25, strumento generico per le voci scoperte); dire dove sta, se esiste, il tuo `skills.md`; accendere la visione sul tuo PC (`vision: dynamic` se il file ha il proiettore) è una modifica di configurazione che chiede il riavvio.
10. ~~Pipeline per turno~~: **deciso**, completo come predefinito (Q); giudice scelto per caso d'uso dalle impostazioni, Jev attivabile da lì (R).
11. **Arresti precoci del modello**: la prova con `spec: 0` è fatta e li esclude come causa (§ Avanzamento). Resta da decidere se offrire, nelle impostazioni di inferenza, una **penalità sul token di fine turno** (opzione, spenta di default; cambia la distribuzione del modello e va documentata in `docs/precision.md`), e se provare un'altra quantizzazione dello stesso modello (decisione tua).

## 8. Rischi

- **Iniezione nei piani**: una modifica nata da contenuto non fidato non si applica senza approvazione, e la cancellazione chiede sempre conferma. È struttura, non filtro; va provata con la suite avversariale.
- **Conferma per dichiarazione diretta**: il controllo della citazione frena i casi comuni ma una citazione banale lo supera; la difesa vera è che ogni operazione sia visibile subito e annullabile.
- **Pulsanti**: un'etichetta diversa dal `value` sarebbe un inganno; la pagina mostra sempre ciò che verrà inviato.
- **Prefisso in cache**: un riassunto dei piani o una politica di contesto che cambiano a ogni turno annullano il riuso del motore; è una misura, non un'ipotesi.
- **Chat più lenta** per le domande semplici se ogni messaggio entra nel ciclo con strumenti: la modalità "solo domanda" e la misura 5 servono a questo.
- **Una sola GPU**: Chat, riesame e corse competono; senza la priorità della Fase 5 un compito lungo blocca la conversazione.
- **Dipendenze**: llmswitch porta `requests` nel server, che oggi usa solo la libreria standard.

## 9. Fonti

- [llama-swap, configurazione](https://github.com/mostlygeek/llama-swap/wiki/Configuration) (gruppi, scarico per inattività, alias)
- [LiteLLM, budget e ripiegamenti](https://docs.litellm.ai/docs/proxy/budget_fallbacks)
- Interne: `the_way_to_the_moon.md` (§4.8, §4.20–4.24, §8), `conquer_of_the_moon.md`, README di `C:\projects\llmswitch`.
- [Anthropic, prompt per contesti lunghi](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) (documenti in alto, domanda in fondo, blocchi `<document>` con `<source>`, citare prima di rispondere, formati strutturati per lo stato, immagini e ingrandimento)
- [Anthropic, Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (contesto come risorsa finita, riferimenti leggeri e recupero *just in time*, rivelazione progressiva, compattazione, note strutturate, sotto-agenti)
- [Anthropic, tool search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) (definizioni caricate a richiesta; oltre 10 strumenti o 10.000 token di definizioni; la scelta peggiora oltre 30–50 strumenti; ricerca BM25 o regex; ricerca propria con incorporamenti; il prefisso resta intatto)
- Skill con rivelazione progressiva: nel prompt solo nome e descrizione, il corpo con uno strumento di caricamento ([Vercel](https://vercel.com/academy/build-ai-agent-harness/skills-system), [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/agents/skills)); le fonti non primarie sono indicative.
- Testo dell'utente: i **cinque livelli di comprensione** (modello concettuale per progettare agenti, non classificazione scientifica) e la pipeline interpretazione, intento, contesto, validazione, pianificazione, esecuzione.

## 10. Verifica di allineamento ai principi (24/10/2026)

Letto il codice di `harness/` e `serve/`, senza eseguire il modello. **Giudizio: l'harness è allineato sul principio di fondo** (il modello pianifica, l'applicazione esegue, controlla e può rifiutare: permessi, sandbox, punti di ripristino, audit, rilevatore di cicli, dati non fidati, prefisso stabile per la cache) **ma non separa le capacità in fasi e non verifica i vincoli**.

| Livello | Dove sta oggi | Stato | Differenza | Si risolve in |
|---|---|---|---|---|
| 1 Letterale | il modello, dentro la sola inferenza del ciclo (`AgentLoop.run`) | non separato | nessuna scheda dei requisiti: numeri, formati e vincoli non vengono estratti né tenuti | 2c |
| 2 Semantico (intento) | il modello; il triage Jev c'è solo per gli eventi del planner | assente per le richieste | scopo e pubblico non sono un dato dell'harness | 2c, 3 |
| 3 Contestuale | `ContextBuilder` (blocchi fissati in ordine, memorie in coda e marcate non fidate), `MemoryStore` FTS5, `knowledge.choose_strategy` (intero o a pezzi), compattazione e mascheramento, `chatctx` (ferma, finestra, compatta) | **parziale** | il recupero (`retrieved_message`) vale solo in headless e ACP, non nella Chat; scopi e piani non entrano; allegati assenti; nessuna ricerca per capacità | 2, 2b, 3, 3b, 4b |
| 4 Vincoli | permessi, hook, rilevatore di cicli; nel server la risposta strutturata con validazione e nuovi tentativi | **lacuna principale** | il ciclo termina quando il modello risponde senza strumenti: nessuna verifica di lunghezza, formato o requisiti, nessun secondo tentativo guidato | 2c |
| 5 Operativo | pianificazione implicita nel ciclo, planner separato non collegato alla Chat; esecuzione e permessi nell'applicazione | allineato sul principio | tutti gli schemi degli strumenti a ogni turno; catalogo delle skill piatto; nessuna scelta a richiesta | 3, 3b |

**Analisi visiva: la risposta.** *C'è* una lettura delle immagini nel motore (il proiettore del modello, modi `dynamic`, `on`, `peer`, `cpu`; con `dynamic` quando il file ha il proiettore). Le differenze rispetto a ciò che descrivi:
1. **Sul tuo PC è spenta** (`vision: off` in `basalt_config.json`; `/v1/status` dice `vision.enabled: false`): la Chat oggi non può leggere immagini.
2. **L'harness non la usa**: il client invia solo testo, ACP dichiara `image: false`, un'immagine restituita da un MCP diventa un segnaposto di testo; nessuno strumento `analyze_image`.
3. **Non è un sistema di analisi**: niente ritaglio o ingrandimento, OCR, lettura di grafici e diagrammi, fotogrammi di video.
4. **Nessuna impostazione** per scegliere tra la soluzione integrata e una skill, un plugin o un MCP; **nessun catalogo delle capacità**; **`skills.md` non esiste**.
5. **Skill, plugin e MCP**: `SkillCatalog` legge solo le cartelle passate in `skill_paths`; non legge i plugin né le configurazioni MCP; le voci sono piatte, senza etichette né ricerca, e tutte nel prompt (282 file nei tuoi plugin).
