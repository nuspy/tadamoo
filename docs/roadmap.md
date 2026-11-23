# LoRA in Basalt-Plus

Gli adapter LoRA nel motore sono fatti, esperti compresi. L'addestramento vero è in corso. In fondo ci sono anche gli altri task del progetto, fatti e da fare. Tocca una voce per le note.

_Aggiornato il 23/11/2026 notte. Generato da `docs/roadmap.json`: non si modifica a mano._

| Stato | Nodi |
|---|---|
| fatto | 27 |
| in corso | 1 |
| decisione aperta | 1 |
| in attesa | 3 |
| non iniziato | 5 |

## Il motore: adapter fusi all'avvio

### 1. Idea e studio iniziale (fatto)

Poter caricare LoRA nel motore è un traguardo a sé, utile anche senza addestrare LoRA fatti su misura per il nostro harness.

**Fatto e problemi risolti**

- Nessun adapter LoRA pubblico esiste oggi per Qwen3.8-Flash-Next.
- ms-swift sa addestrare LoRA su questo modello: la via per crearne uno in futuro esiste già.

### 2. Decisione del disegno (fatto)

Scelta la via: fondere l'adapter nei pesi al caricamento (non applicarlo ad ogni token).

**Fatto e problemi risolti**

- Due vie sul tavolo: fondere l'adapter una volta al caricamento (Via A), oppure applicarlo a runtime a ogni token (Via B).
- Decisione dell'utente: ora solo la Via A; la Via B resta riservata ai due casi limite più avanti.
- Decisione dell'utente: niente richieste in parallelo con adapter diversi — il server ne gestisce una alla volta, e va bene rileggere tutto il prompt quando l'adapter cambia.

### 3. Convertitore PEFT → GGUF (fatto)

tools/convert/lora.py legge una cartella di addestramento PEFT vera e scrive l'adapter nel formato che il motore legge.

**Fatto e problemi risolti**

- Bug nel confronto dei suffissi di `alpha_pattern`: corretto.
- Il primo test non verificava davvero nulla (falso positivo silenzioso): riscritto.
- Le quote di energia per gruppo (router / esperti / altro) erano mischiate: separate.
- Un formato di configurazione annidato non veniva letto: aggiunto.

### 4. Libreria di fusione nel motore (E1) (fatto)

La parte C++ che fonde un adapter dentro una matrice dei pesi densi, in Q8_0 pronto per la GPU.

**Fatto e problemi risolti**

- Bug vero: lo stesso adapter a scala +1 e a scala -1 insieme non si annullava in modo esatto, per l'ordine dei calcoli — corretto.
- Il test usava basi casuali che diventavano quasi sempre NaN o infinito, mascherando errori — corretto.
- La grandezza tipica del delta era stimata con un numero fisso sbagliato — ora si calcola dalla potenza reale dei pesi.

### 5. Collegamento nel motore (E2) (fatto)

I flag --lora/--lora-scaled, il momento in cui l'adapter viene letto, il log e cosa succede se tocca pezzi che il motore non sa fondere.

**Fatto e problemi risolti**

- L'adapter va letto prima dei vettori di controllo: il motore deve sapere quali matrici ha il pacchetto prima della loro geometria.
- Aggiunta la riga di log con tensori fusi, VRAM in più e tempo.
- Documentazione spostata in `docs/configuration/lora.md` (limite di lunghezza del progetto).
- La lista di controlli P1-P7 citata in un commit è stata scritta per davvero in `tests/lora_gpu_checks.md`.
- Un errore generico (tensore non quantizzato nel pacchetto) è diventato un messaggio chiaro.

### 6. Tensori fuori dalle famiglie fuse (fatto)

Un adapter che tocca il router o altro: di default si fonde comunque la parte che il motore sa fare, con un avviso.

**Fatto e problemi risolti**

- Decisione dell'utente: interruttore acceso per default, niente rifiuto secco di serie.
- Un avviso nel log dice quanti tensori non si sanno trattare e quanta parte dell'aggiornamento rappresentano.
- `--no-lora-partial` torna al rifiuto secco per chi lo vuole.
- Aggiunto `lora_skipped` alla riga INFO che il server legge.
- Dal 23 novembre gli esperti MoE non sono più «fuori»: sono fusi (vedi la fase 10).

### 7. Prove sul modello vero con la GPU (E3) (fatto)

Fatte il 22 novembre. Tutte le prove passano; il merge è stato approvato.

