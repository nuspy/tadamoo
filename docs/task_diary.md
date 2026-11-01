# Diario dei task

Ultimo aggiornamento: 31 ottobre 2026, sera. Si aggiorna a ogni task nuovo ricevuto e a ogni task completato
(regola nel `CLAUDE.md` globale). Se hai perso il filo, parti da «In corso adesso» e da «Decisioni tue in attesa».

## In corso adesso

- Esperimento E9 (cosa sa ancora l'agente dopo la compattazione, spenta contro accesa): primo giro k=5 su `long45` chiuso il 1 novembre, 600 W. Solo 7 prove su 15 valide: 8 sono cadute per «connessione chiusa» dalla macchina (nulla nel log del server). Dalle 7 valide: compattazione spenta 0,61 e 0,74 di domande giuste (controllo), accesa 0,28, con un rumore del controllo di 0,13: la compattazione accesa ricorda MENO. Le domande sbagliate dall'accesa sono quasi tutte nomi e valori che nessun passo dopo richiedeva (nome della prima funzione, `LIMIT`): un riassunto è fatto per scartarli. Per questo ho diviso le domande in due gruppi (cosa serviva dopo: conteggi del registro, se un modulo aveva l'errore, regole; cosa no) e la regola ora decide sul primo (`e8816449`). Il client ora riprova le connessioni chiuse (`784bd2fa`). Da rifare: E9 con k=5 sulle nuove domande, quando l'altra sessione scrive FINE (la sua riprova dei 3 casi è in corso).

- Studio del confronto tra memorie (1 novembre, iniziato mentre E9 gira, solo lettura): nostra memoria riletta in `docs/harness.md`; Claude-Mem e Obsidian visti dalle fonti pubbliche. Nessun codice toccato. La proposta è scritta in `docs/harness-study/memory.md` (indice globale con note, pezzi di conversazione e schede per conversazione; strumenti `search_past` e `open_past`); decise il 1 novembre: routine escluse dall'indice di default, con un interruttore per ogni routine nella GUI che ne accende l'indicizzazione (etichettata col nome); indice non cifrato; la modalità «Solo il modello» della scheda Chat resta e viene indicizzata (invierà i turni al server). Decisione tua del 1 novembre: la memoria sull'utente è accesa di default, i suoi interruttori si cambiano da Impostazioni > Memorie e valgono per tutte le chat e i progetti, anche nuovi e dopo un riavvio (il file è uno solo per la persona). Fatto nel codice e nei test della cartella `tests/`; da eseguire ancora `serve/tests/test_memories_api.py` e la prova nell'app vera, dopo E9. `cloud_ok` (note verso provider esterni) resta spento. Costruito il 1 novembre (non ancora tutto committato): indice globale delle conversazioni (`1052d117`), strumenti `search_past` e `open_past`, indicizzazione a fine turno e recupero delle conversazioni già salvate, chat «Solo il modello» che invia i turni al server, interruttore «Ricorda cosa fa» per ogni routine. Mancano: le schede per conversazione (sessione tecnica), l'esperimento, e i test con server locale (`test_past_chat_route`, `test_routines_api`, `test_memories_api`) da eseguire quando E9 ha finito. Proposta su cosa prendere dagli altri harness consegnata nel messaggio del 1 novembre: in attesa delle tue scelte.

## Fatto il 31 ottobre sera (committato, non pubblicato)

- Quattro commit su `next/integration`: `b413b544` contesto (nota unica di compattazione, ricompattazione dagli originali, lettura a pezzi controllata e rifatta se non è buona, nessun limite di tempo); `889a2508` server (sessione tecnica accesa di default, conteggio per uso, rifiuto se non entra, `--kv int8` con avviso, Loader a 30.000 e spenta nei valori sicuri, test adattati, golden rigenerato); `064f709d` registro dei 15 usi con dialogo, sei usi nuovi, funzione `technical` e interruttore generale per provider esterni; `f84dfba3` E9.
- Prove sul modello vero degli usi (GPU libera, 600 W, IQ4_XS): rifiuto se non entra; lettura a pezzi di 213.000 caratteri in 240 s con avanzamento e tasto Ferma; estrazione dagli output 138 caratteri su 115.848 con l'errore giusto; controllo iniezioni 6 su 6; scelta note giusta; riga di avanzamento giusta; suggerimenti compatti; promemoria da 6.000 a 1.272 caratteri; riassunto del Chat da 60.655 a 3.709 token con 38 chiamate tutte nella sessione tecnica.
- Test: `serve/tests` per file e ripetuti. Rispetto al punto di partenza (19 fallimenti su quattro file) il mio albero ne ha meno o pari, ma ogni giro fallisce test diversi per timeout HTTP verso 127.0.0.1 (una connessione ogni tanto non risponde per 21 s), anche sul punto di partenza e anche a macchina quasi ferma. I tre fallimenti veri trovati sono corretti.
- Precisione del `kv int8`: tutte le righe di qualità in `docs/precision` sono già misurate con int8; la nota è scritta. Il confronto diretto fp16 contro int8 non si può fare qui: mancano il file base `.kld` di llama.cpp e wikitext.

## Ancora aperto sul contesto e sulla sessione tecnica

- Il riassunto a pezzi tiene 15 percorsi su 40: l'esperimento E9 e le soglie diranno se basta.
- Piegare a piccoli blocchi solo il materiale già usato; un solo modulo di riassunto per Chat e agente; condizione del 50% della finestra per il promemoria.
- Provider esterni: llama-server dal Loader, secondo modello piccolo in LM Studio o vLLM, involucro per llama.cpp con aggiornamento, prova di `id_slot` su `/v1/chat/completions`. Su Anthropic la risposta JSON si perde.
- Dialogo esteso (15 usi, scelta del provider) non provato nel browser: Edge non rispondeva.
- Memorie, planner e riga di avanzamento non provati sul modello vero dentro una sessione di agente.
- Jev: la funzione `relevance` non è chiamata da nessun codice; nessuna prova dal vivo.
- Compiti già annunciati: riesame delle ispirazioni da altri strumenti, confronto delle memorie (Claude-Mem, nostro sistema, Obsidian).

## In coda, senza GPU

- Unire le rifiniture della casella «Ottimizza il prompt» (commit `e6708707`): sempre spegnibile, riga «Sto riscrivendo il
  prompt…», esperimento E8 esteso a richieste vaghe e lunghe.
- Rifare la suite completa su una macchina ferma (oggi i timeout verso 127.0.0.1 fanno fallire test diversi a ogni giro).
- Il percorso `long_answer` del Chat usa ancora il flusso vecchio delle risposte lunghe: portarlo al flusso nuovo.
- L'app Android non ha l'interruttore «Analisi del prompt», la casella «Ottimizza il prompt» e il selettore del ruolo.
- Ricostruire l'app e il programma di installazione (`compile_and_package.ps1`) e reinstallare: l'app in Program Files
  non è ancora aggiornata.
- Riavviare il bridge di callAPIcall per attivare le due correzioni (appunti dopo la dettatura, Alt+Spazio). Le modifiche
  in callAPIcall non sono ancora committate perché l'albero è condiviso con l'altra sessione.
- Controllo a occhio nell'app vera a 1024, 1440 e 3600 px (riga 81 della coda): interruttore, casella, schede di domande,
  finestra «Risposta lunga», «Salva come file», ruolo Developer, controlli del progetto.

## Dopo l'analisi del contesto (già annunciati da te, 31 ottobre)

- Esaminare uno per uno dove e come si usa la sessione tecnica, trovare altri posti dove usarla, decidere cosa modificare, eliminare o aggiungere, e fare un dialogo per attivarle e disattivarle tenendo conto dei conflitti (accenderne una spegne o rende non attivabili altre).
- Riesaminare le ispirazioni prese dagli altri strumenti (Hermes, OpenClaw, Harness² e altri): come funzionano, cosa manca a noi, errori fatti, buone pratiche, alternative; importarle una per una.
- Memoria a breve e lungo termine dell'harness (comune a tutti i progetti: cosa è stato fatto, l'utente, i suoi gusti): confrontare Claude-Mem riadattato, il nostro sistema proprietario e Obsidian per efficienza e utilità; rileggere la documentazione della funzione.

## In coda, serve la GPU (e l'OK dell'altra sessione, sempre)

- Esperimento sul contesto (E9, da scrivere con regola di vittoria prima): riassunto nostro, riassunto del proxy (modello decide) e misto, su conversazioni lunghe di agente; misure: domande di richiamo dopo la compattazione, riuso della cache, token, tempo, riuscita del compito.
- Le prove con il modello vero, righe 36-88 di `tests/QUEUE_daytime.md`.
- Gli esperimenti E1, E2, E4, E5, E6, E7, E8 (`python -m harness.eval.cases list`). Solo E3 è già girato, con il motore
  finto.
- Misura fatta il 31 ottobre: sessione accesa, nessun rallentamento visibile (125-185 token al secondo); la misura accurata della coda (riga 85) resta.

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
- Prove GPU della sessione tecnica finite (dopo il riavvio del PC per una schermata blu dell'altra sessione, limite 600 W).
  Il server parte in 56 s; VRAM 31,26 GB. Slot lato (30.000 token) accanto al principale (65.536): una richiesta «side» è
  servita dallo slot lato, una conversazione principale non tocca la cache dell'altra (62 token riusati dopo una chiamata
  laterale). Un prompt troppo lungo per la sessione tecnica ripiega sul principale, come previsto. Velocità con la sessione
  accesa: primo token 0,04 s, scrittura da 125 a 185 token al secondo (mediana 170), cioè nessun rallentamento visibile
  rispetto ai circa 141 della misura senza (le misure oscillano molto). Identità dei token: il test via API non la può
  dimostrare, perché anche senza sessione tecnica la stessa richiesta ripetuta dà testi diversi già dopo 11 caratteri
  (non è colpa della sessione tecnica). Per l'identità vera serve lo strumento `greedy_identity` del motore con la sessione
  tecnica accesa, che non ho ancora provato.
- Primo avvio reale della sessione tecnica (GPU, limite 600 W, `--kv int8`): il server parte in 68 s, lo stato mostra la
  sessione corta da 30.000 token accanto a quella principale da 65.536, una richiesta marcata «side» è servita da quella e
  il testo della conversazione principale è identico a quello senza sessione tecnica. VRAM dopo l'avvio: 31,4 GB contro
  30,9 GB senza (circa 0,5 GB in più). Velocità senza sessione tecnica: primo token 0,05 s, circa 141 token al secondo.
- Scoperta: le sessioni parallele richiedono `--kv int8` (regola del motore che già c'era). Ora il server lo aggiunge da
  solo e rifiuta con un messaggio chiaro un altro formato.
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

- `tests/`: 3115 test passati (31 ottobre, testa `bfe43299`). `serve/tests`: ultima misura 1695 passati e 5 che falliscono
  solo su questo PC (4 `RunningServer`, 1 `NoModel`); dopo di allora ho corretto 3 test miei per la regola `--kv int8`.
- Mai visti in un browser: interruttore, casella, schede di domande, finestra «Risposta lunga», selettore del ruolo.
- Qualità con il modello vero: non misurata (stima, piani, correzioni, riscritture, suggerimenti).

## Decisioni già prese

- Contesto (31 ottobre): una sola nota di compattazione, generica, uguale per tutti i sistemi (non dice chi ha compattato: dice che c'è stata una compattazione, con limiti e controlli); tutti i sistemi possono ricompattare se serve e se è ancora possibile; si ricompatta dagli originali, non riassumendo un riassunto (una chiamata in più va bene); l'idea delle risposte astratte entra nella stessa pipeline e solo nel Chat.
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
