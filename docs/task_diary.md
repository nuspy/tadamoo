# Diario dei task

Ultimo aggiornamento: 31 ottobre 2026, mattina presto. Si aggiorna a ogni task nuovo ricevuto e a ogni task completato
(regola nel `CLAUDE.md` globale). Se hai perso il filo, parti da «In corso adesso» e da «Decisioni tue in attesa».

## In corso adesso

- Test GPU della sessione tecnica (primo avvio reale, con e senza, per misurare il costo sulla velocità): lo script è pronto in
  `C:\TEMP\claude\E--Projects-basalt526e1fa-56b6-4854-af76-3c412ae1a60c\scratchpad\side_smoke.py`. Il lancio è stato
  bloccato dal sistema di autorizzazione: lo lancia il proprietario a mano. L'altra sessione ha dato il via libera e dopo
  userà la GPU a lungo (circa 3 ore, poi stanotte circa 4,5 ore): niente GPU da basalt in quella finestra.
- Suite completa dell'ultima testa: da rifare dopo le ultime unioni.

## In coda, senza GPU

- Unire le rifiniture della casella «Ottimizza il prompt» (commit `e6708707`): sempre spegnibile, riga «Sto riscrivendo il
  prompt…», esperimento E8 esteso a richieste vaghe e lunghe.
- Rifare la suite completa dopo le ultime unioni (ottimizzazione rifinita, scheda italiana, suggerimenti chiari) e committare.
- Il percorso `long_answer` del Chat usa ancora il flusso vecchio delle risposte lunghe: portarlo al flusso nuovo.
- L'app Android non ha l'interruttore «Analisi del prompt», la casella «Ottimizza il prompt» e il selettore del ruolo.
- Ricostruire l'app e il programma di installazione (`compile_and_package.ps1`) e reinstallare: l'app in Program Files
  non è ancora aggiornata.
- Riavviare il bridge di callAPIcall per attivare le due correzioni (appunti dopo la dettatura, Alt+Spazio). Le modifiche
  in callAPIcall non sono ancora committate perché l'albero è condiviso con l'altra sessione.
- Controllo a occhio nell'app vera a 1024, 1440 e 3600 px (riga 81 della coda): interruttore, casella, schede di domande,
  finestra «Risposta lunga», «Salva come file», ruolo Developer, controlli del progetto.

## In coda, serve la GPU (e l'OK dell'altra sessione, sempre)

- Le prove con il modello vero, righe 36-88 di `tests/QUEUE_daytime.md`.
- Gli esperimenti E1, E2, E4, E5, E6, E7, E8 (`python -m harness.eval.cases list`). Solo E3 è già girato, con il motore
  finto.
- Misura del costo della sessione tecnica sulla velocità della conversazione (riga 85).
- Prova del motore con `--side-slot` acceso: sessione da 30.000 token accanto a quelle normali.

## Decisioni tue in attesa

- Sessione tecnica accesa di default: decidi tu dopo la misura. Hai detto che la capienza del contesto conta più di una
  percentuale di velocità.
- Formato del primo messaggio del Target Loop (unito o separato): si decide con l'esperimento E1.
- Cancello di verifica acceso di default (con te presente / non presidiato): si decide con l'esperimento E2.
- (Deciso: classificatore degli errori acceso di default.)
- Soglie per dichiarare un vincitore negli esperimenti (10, 15 o 20 punti) ed E5 con 3 ripetizioni: puoi cambiarle prima
  di qualsiasi prova.
- Sei domande dell'agente delle risposte lunghe. Mie scelte predefinite: testo intero nella cronologia, scheda prima della
  stima, stima saltata se la lunghezza è dichiarata, flusso nuovo anche nel Chat, cambio del prefisso accettato una volta.
- Rilascio su GitHub (anche come bozza): solo quando dirai che abbiamo finito.

## Fatti

### 31 ottobre
- Classificatore degli errori acceso di default (si spegne con `--no-error-classifier` o dalle Impostazioni): un errore
  che non passa mai (chiave sbagliata, richiesta malformata) ferma il turno e lo mostra; gli errori temporanei si riprovano.