**Fatto e problemi risolti**

- Tre errori reali trovati da tre riletture di basalt-3d, tutti nello script di prova, nessuno nel motore: copertura di 10 tensori su 300; scala del delta applicata a A e B insieme (adapter diecimila volte più piccolo del previsto); confronto contro «nessun adapter» invece che contro l'adapter nullo.
- Senza `--lora`: uguale. Trovato che `--spec 7` non è deterministico da solo: i confronti byte-esatti usano `--spec 2`.
- Adapter nullo sull'unico tensore Q8_0 reale: uguale, `+0.0 MiB`. Coppia +1/-1: uguale all'adapter nullo.
- Adapter vero, due avvii: identici tra loro, diversi dall'adapter nullo, testo sensato.
- Quattro prompt reali con copertura intera: tre identici, uno diverso ma coerente.
- Costo misurato: +1328,9 MiB di VRAM, cache esperti −392 posti, fusione 15-18 s per 300 tensori, nessuna differenza di velocità a `--spec 2`.
- Tipi misti in un livello: nessun arresto. Qualità (P5) ritirata per scelta dell'utente.

**Problemi futuri previsti e come pensiamo di risolverli**

- `--spec 7` potrebbe non essere deterministico sul motore in generale: fatto a parte, non verificato sul binario senza le modifiche LoRA.

### 8. Unione in master (fatto)

Il lavoro LoRA è nel ramo principale e il motore dell'albero principale è ricompilato.

**Fatto e problemi risolti**

- Unito il 22 novembre con l'approvazione dell'utente (merge `540b3004`).
- Un solo conflitto, nella coda delle prove diurne, risolto a mano.
- Motore dell'albero principale ricompilato a server fermo: `bin/basalt.exe` ha `--lora`.

**Decisioni e punti aperti**

- Da comunicare a basalt-3d: l'hash del merge, per la specifica della fase 11.

### 9. Due adapter veri, scaricati (fatto)

Provati due adapter pubblici. Uno ha fatto emergere un difetto, corretto.

**Fatto e problemi risolti**

- Adapter di huihui (2,3 MB): 96 tensori fusi, +420,0 MiB, 3,25 s.
- Adapter di loraforge (318 MB, con esperti): il motore si fermava su un tensore a tre dimensioni. Corretto guardando il nome prima della forma (commit `4e313ead`).
- Ora anche gli esperti di quell'adapter si possono fondere (fase 10).

**Decisioni e punti aperti**

- Questi adapter tolgono i rifiuti del modello: non sono lo scopo, servono solo a collaudare il caricamento.

**Problemi futuri previsti e come pensiamo di risolverli**

- Non è verificato che l'uscita sia quella voluta dagli autori: è verificato che si caricano e che l'uscita cambia.

### 10. Esperti MoE fusi all'avvio (E5) (fatto)

Il motore ora fonde anche i 512 esperti per livello: nuova copia della sezione esperti, stessi tipi e dimensioni, nessun costo di VRAM.

**Fatto e problemi risolti**

- Decisione dell'utente: addestrare anche gli esperti; la quantizzazione non è un problema; il limite dei 106 GiB di RAM non conta (RAM 127,7 GiB più file di paging fino a 100 GB, limite di commit 157 GiB).
- Come funziona: ogni riga toccata viene decompressa, riceve il delta e viene compressa di nuovo nello stesso tipo. Una riga con B a zero, un esperto non toccato e un livello non toccato sono copie byte per byte.
- Il motore non poteva scrivere i formati i-quant: ho portato da llama.cpp i quantizzatori IQ4_NL, IQ4_XS, IQ3_XXS, IQ3_S e IQ2_S. Sui byte coincidono con quelli di llama.cpp (file di parità con 6 tipi, compreso Q5_0).
- Il lettore degli adapter legge gli esperti impilati (come scrive il trainer) o uno per uno (come scrive `lora.py`).
- La copia fusa sta accanto all'adapter, con un nome che dipende da adapter, modello e scala, e si riusa al caricamento dopo; se l'adapter cambia, la vecchia si toglie.
- Test `lora_experts_test` verde: parità dei quantizzatori, lettura di un adapter scritto dal trainer, fusione a geometria vera (blob da 2.662.400 byte), rifiuti con messaggio, riuso della copia.
- Prova sul modello vero con adapter a esperti nulli su tutti i 48 livelli: uscita identica alla baseline; copia da 61 GiB scritta in 237 s.
- Prova con un adapter vero del trainer: 300 tensori densi e 12.370 blob di esperti fusi in 611 s (con la CPU occupata da altro); uscita sensata e diversa dalla baseline.

