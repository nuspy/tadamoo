# Diario dei task

Ultimo aggiornamento: 31 ottobre 2026, mattina. Si aggiorna a ogni task nuovo ricevuto e a ogni task completato
(regola nel `CLAUDE.md` globale). Se hai perso il filo, parti da «In corso adesso» e da «Decisioni tue in attesa».

## In corso adesso

- Analisi dei sistemi che accorciano il contesto (richiesta del 31 ottobre): compattazione al raggiungimento della soglia, funzioni Billion Context, sessione tecnica che riassume/ripulisce, e gli altri che trovo. Capire come interagiscono (A+B, A+C, B+C, tutti), che perdite stratificate causano, cosa succede se l'utente ne spegne uno a metà, come riconoscere nel contesto ciò che è già compattato. Fatta la mappa e la prima analisi (31 ottobre); ora se ne discute con te prima di cambiare qualsiasi cosa. Solo lettura finora.
- Idea tua da valutare (31 ottobre): due livelli per ogni risposta del modello. Livello operativo: l'harness esegue quello che la risposta chiede. Livello di memoria: nel contesto resta solo un riassunto/astrazione di ciò che è stato fatto (senza spiegazioni, codice, istruzioni degli strumenti). Solo nel Chat, non nella scheda Agentic coding (lì serve il codice); disattivabile. Prima si risolve il punto sopra.
- Sessione tecnica accesa di default (deciso da te il 31 ottobre): il codice è scritto ma NON ancora committato, perché i test
  vanno adattati al nuovo valore predefinito (lavoro rimandato, vedi «In coda»). Fatto: il server la accende da solo (30.000
  token) e la lascia spenta, dicendo il motivo, se `--parallel` è 8, se sono impostate le opzioni di batch del motore o se
  `--kv` da riga di comando non è int8; il `kv` del file passa a int8 con un avviso. Il Loader ha 30.000 come valore predefinito,
  0 per escluderla, e nel piano conta la cache int8. Impostazioni > Inferenza mostra se è accesa e quanto è grande (solo
  lettura); «Ottimizza il prompt» resta grigia senza sessione; l'harness non la usa se è spenta. Nuovo `harness/digest.py`:
  un testo più grande della sessione si divide in pezzi (senza modello), ogni pezzo è riassunto dalla sessione tecnica e i
  riassunti si uniscono; usato dal riassunto della compattazione. Documenti aggiornati.
- Facoltativo: provare l'identità vera dei token con `greedy_identity` e la sessione tecnica accesa (5 minuti di GPU).

## In coda, senza GPU

- Adattare i test al nuovo valore predefinito della sessione tecnica (rimandato su tua richiesta, da fare quando la GPU sarà libera, fra circa 5 ore): molti test di `serve/tests` leggono le opzioni del motore senza `--side-slot` e ora trovano `--kv int8 --batch 2 --side-context 30000`; poi committare il lavoro sulla sessione tecnica (codice, test, documenti). Aggiungere anche i test del Loader (nota «cache int8») e della scheda in Impostazioni.
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

## Dopo l'analisi del contesto (già annunciati da te, 31 ottobre)

- Esaminare uno per uno dove e come si usa la sessione tecnica, trovare altri posti dove usarla, decidere cosa modificare, eliminare o aggiungere, e fare un dialogo per attivarle e disattivarle tenendo conto dei conflitti (accenderne una spegne o rende non attivabili altre).
- Riesaminare le ispirazioni prese dagli altri strumenti (Hermes, OpenClaw, Harness² e altri): come funzionano, cosa manca a noi, errori fatti, buone pratiche, alternative; importarle una per una.
- Memoria a breve e lungo termine dell'harness (comune a tutti i progetti: cosa è stato fatto, l'utente, i suoi gusti): confrontare Claude-Mem riadattato, il nostro sistema proprietario e Obsidian per efficienza e utilità; rileggere la documentazione della funzione.

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
