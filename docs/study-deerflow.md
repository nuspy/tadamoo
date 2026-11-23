# Studio di DeerFlow (bytedance/deer-flow), 23 novembre 2026

Letto dal codice (clone superficiale, MIT, 290.000 righe di Python nel backend, versione del 23/11/2026). Niente è stato eseguito. Chi è DeerFlow: un
harness di agenti su LangGraph pensato per un **server con più utenti** (database SQL, accessi e SSO, assegnazione dei lavori a «proprietari»,
dieci canali di messaggi, una trentina di fornitori di ricerca e di sandbox). Noi siamo un sistema personale e locale: molte cose loro non servono.

Ogni riga dice se è **verificato** (letto nel loro codice e cercato nel nostro) o **plausibile, non provato** (con la prova più economica).

## Dove fanno meglio

1. **Disciplina delle release** (verificato). `RELEASING.md`: la versione compare in 5 file più l'etichetta git, `scripts/verify_versions.sh` e la CI
   bloccano la release se non coincidono; `CHANGELOG.md` con `[Unreleased]`; candidate di release; la pubblicazione parte dall'etichetta.
   Da noi la versione è sparsa e già non coincide: `VERSION` dice 1.14, `mobile/pubspec.yaml` dice `1.0.0+1`, le note in `docs/releases/` parlano di 1.12.8.
2. **Un `AGENTS.md` per modulo** (verificato: 64 file contro il nostro unico). Ognuno dice le regole del modulo («l'ordine è comportamento», «la identità
   viene dal server») e come verificarlo. Aiuta gli agenti che lavorano sul codice.
3. **File di contratto condivisi tra server e client** (verificato). `contracts/*.json` (10 file, per esempio `subagent_status_contract.json`) sono letti
   dai test del backend e dal frontend: se uno dei due cambia forma il test dell'altro si rompe. Noi abbiamo web, Android e Telegram che leggono gli stessi
   eventi senza un file comune.
4. **Errori del modello da fornitori esterni** (verificato). Rispettano `Retry-After`, hanno un interruttore (circuit breaker) e una coda di ammissione
   (`llm_error_handling_middleware.py`, `models/request_admission.py`); le classi di errore si decidono da campi strutturati e non dalle parole del messaggio.
   Il nostro `error_classifier.py` usa lo stato HTTP e parole del testo, e `Retry-After` lo leggiamo solo per Telegram. Per il motore locale basta; per
   Z.AI o Codex con raffiche di 429 potrebbe contare (non misurato).
5. **Le citazioni dei subagenti si controllano** (verificato). Il rapporto di un subagente cita ricevute `[rN]` delle chiamate fatte; il genitore le confronta
   con il registro reale (`receipt_verification.py`), come prova di esecuzione e non di verità. Noi non abbiamo niente di simile per `spawn_subagent`.
6. **Riparazione della cronologia** (plausibile). `dangling_tool_call_middleware.py` mette un risultato finto alle chiamate senza risposta e toglie i
   risultati orfani, perché i fornitori severi rifiutano la richiesta. Il nostro ciclo chiude le chiamate quando si annulla (`agent_loop.py:1028`) e la
   compattazione non taglia mai tra chiamata e risultato, ma una cronologia ripresa dopo un crollo del processo potrebbe restare scompagnata.
   Prova: uccidere l'harness a metà di un gruppo di chiamate, riprendere e guardare se il fornitore risponde 400.
7. **Compattazione a pezzi** (plausibile). Riassumono tutta la parte tolta a pezzi, ognuno con il riassunto precedente; applicano solo se tutti i pezzi
   riescono; dopo un fallimento aspettano cinque minuti; ricordano i riassunti identici. Noi leggiamo estratti (fino a 96.000 caratteri) e salviamo gli
   originali in un file. Su una cronologia molto lunga il loro metodo copre di più. Prova: compattare una conversazione da 500.000 caratteri e confrontare i fatti.
8. **Il segno «letto» vive nei messaggi** (ipotesi). Il loro controllo «leggi prima di scrivere» tiene l'impronta nel messaggio del risultato: se la
   compattazione toglie quel messaggio, il permesso sparisce con lui. Il nostro (`tools/filesystem.py`) tiene lo stato nello strumento. Prova: leggere un
   file, compattare, sovrascriverlo senza rileggere.

## Dove siamo alla pari o avanti (verificato)

Ciclo e ripetizioni (nostro `LoopDetector`, `OutcomeGuard`, `RepeatGuard`, uscita degenerata); sovrascrittura (impronta, file cambiato, riduzione
sospetta, sintassi, rilettura dopo la scrittura); risultati lunghi salvati in un file con il puntatore; compattazione (controllo degli identificatori,
fatti, puntatori, testo recintato come dato); ricerca di strumenti rinviati (`capabilities.py`); obiettivi (il nostro Target Loop ha criteri, giudice e
prove); versioni e ripristino delle abilità personali (`personal_skills/store.py`, 10 versioni); uso di Jev come giudice rapido; ricerca nelle
conversazioni passate.

## Cose loro che non proponiamo

- Togliere dalle variabili d'ambiente dei comandi tutto ciò che sembra una chiave (`sandbox/env_policy.py`) e il filtro di autorizzazione prima di ogni
  chiamata (`GUARDRAILS.md`): sono regole di sicurezza, e per le tue regole le introduci tu (il codice libero con le chiavi nell'ambiente è una tua decisione).
- Database SQL, accessi con SSO, lavori assegnati a proprietari, concessioni di permessi: servono a un server con più utenti.
- Dieci canali di messaggi (Slack, Discord, Feishu, WeChat, WeCom, DingTalk, QQ, GitHub...) e le decine di fornitori: è un tema di ampiezza da decidere a parte.

## Idee in ordine di costo

Da 1 a 3 sono lavoro piccolo e certo; da 4 a 8 vanno misurate prima.