**Decisioni e punti aperti**

- Committato nel ramo `feat/lora-experts` (`bf661a09` quantizzatori, `988c2798` fusione) e unito in `master` (`1d53c470`).
- Tipi gestiti: IQ4_XS, IQ4_NL, IQ3_XXS, IQ3_S, IQ2_S, Q2_0, Q5_0 e i K/legacy; un livello in un altro tipo (per esempio IQ2_XXS) viene rifiutato per nome. Q2_0 non ha un riferimento di parità.
- Costo: 0,37 s per blob su un thread; circa 5-10 minuti per un adapter completo, una volta sola per adapter.

**Problemi futuri previsti e come pensiamo di risolverli**

- Ricomprimere a 4 bit può perdere parte di una correzione piccola: va misurato sull'adapter vero.
- La copia occupa quanto la sezione esperti (circa 61 GiB per il pack IQ4_XS): serve spazio accanto all'adapter.

### 11. Caricamento dinamico per sessione (E4) (non iniziato)

Poter cambiare o aggiungere l'adapter durante l'uso, non solo all'avvio. Solo un abbozzo di disegno.

**Decisioni e punti aperti**

- Oggi l'adapter si fonde solo all'avvio: per cambiarlo si riavvia il motore.
- Disegno di partenza: blocco di VRAM fisso in Q8_0 per le famiglie dense, chiave di richiesta che indica l'adapter, identità di conversazione estesa come per i vettori di controllo.
- La specifica la scrive basalt-3d.

**Problemi futuri previsti e come pensiamo di risolverli**

- 15-18 secondi di rifusione vanno bene per un adapter per sessione, non per un cambio a ogni prompt; con gli esperti sono minuti: serve un'idea diversa (fondere sulla GPU, o tenere pronti pochi adapter già fusi).

### 12. Collegamento a Loader e interfaccia (non iniziato)

Oggi LoRA si usa solo da riga di comando o dal file di configurazione. Nessun pulsante o campo nell'interfaccia.

**Decisioni e punti aperti**

- Non pianificato: si aspetta che la fase 11 sia stabile.

**Problemi futuri previsti e come pensiamo di risolverli**

- La stima di VRAM del Loader non sa che un tensore coperto da un adapter costa più byte (passa a Q8_0).
- Il Loader dovrà dire che serve spazio su disco per la copia degli esperti.

## Seconda traccia: addestrare una LoRA nostra

### 13. Scopo e via per addestrare (fatto)

Una LoRA che aggiunge informazioni al modello. Solo LoRA a 4 bit, niente fine-tuning pieno.

**Fatto e problemi risolti**

- Lo scopo è aggiungere informazioni, non togliere i rifiuti: serve un addestramento vero.
- Il modello ha 180 miliardi di parametri: in BF16 sono 336 GB, troppo per questa macchina.
- ms-swift ha una ricetta LoRA ma chiede 8 GPU da circa 75 GiB; `transformers` non supporta questa architettura.
- Scelta: QVAC Fabric, un fork di llama.cpp che addestra LoRA direttamente su un GGUF.

**Decisioni e punti aperti**

- Resta aperto l'ordine dei lavori: studiare prima come allungare le risposte (idea sotto), oppure prima la LoRA degli strumenti.

### 14. Prova su un modello finto (fatto)

L'addestramento arriva in fondo e impara, su un modello piccolo con la nostra architettura. Tre correzioni al loro codice.

**Fatto e problemi risolti**

- Modello finto: 4 livelli (3 GDN e 1 ad attenzione), 4 esperti, pesi casuali.
- Cause trovate e corrette: le hyper-connection «fuse» non hanno il passo all'indietro; nei livelli QSA K e V non ricevevano gradiente; al `CLAMP` mancava il passo all'indietro.
- Esito: perdita da 5,60 a 3,37 in 8 epoche; perplessità in inferenza da 281 a 23,4.
- Il 23 novembre verificato anche con pesi quantizzati (Q8_0, Q4_K_M, IQ4_XS, Q4_0) e con tutti i moduli, GDN, esperti condivisi ed esperti instradati compresi: i pesi LoRA si addestrano ovunque.
- Patch e script in `tools/lora_training/` (non ancora in un commit).

**Problemi futuri previsti e come pensiamo di risolverli**

