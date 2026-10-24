# PIANO — Chat, Planner ed Esecutore, Modelli e Loader, e ciò che manca delle specifiche

**Stato: proposta. Nulla di quanto descritto qui è stato implementato.** Si basa sul ramo `feature/agent-moon` al commit
`8b8f7f76`. Estende `the_way_to_the_moon.md` (§8) e `conquer_of_the_moon.md` senza sostituirli.
Le dimensioni sono taglie relative (S/M/L/XL), senza date: come in §8 delle specifiche, una data senza base è peggio di nessuna.

**Come leggerlo.** §1 le decisioni già prese, §2 l'architettura, §3 lo stato verificato, §4 le fasi di lavoro,
§5 ciò che resta delle specifiche, §6 le misure, §7 le decisioni aperte. Ciò che non ho verificato è marcato *(non verificato)*.

---

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
| L | **Processo a vita lunga** per ogni conversazione, con gestione degli errori, nuovi tentativi per errori e passi falliti, e, a incapacità verificata di procedere, avviso all'utente con richiesta di intervento. | Fase 2. |

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

Ordine: **0 → 1 → 2 → 3 → 4 → 5**; la **6** parte dopo la decisione su llmswitch; la **7** si intercala secondo le risorse; la **8** precede ogni rilascio.
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

### Fase 4 — La Chat come ingresso principale (L)

- **Pagina Chat** su sessioni: scelta "con strumenti" / "solo domanda", risposta a pezzi, **elenco di ciò che è stato fatto** (strumenti usati, esito, piani toccati con collegamento alla scheda Agente e alla vista Piani), **domande e conferme con pulsanti in linea** insieme alla risposta libera (decisioni G e H), pulsante di arresto.
- **Scheda Agente (decisione I): non si riduce.** Resta subito dopo la Chat, con ordine e scorciatoie invariati. Diventa il **cruscotto più completo del planner**: piani attivi con passi e stato, corse, approvazioni e domande in sospeso, punti di ripristino ("Torna indietro"), avvisi del planner, e la possibilità di avviare e correggere. La Chat è la via più automatica allo stesso insieme di dati.
- **Vista Piani**: resta la gestione manuale di dettaglio (grafo, modifica dei passi), sullo stesso archivio (Fase 1).
- Tavolozza Ctrl+K con i nuovi comandi; stringhe in italiano e inglese.
- **La Chat senza strumenti** (oggi) resta raggiungibile come modalità, con le app esterne che continuano a usare `chatctx.py`.
- *Test*: audit con Edge headless (`tools/web_audit.mjs` aggiornato), controlli statici (`serve/tests/test_web_static.py`), nessun `innerHTML`, tutte le stringhe tradotte, nessuno scroll orizzontale da 1024 px in su.
- *Accettazione*: dalla sola Chat si crea un piano, lo si modifica, se ne avvia un passo, si approva un'azione e si legge il resoconto, senza aprire altre schede; **da verificare dal vivo con Qwen** (coda di giorno).

### Fase 5 — Una GPU, più chiamanti (M) · *prima misurare*

- Misure di §6 (perdita di cache alternando funzioni, tempo di caricamento a freddo, costo di avvio per turno).
- **Priorità nella coda del motore**: Chat > riesame del planner > corse in background; i subagenti restano sequenziali finché la misura non dimostra un guadagno.
- `prompt-cache` portata almeno al numero di funzioni locali attive, così alternarle non cancella le cache *(da misurare)*.
- *Accettazione*: una corsa lunga dell'agente non fa attendere la Chat oltre un limite dichiarato; i numeri sono etichettati con il limite di potenza della GPU.

### Fase 6 — Modelli, Providers e Qwen Loader (XL) · *decisioni principali prese (E, K); parte in parallelo alle Fasi 2–4*

**Perimetro (vincolo A):** riguarda solo le funzioni **interne** di Basalt. Le app esterne vedono solo il modello caricato; per loro esiste al più il **preset di inferenza di default**, che una richiesta può comunque sovrascrivere.

1. **Scheda Modelli** con due sottoschede (decisione K): **Providers** (in alto la tabella delle funzioni, sotto i provider e i modelli, con una striscia di stato comune: che cosa è caricato, VRAM, chi lo usa) e **Qwen Loader** (la scheda Loader attuale, spostata).
2. **Funzioni**: Chat, Esecutore, Planner (riesame), filtro Jev, compattazione del contesto, valutazione, App esterne (solo preset; rotta bloccata sul modello caricato). Ognuna ha rotta, preset (ragionamento, budget, `max_tokens`, comportamento a contesto pieno), classe di dati e priorità. Precedenza: richiesta, funzione, provider, modello.
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

## 7. Decisioni aperte (all'utente)

Le decisioni di §1 sono prese. Restano:

1. **Nuovi tentativi**: i valori di partenza (numero di tentativi, attese, finestra delle ripartenze) li propongo io in Fase 2 e li correggi con i dati.
2. **llmswitch, dettagli**: scarico per inattività (proposta: mai di default); quali abbonamenti e provider a riga di comando nel primo rilascio, e in che ordine si abilitano; quali domande tipizzate di Jev oltre al triage (ad esempio pertinenza di estratti per la conoscenza).
3. **Benchmark pubblico** da usare (SWE-bench Pro o Terminal-Bench) e quali altri harness mettere a confronto.
4. **Isolamento D5, connettori di pagamento, licenza, contenuti del rilascio**: decisioni dell'utente, non del codice.

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