- Prova GPU n. 2, motore vecchio contro nuovo con cache fissata: IDENTICO. Le mie modifiche al motore non cambiano i token.
- Unite le rifiniture dell'ottimizzazione (casella sempre spegnibile, riga «sto riscrivendo», E8 esteso), la scheda italiana
  corretta e i suggerimenti del messaggio successivo come frasi complete.
- Suite `tests/` completa sulla testa precedente: 3102 passati, nessun fallimento.
- Casella «Ottimizza il prompt», solo con la sessione tecnica attiva (commit `45ececa0`), con esperimento E8.
- Sessione tecnica corta da circa 30.000 token: opzione del motore `--side-context`, server `--side-slot`, campo di
  richiesta `cache_slot`, voce «Sessione tecnica» nel Loader (commit `933c4503`). Spenta di default. Il motore compila per
  intero senza errori né avvisi.
- Prova GPU n. 1 (limite di potenza 600 W): motore vecchio contro nuovo diverso al token 62. Causa trovata nei log: la
  cache degli esperti ha avuto dimensioni diverse (9380 contro 9364 posizioni). Controllo con cache fissata: IDENTICO.
- Risposte lunghe in modo proattivo: stima della lunghezza, piano con una domanda per pezzo, scrittura a pezzi, verifica con
  correzioni per posizione. `plan_long_answer` eliminato (commit `e89dea28`).
- Interruttore «Analisi del prompt» a tre stati, spento di default e salvato dopo il riavvio. Ruolo «Developer».
- Sette esperimenti con braccio di controllo e regola di vittoria scritta prima (commit `3b8beeda`).
- callAPIcall: il testo dettato resta negli appunti; Alt+Spazio non apre più il menu delle app.

### 30 ottobre
- Core, strumenti file, Target Loop, Intake e domande, cancello di verifica e guardie, modalità sviluppatore, risposte
  lunghe, compattazione: tutto costruito e unito in `next/integration`.
- Continuazione automatica delle risposte tagliate, parti da 2000 parole (massimo 2500), soglia a 3000 token,
  «Salva come file» accanto a Copia, compattazione accesa di default con avviso rinominato.
- Studi sul motore: la cache riusa solo l'inizio identico; il modello non permette di riusare pezzi in mezzo.

### 29 ottobre
- Strata come secondo motore nel Loader, Billion Context, REA, canary; motore Qwen3.6: corretto il crash del warm-up.
- Sito tadamoo.com, aiforge e personalities online; Personalities su Kubernetes; pgvector nel database condiviso.
- Studi sugli altri agenti (Hermes, OpenClaw, Harness²) e il piano di lavoro.

## Stato dei test (ultima misura)

- `tests/`: 3059 test passati. `serve/tests`: 1692 passati e 5 che falliscono solo su questo PC (4 `RunningServer`,
  1 `NoModel`).
- Mai visti in un browser: interruttore, casella, schede di domande, finestra «Risposta lunga», selettore del ruolo.
- Qualità con il modello vero: non misurata (stima, piani, correzioni, riscritture, suggerimenti).

## Decisioni già prese

- Nessun push e nessun rilascio, nemmeno come bozza, finché non dici che abbiamo finito.
- La GPU si usa solo dopo il via libera dell'altra sessione e le si scrive a fine uso. Con il commit libero sotto i 20 GB
  si ferma tutto.
- Le esecuzioni non presidiate non si fermano mai a chiedere: scelgono l'opzione consigliata e annotano l'ipotesi.
- Analisi del prompt spenta di default; suggerimenti del prossimo messaggio accesi, mai accorciati.
- Compattazione accesa di default, interruttore nell'interfaccia, riassunto sempre locale salvo tua scelta contraria.
- `docs/harness.md` e `docs/experiments.md` possono superare le 300 righe.
- Non toccare `E:\Projects\strata`. Mai chiudere Chrome con `taskkill`.

## Altri progetti aperti

- Personalities: indirizzi di LLM ed embedding, posta, fatturazione, voce e GPU da configurare; pulire i vecchi pod;
  salvare i file dell'overlay in `C:\Projects\Personalities`; creare `aiact.tadamoo.com`.
- Repository del canary: commit locali, nessun push finché non dici sì.
- Modulo «Accesso anticipato» del sito: senza backend.
- Rilascio per Mac: rimandato a quando il progetto è quasi finale.
- Studio sulle chiamate in contesti separati: in parte coperto dalla sessione tecnica.