- I gradienti non sono stati controllati numericamente: la prova è che la perdita scende e che l'adapter funziona.

### 15. Quali moduli addestrare (fatto)

Deciso: tutto ciò che il motore fonde, esperti instradati compresi. Rango 8.

**Fatto e problemi risolti**

- Attenzione (`attn_q/k/v/o`, 12 livelli), moduli GDN (`attn_qkv`, `attn_gate`, `ssm_out`, 36 livelli), esperti condivisi (`ffn_*_shexp`, 48 livelli) ed esperti instradati (512 per livello, 48 livelli).
- Il fork QVAC non addestrava GDN ed esperti condivisi: aggiunti sei target (`tools/lora_training/qvac_fabric_lora_targets.patch`). Gli esperti li addestrava già, con un LoRA per esperto.
- Adapter completo con rango 8: circa 7,26 GB in F32, di cui quasi tutto negli esperti.

**Decisioni e punti aperti**

- In addestramento servono anche gradienti e stati dell'ottimizzatore: circa 30 GB in più per gli esperti, oltre al modello mappato.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il consiglio incollato da Gemini di non addestrare gli esperti contraddice la tua scelta: tenuta la tua.

### 16. Il modello vero: NAS, disco locale, tipi (fatto)

GGUF Unsloth UD-IQ4_XS (87,2 GiB, 4 pezzi) sul NAS; scaricato su D: con SHA-256 e copiato su E: (stesse dimensioni).

**Fatto e problemi risolti**

- Il Q4_K_XL (103,7 GiB) non entrava nella RAM libera: cancellato dal NAS su richiesta.
- Scaricato UD-IQ4_XS: i quattro pezzi verificati con SHA-256.
- Letto direttamente dal NAS non conviene: l'addestramento carica tutto il modello; il disco E: (NVMe) è molto più veloce del disco D: (meccanico). Montare il NAS come volume è possibile ma richiede la password: la digiti tu.
- Il modello non è uniforme: esperti gate/up in IQ2_S, IQ3_XXS e IQ4_XS; down in IQ4_NL, Q2_0 e Q5_0.

**Decisioni e punti aperti**

- Per provare l'adapter sugli stessi pesi serve un pack ottenuto da questo GGUF (`convert.py --sections-from` il pack Uncensored IQ4_XS).

### 17. Addestramento sul modello vero (in corso)

Prova su CPU riuscita; l'addestramento completo sui fatti di Valdoria gira dalle 23:10.

**Fatto e problemi risolti**

- Prova di 4 passi: circa 3,5 minuti, perdita 2,1-2,3 (finita e sensata), adapter da 7,26 GB scritto.
- Comando: rango 8, alpha 16, 4 epoche, learning rate 3e-4 con coseno, contesto 128, 24 thread, moduli attenzione, GDN, esperti condivisi ed esperti instradati.
- Dati: 24 fatti inventati su una città che non esiste (Valdoria), ognuno in tre formulazioni, due volte.

**Decisioni e punti aperti**

- Durata stimata: circa 65 secondi per passo; con circa 40 passi per epoca fanno circa 3 ore.
- Se la perdita non scende o i fatti non si imparano: alzare il learning rate o le epoche, o ridurre a rango 4.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il backward sul modello vero è provato solo su CPU; su GPU non è provato.
- Qualità dell'adapter: dire se il modello risponde ai fatti nuovi senza perdere il resto.

### 18. Prova di inferenza con la LoRA (non iniziato)

Domande sui fatti di Valdoria, con e senza adapter, su un pack con gli stessi pesi.

**Decisioni e punti aperti**

- Convertire il GGUF in un `.basalt` (circa 100 GB su E:), poi eseguire con `--lora`: la copia degli esperti (61 GiB) si scrive accanto all'adapter, su un disco con spazio.
- Confronto: stesse domande con e senza adapter; le domande tenute fuori dall'addestramento dicono se ha imparato o solo memorizzato.

**Problemi futuri previsti e come pensiamo di risolverli**

- Se l'adapter funziona solo sul pack dell'addestramento e non sul pack Uncensored già in uso, si vede dal confronto.

### 19. LoRA per l'harness (non iniziato)

Una LoRA pensata per come TaDaMoo usa il modello: i suoi strumenti e le sue convenzioni.

**Fatto e problemi risolti**

- Decisioni già prese: dati Pi + OpenCode; insegnare all'adapter gli strumenti propri del progetto.

**Decisioni e punti aperti**

- Dipende dall'esito della fase 17.

**Problemi futuri previsti e come pensiamo di risolverli**

- Idea solo annotata: trasferire una LoRA addestrata su un'architettura a un'altra.

## Altri task fatti in questi giorni

### 20. Android: messaggi mentre l'harness lavora (fatto)

Si scrive anche mentre l'agente lavora: guida, in coda, urgente, con «Fallo subito» e «Togli».

**Fatto e problemi risolti**

- Il server decide da solo se il messaggio guida il compito, aspetta o è urgente; l'app mostra la scelta in una nota.
- Una richiesta in coda ha i pulsanti «Fallo subito» e «Togli» (con conferma, non si annulla); i pulsanti spariscono quando parte.
- Il composer ha «Ferma» e «Invia» insieme mentre l'agente lavora; vale per Chat e Agentic coding (stessa schermata).
- Test nuovi di eventi, di invio da occupato e di larghezza dei pulsanti: tutti i test dell'app passano.

**Decisioni e punti aperti**

- Committato nel ramo `feat/mobile-parity` (`fc8483ad`) e unito in `master` (`1d53c470`). APK compilato il 23 novembre (165 MiB), scaricabile via Tailscale; non provato su un telefono vero.

### 21. Android: «Immagini e grafici» (fatto)

Scelta propria sul telefono, due interruttori con livello, per la nuova conversazione e per quella aperta.

**Fatto e problemi risolti**

- Era solo nella memoria del browser: l'app non poteva né chiederla né mandarla.
- Ora è nella schermata di nuova conversazione e nelle opzioni di una conversazione aperta; la scelta si ricorda sul telefono.
- Sei livelli per interruttore, come sul web; testi in italiano e inglese; controlli da almeno 48 dp.

**Decisioni e punti aperti**

- Committato (`fc8483ad`) e unito in `master` (`1d53c470`).

### 22. Android: risposta lunga (fatto)

Scritta da zero in Flutter: modalità scrivi, traduci, riscrivi; il messaggio finisce con la riga che l'harness legge.

**Fatto e problemi risolti**

- Nel menu «+» c'è «Risposta lunga»; quando è attiva compare un chip sopra il campo.
- Una volta sola o per tutta la conversazione; lunghezza fissa, giudicata dall'agente o approssimativa.
- L'avanzamento («scrivo la parte 2 di 4») appare come una riga che si aggiorna, e il testo corretto sostituisce quello mostrato.
- Il test ha trovato che i pulsanti del composer erano 40 dp e non 48: corretto.

**Decisioni e punti aperti**

- Committato (`fc8483ad`) e unito in `master` (`1d53c470`). Il glossario e il «conserva link e codice» esistono solo sul web.

### 23. Providers: richieste esterne OpenAI e Anthropic (fatto)

Una riga propria sotto la tabella delle funzioni: risponde solo il modello locale caricato.

**Fatto e problemi risolti**

- Verificato nel codice: un programma esterno vede sempre il motore locale, mai il provider primario; senza modello riceve 503.
- Testi in italiano e inglese; test statico dell'ordine e delle lingue.

**Decisioni e punti aperti**

- Committato (`97370132`), controllato in un browser vero a 360, 390 e 1280 px (`234bbbed`) e unito in `master` (`1d53c470`).

### 24. Dark web: controllo e Tor (fatto)

Funziona, ma Tor non era installato nella copia di sviluppo: per questo rispondeva solo il web normale.

**Fatto e problemi risolti**

- Il server diceva `tor.installed: false`: lo strumento dava errore al modello, che rispondeva dal web.
- Il pacchetto di installazione include Tor in `addons\tor`; la cartella di sviluppo era nata prima di quel passo. Copiato il bundle già verificato, senza riavviare il server.
- Prova con un Tor separato: SecureDrop, ProPublica, BBC News e Debian danno 20 risultati su 20 su indirizzi .onion, da motori onion (Onionway, Tor66, The Deep Searches, TorNet, Ahmia, Amnesia).
- Lettura di ProPublica via onion: 13.052 caratteri, incapsulati come dati. Molti risultati sono cloni e truffe: i siti autentici si riconoscono dall'indirizzo.

**Decisioni e punti aperti**

- Gli strumenti dark web esistono solo nelle conversazioni iniziate con l'opzione accesa: aprirne una nuova.

### 25. Unione degli update, plugin autocontenuto, voce nell'harness (fatto)

Tre rami, uniti in master il 22 novembre senza riavviare TaDaMoo.

**Fatto e problemi risolti**

- Un update non perde le modifiche locali: unione dentro i file, con tre versioni mostrate e descritte a parole; skill, plugin, MCP e connettori.
- Un plugin porta con sé il suo programma, caricato solo quando serve.
- L'harness conosce otto strumenti vocali e la lingua della voce; `/brain/say` accetta la sola lingua.

**Decisioni e punti aperti**

- Il salvataggio delle istruzioni di unione va tolto (task sotto).

### 26. Videonews separato e pulizia dei worktree (fatto)

Il telegiornale è un progetto a sé in E:/Projects/skills/Videonews; 27 worktree vecchi cancellati.

**Fatto e problemi risolti**

- Il telegiornale non si distribuisce con TaDaMoo su GitHub.
- Cancellati i worktree vecchi (rami tenuti, nessun commit perso); il worktree del diario di TaDaMoo cancellato su ordine dell'utente.

**Decisioni e punti aperti**

- Resta da installare il plugin da Videonews e togliere i vecchi file (dopo il riavvio).

### 27. Togliere il salvataggio delle istruzioni di unione (fatto)

Deciso: ogni update è indipendente, le istruzioni non si salvano. Fatto, nel ramo feat/no-saved-merge-notes.

**Fatto e problemi risolti**

- Tolti dal codice la casella «ricorda» e il salvataggio, senza alias né tracce: importer, skill manager, connettori, finestra web dell'unione (le istruzioni si accumulano solo dentro la finestra), test, documentazione.
- Commit `538e563a`; 53 test e la prova in browser vero passano.

**Decisioni e punti aperti**

- Unito in `master` (`1d53c470`).

### 28. Verifica grammaticale nella Chat, per lingua (fatto)

Menu a tendina per la lingua su cui caricare il modello specializzato; offerta automatica nelle traduzioni. Fatto, unito in master.

**Fatto e problemi risolti**

- Elenco lingua → modello in Impostazioni > Inferenza > Verifica della lingua: ogni voce è un provider di Providers che gira su questo PC o sulla sua rete (mai un cloud); casella per spegnere l'offerta.
- In Chat, sotto ogni risposta finita, il pulsante «Controlla la grammatica» con il menu delle lingue e il testo corretto da copiare; la risposta non cambia.
- Quando la richiesta è una traduzione in una lingua dell'elenco, il pannello si apre da solo sulla risposta nuova e offre il controllo (non sulle risposte rilette dalla storia).
- Il codice, le formule, i link, i percorsi e il resto che non deve cambiare sono messi da parte prima della chiamata e rimessi dopo; il modello non li vede mai.
- Il programma controlla ogni risposta (non vuota, non ripetuta, lunga come il pezzo, tutti i segnaposto tornati); se no, una seconda richiesta, poi il pezzo resta com'era e il pannello lo dice.
- Test: 14 unitari, 4 sulle rotte con un server di modello finto, prova in browser vero a tre larghezze con una conversazione reale. Commit `45ca7a13`.

**Decisioni e punti aperti**

- Unito in `master` (`1d53c470`).
- Non provato con un vero modello ungherese (OpenEuroLLM-Hungarian via Ollama): serve aggiungerlo come provider locale.
- Solo sul web: l'app Android non ha ancora il controllo.

**Problemi futuri previsti e come pensiamo di risolverli**

- Ollama non è un tipo di provider: oggi si usa come server compatibile OpenAI (llama.cpp, LM Studio, vLLM o altro).

### 29. Voce in Chat e in vibe coding (fatto)

Parlare senza scrivere: la chat ascolta, esegue e legge la risposta. Fatto sul web, unito in master.

**Fatto e problemi risolti**

- Barra della voce sopra il campo messaggio della Chat e di Agentic coding: «Parla», «Leggi le risposte ad alta voce», «Conversazione vocale», lingua.
- «Parla»: un tocco registra, si ferma da sola dopo una pausa di circa 1,4 s; il WAV a 16 kHz si fa nella pagina e va al servizio voce (`POST /transcribe`); il testo va nel campo e parte.
- Lettura: la risposta finita, senza codice, formule, tabelle e indirizzi, si dice a parti con la voce del PC (`POST /tts/test`); codice e formule si nominano («codice sullo schermo»).
- Conversazione vocale: ascolta, invia, aspetta la risposta, la dice, ascolta di nuovo; finisce con «Ferma la voce», se il microfono fallisce o dopo due silenzi.
- Test: 9 sulle funzioni pure in node e una prova in browser vero con microfono finto a tre larghezze (18 controlli: registrazione, invio, lettura, errori, conversazione, silenzio, italiano). Commit `c869258e`.

**Decisioni e punti aperti**

- Unito in `master` (`1d53c470`).
- Non provato con la voce e il microfono veri: il microfono del browser serve su localhost o https.
- Solo sul web: l'app Android ha la sua voce e non è stata toccata.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il riconoscimento riceve WAV, FLAC e OGG, non i formati che il browser registra da solo: per questo il WAV si fa nella pagina.

### 30. Riassunti a più livelli (fatto)

Quattro livelli di riassunto tramite la sessione tecnica, con un pannello in Impostazioni > Inferenza. Fatto, unito in master.

**Fatto e problemi risolti**

- Livelli: integrale (toglie solo l'inutile, circa 85 %), compressione media (circa 50 %), alta compressione (circa 20 %), executive e note chiave (una frase e al massimo 7 punti).
- Integrale e medio si scrivono parte per parte e si uniscono, quindi il testo può essere molto più grande della sessione; alta e executive vengono da note delle parti, a giri.
- Codice, comandi, percorsi, numeri e nomi si copiano come sono. Il programma (non un modello) controlla ogni chiamata e richiede la parte se è vuota, ripetuta o troppo lunga; una parte che non riesce resta com'era.
- `POST /api/summarize`, un nuovo uso `summary_levels` della sessione tecnica (interruttore e dimensione della parte) e il pannello «Riassumi un testo»: testo incollato o file fino a 8 MB, livello, lingua, copia e salva come .md.
- Test: 9 unitari con un modello finto, 2 sulla rotta, prova in browser vero a tre larghezze. Commit `aa62cfb9`.

**Decisioni e punti aperti**

- Unito in `master` (`1d53c470`).
- Non provato con il modello vero: la sessione tecnica locale o un provider scelto.
- L'utente aveva detto che le impostazioni di inferenza non sono il posto più corretto: collocazione da rivedere.

### 31. MarkItDown nell'harness e esportazione del Markdown (fatto)

File convertiti in Markdown dal programma e non dal modello; a fine lavoro si sceglie cosa scaricare.

**Fatto e problemi risolti**

- Ricerca fatta: `microsoft/markitdown` e il server MCP `trsdn/markitdown-mcp` convertono oltre 29 formati. Da Markdown a Word e PDF: nessuna dipendenza nuova, bastano le funzioni già nostre (docx, odt, html) e il browser installato per il PDF; Pandoc per epub e rtf.
- Lettura: MarkItDown installato e agganciato agli allegati (Word, PowerPoint, pagine web, e-book; PDF se manca pypdf) e strumento `convert_to_markdown` per l'agente; se manca o il file è rovinato, restano i lettori di prima.
- Salvataggio: finestra con le tre scelte (solo Markdown, solo convertito con menu dei formati, tutti e due in uno zip) sul web e nell'app Android; docx, odt e html risultano identici byte per byte, il PDF no perché porta l'ora di stampa.
- Test: 9 + 5 di Python, 7 dell'app, prova in browser vero a tre larghezze (15 controlli). Committato nel ramo `feat/mobile-parity` (`d2a9cd44`).

**Decisioni e punti aperti**

- Unito in `master` (`1d53c470`). Il PDF e l'OCR su GPU restano un lavoro a parte (nessun modello è collegato oggi).

### 37. Completare il registro dei task dell'harness (fatto)

Il registro dell'harness è ora anche la mappa dello sviluppo: gruppi, cronologia, note, vista a spina e un solo file da cui nascono scheda, testo e pagina.

**Fatto e problemi risolti**

- Confronto fatto: il registro è il lavoro in corso, l'artefatto la mappa dello sviluppo; ora sono lo stesso dato.
- 1. **Cronologia**: chiudere un task lo porta nella cronologia con data e note; si può riaprire. Eliminare è un atto separato e toglie per sempre. Quello che riferisce un programma (esecuzioni dell'agente, obiettivi, parti di una richiesta grande, righe di un piano) si cancella come prima.
- 2. **Gruppi** (filoni) con il proprio avanzamento: quanti task aperti e quanti fatti. Eliminare un gruppo lascia i suoi task.
- 3. **Stati nuovi**: «fatto, da committare» e «decisione aperta». Gli strumenti dell'agente restano a quattro stati.
- 4. **Tre note** per task: fatto e problemi risolti, decisioni e punti aperti, problemi previsti.
- 5. **Vista a spina** con il pannello delle note, accanto alla vista ad albero e alla cronologia.
- 6. **Un solo dato**: `docs/roadmap.json` si scrive dal registro (e si rilegge); da lì nascono `docs/roadmap.md` e la pagina dell'artefatto (`tools/roadmap/page_template.html`).
- Nascondere ed eliminare i nodi come azioni separate; casella «Visualizza nodi nascosti» con il conteggio; il pulsante «Task» nella Chat mostra solo i task della conversazione aperta.
- Test: 10 sul registro, 10 sulla mappa, 5 sulle rotte, e due prove in browser vero a tre larghezze (la scheda dei task e la mappa, 23 controlli); la pagina generata si disegna con tutti i nodi.

**Decisioni e punti aperti**

- Da unire a master e da riavviare TaDaMoo: il processo in corso ha ancora il codice vecchio e, se scrivesse il file dei task, perderebbe i campi nuovi. Dopo il riavvio si legge `docs/roadmap.json` nel registro dal pulsante «Leggi docs/roadmap.json».
- Il punto 7 (collegamenti a commit e rami, vista per telefono) non è stato accettato e non è fatto.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il diario `docs/task_diary.md` resta scritto a mano (è il racconto dei giorni): da lì in poi le liste dei task si leggono in `docs/roadmap.md`.

## Altri task da fare, senza riavviare TaDaMoo

### 32. Risposte più lunghe (decisione aperta)

Il modello chiude le risposte da solo, anche con limiti alti. Prima prove senza addestramento.

**Fatto e problemi risolti**

- Valutato il testo di Gemini: diagnosi plausibile, numeri non misurati; l'ID del token di fine è sbagliato per questo modello (qui 248044/248046).

**Decisioni e punti aperti**

- Prima: `end_turn_bias` negativo (già nel pannello Chat) con un limite alto e le continuazioni automatiche.
- Poi: una rampa sul token di fine nel campionatore, senza addestramento.
- Solo se degrada la qualità: SFT su risposte lunghe generate dal modello stesso.

**Problemi futuri previsti e come pensiamo di risolverli**

- Addestrare su sequenze da 16.000-40.000 token non è realistico su questa CPU; il trainer non ha il DPO.

### 33. Controllo del telefono via adb e caricamento automatico del modello per la voce (non iniziato)

Due idee ancora da progettare.

**Decisioni e punti aperti**

- adb: tocca, leggi lo schermo, scorri, senza installare nulla; canale separato dalla app Flutter, più fragile se cambia l'interfaccia di un'app.
- Voce: oggi non esiste un caricamento automatico del modello quando una funzione vocale ne ha bisogno; costa tempo e VRAM, da decidere insieme.

## Dopo il riavvio di TaDaMoo e prove tue

### 34. Dopo il riavvio di TaDaMoo (lo fai tu) (in attesa)

Cose già pronte che aspettano il riavvio.

**Decisioni e punti aperti**

- Riavviare il servizio voce per il nuovo `/brain/say`.
- Installare Videonews da `E:/Projects/skills/Videonews` e togliere i vecchi file del telegiornale da `E:/Projects/basalt`.
- Provare i nuovi pulsanti Admin (riavvia il server, riavvia il servizio voce).
- Ricompilare il motore dell'albero principale se è in uso.

### 35. Prove tue sul PC (in attesa)

Servono GPU libera, finestre vere, un telefono vero.

**Decisioni e punti aperti**

- **MiniMax H3 (video):** si prova da LTX Studio; la GPU ora è libera (nessun modello caricato nel server).
- La coda `tests/QUEUE_daytime.md`: voce e avatar dal vivo, app Android su un telefono vero, app desktop, controllo del computer e del browser con clic veri, piani e telegiornale veri.
- La prova SWE-bench (smoke, poi dev). Su richiesta preparo la pagina di revisione, un test per riga.

### 36. Decisioni e promemoria (in attesa)

Cose che spettano a te o che ti ricordo.

**Decisioni e punti aperti**

- Riordino del disco E:: lo fai dalla sessione LTX Studio, non da qui.
- Piano contro le istruzioni nascoste nelle pagine web: te lo ricordo intorno al 26-27 novembre.
- OCR e PDF: confermato su GPU, non su CPU.
