# LoRA in Basalt-Plus

Gli adapter LoRA nel motore sono fatti, esperti compresi. L'addestramento vero è in corso. In fondo ci sono anche gli altri task del progetto, fatti e da fare. Tocca una voce per le note.

_Aggiornato il 25/11/2026. Generato da `docs/roadmap.json`: non si modifica a mano._

| Stato | Nodi |
|---|---|
| fatto | 33 |
| in corso | 9 |
| in attesa | 12 |
| non iniziato | 1 |

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

### 11. Caricamento dinamico per sessione (E4) (in attesa)

Una LoRA fusa all'avvio più una seconda, dinamica, caricata durante l'uso in tempo reale. Deciso dall'utente il 23 novembre; da fare in seguito.

**Decisioni e punti aperti**

- Deciso dall'utente (23 novembre): una LoRA attiva fusa all'inizio, più una dinamica caricata in tempo reale. La dinamica si fa dopo la LoRA da Knowledge.
- Oggi l'adapter si fonde solo all'avvio: per cambiarlo si riavvia il motore.
- Disegno di partenza: blocco di VRAM fisso in Q8_0 per le famiglie dense, chiave di richiesta che indica l'adapter, identità di conversazione estesa come per i vettori di controllo.
- La specifica la scrive basalt-3d.
- Rimandato al 30 novembre insieme a tutto il filone LoRA (decisione dell'utente, 23 novembre).

**Problemi futuri previsti e come pensiamo di risolverli**

- 15-18 secondi di rifusione vanno bene per un adapter per sessione, non per un cambio a ogni prompt; con gli esperti sono minuti: serve un'idea diversa (fondere sulla GPU, o tenere pronti pochi adapter già fusi).

### 12. Collegamento a Loader e interfaccia (in attesa)

Oggi LoRA si usa solo da riga di comando o dal file di configurazione. Nessun pulsante o campo nell'interfaccia.

**Decisioni e punti aperti**

- Non pianificato: si aspetta che la fase 11 sia stabile.
- Rimandato al 30 novembre insieme a tutto il filone LoRA (decisione dell'utente, 23 novembre).

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

### 17. Addestramento sul modello vero (fatto)

Addestramento completo sui fatti di Valdoria finito (23 novembre, 5 ore e 33 minuti): perdita da 2,4 a 0,09, adapter da 7,26 GB.

**Fatto e problemi risolti**

- Finito alle 04:44 del 23 novembre, 4 epoche, 66 passi per epoca. Perdita di addestramento 0,205 / 0,142 / 0,086 a fine epoche 1-3; sui dati tenuti da parte 0,25 / 0,23 / 0,22 con precisione 94%.
- Adapter scritto: scratchpad/lt/valdoria_adapter.gguf (7,26 GB). La prova di inferenza è il passo successivo.
- Prova di 4 passi: circa 3,5 minuti, perdita 2,1-2,3 (finita e sensata), adapter da 7,26 GB scritto.
- Comando: rango 8, alpha 16, 4 epoche, learning rate 3e-4 con coseno, contesto 128, 24 thread, moduli attenzione, GDN, esperti condivisi ed esperti instradati.
- Dati: 24 fatti inventati su una città che non esiste (Valdoria), ognuno in tre formulazioni, due volte.

**Decisioni e punti aperti**

- Durata reale: circa 75 secondi per passo, 5 ore e 33 minuti in tutto (la stima era 3 ore).
- Se la perdita non scende o i fatti non si imparano: alzare il learning rate o le epoche, o ridurre a rango 4.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il backward sul modello vero è provato solo su CPU; su GPU non è provato.
- Qualità dell'adapter: dire se il modello risponde ai fatti nuovi senza perdere il resto.

### 18. Prova di inferenza con la LoRA (fatto)

Prova fatta il 23 novembre: l'adapter ha un effetto, ma i fatti arrivano solo in parte (2 risposte su 16 giuste, contro 0 senza adapter); nessuna perdita sulle domande generali.

**Fatto e problemi risolti**

- Pack ricavato dal GGUF di addestramento (`--compat-bf16`, 90,9 GiB, 57 minuti) su D:; 22 domande a temperatura 0: 6 con le parole viste, 10 riformulate, 2 su città inventate non nei dati (Marenza, Rovenna), 4 di controllo.
- Senza adapter: 0 su 16 sui fatti di Valdoria (inventa luoghi e persone reali), 4 su 4 sui controlli.
- Con l'adapter fuso solo sulle matrici densi (il motore di prima, che non fondeva gli esperti): 0 su 16, cambia lo stile («Valdoria è una città immaginaria»), controlli 4 su 4.
- Con l'adapter completo, esperti compresi (motore di `master`, copia fusa di 56 GiB scritta in 2968 secondi su D:): 2 su 16. Giuste: l'azienda Brenzone & Figli (orologi a pendolo, madreperla) e il Ponte dei Sospiri Verdi (1452). Vicine ma sbagliate: monte Cirdano 2.412 m (vero 2.314), festa dei Fanali in agosto. Controlli 4 su 4: non ha perso nulla.
- Effetto collaterale: ora inventa risposte nel formato di Valdoria anche per città che non esistono (A.C. Marenza «in maglia granata», Rovenna «fondata nel 1243»).

**Decisioni e punti aperti**

- Per capire se manca l'apprendimento o si perde nella fusione (requantizzazione a 4 bit degli esperti): provare lo stesso adapter non fuso sul fork di addestramento (CPU, lento), oppure fondere con scala 2 (altri 50 minuti).
- Se il problema è la fusione a 4 bit: la LoRA sugli esperti in tempo reale, senza fondere (task «Caricamento dinamico per sessione»).
- Se il problema è l'addestramento (24 fatti, 4 epoche, rango 8): riaddestrare con più epoche, rango più alto o più formulazioni, quando l'addestramento su GPU sarà veloce.
- Ipotesi dell'utente: il modello pensa in inglese e poi traduce; l'addestramento va fatto anche in inglese (e in più forme). In verifica: stesso adapter interrogato in italiano (forma semplice), in inglese (chat e semplice) e in italiano con il ragionamento acceso.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il motore del binario `bin/basalt.exe` dell'albero principale (22 novembre) non fonde gli esperti: va ricompilato (Basalt si può ricompilare). La prova è stata fatta con un albero su D: (`D:\Projects\basalt-lora-test`).

### 19. LoRA per l'harness (in attesa)

Una LoRA pensata per come TaDaMoo usa il modello: i suoi strumenti e le sue convenzioni.

**Fatto e problemi risolti**

- Decisioni già prese: dati Pi + OpenCode; insegnare all'adapter gli strumenti propri del progetto.

**Decisioni e punti aperti**

- Dipende dall'esito della fase 17.
- Rimandato al 30 novembre insieme a tutto il filone LoRA (decisione dell'utente, 23 novembre).

**Problemi futuri previsti e come pensiamo di risolverli**

- Idea solo annotata: trasferire una LoRA addestrata su un'architettura a un'altra.

### 38. Creare una LoRA dalla scheda Knowledge (in attesa)

Una sotto-scheda LoRA in Knowledge: documenti e testo, materiale preparato dal modello, un pulsante addestra, verifica finale e attiva. Progetto approvato, codice da scrivere.

**Fatto e problemi risolti**

- Progetto scritto in `docs/design-lora-knowledge.md`: cosa vede la persona, come si prepara il materiale (riuso di archivi e domande già scritte), costo, memoria, dove stanno i file, quattro fasi.
- Decisioni dell'utente del 23 novembre: GGUF di base su D: (mai su E:), il modello locale può tacere per ore, nessun limite di tempo, una LoRA fusa all'avvio (più una dinamica in seguito), verifica della qualità alla fine.

**Decisioni e punti aperti**

- Fase 0: finire la prova su Basalt (task 18) e portare l'addestramento su GPU (task 40): cambia il tetto e la stima.
- Verifica finale: perplessità con e senza adapter su un testo tenuto da parte e su un controllo generale.
- Tutti i file di questa funzione su D:. E: ospita il file di paging (fino a 100 GB): se si riempie il computer si blocca.
- Idea dell'utente (23 novembre): il materiale va scritto in inglese e nella lingua del documento, in più forme (semplice, domanda e risposta, chat): il modello ragiona in inglese e un legame imparato solo con parole italiane non viene trovato da una ricerca con parole inglesi.
- Rimandato al 30 novembre insieme a tutto il filone LoRA (decisione dell'utente, 23 novembre).

**Problemi futuri previsti e come pensiamo di risolverli**

- Oggi l'addestramento è solo su CPU: circa 76 secondi per passo, 5 ore e 33 minuti per 24 fatti.
- Una LoRA non sostituisce l'archivio per i fatti: non si aggiorna e può inventare cose vicine.
- L'addestratore (fork QVAC patchato) va portato in una ricetta di compilazione e se ne controlla la licenza.

### 40. Addestramento LoRA su GPU (più veloce) (in attesa)

Misurato sul modello vero (23 novembre): con il fork CUDA un passo a contesto 128 passa da 76 a 24-35 secondi (2,2-3,2 volte più veloce), ma il costo cresce con i token: la CPU degli esperti è il limite. Per guadagnare molto servono gli esperti sulla GPU.

**Fatto e problemi risolti**

- Letto il codice del fork QVAC: ha già i passi all'indietro su CUDA per esperti (`MUL_MAT_ID_BACK`), GDN, SSM-conv, attenzione, e `out_prod` con dequantizzazione dei pesi quantizzati; ha l'opzione `-pw` per trasferire i pesi mentre calcola quando il modello non entra in VRAM.
- Fork compilato con CUDA (cartella `build-cuda` nella cartella temporanea, 261 secondi, scheda sm_120). Provato sul modello finto con la dimensione di testa GDN supportata dalla GPU (32; con 8 il kernel GDN su CUDA si ferma: il modello vero ne ha una supportata, da confermare): CPU e GPU danno la stessa perdita passo per passo (5,669 contro 5,670 al primo passo, 5,226 contro 5,229 al 77°), poi si allontanano piano (perdita sui dati di controllo 4,47 contro 4,57 dopo 2 epoche con learning rate 0,01). Passi all'indietro su GPU di esperti, GDN, attenzione: funzionano e vanno nella stessa direzione.
- Misure (dati in inglese, LoRA su attenzione, GDN ed esperti condivisi, senza esperti instradati; `-ngl 99 --no-op-offload`): esperti tutti su CPU: contesto 128 = 35 s/passo, 256 = 61 s, 384 = 92 s (circa 4 token al secondo, l'epoca dura circa 30 minuti in tutti e tre i casi); esperti degli ultimi 12 strati su GPU (30 GB di VRAM) a contesto 128 = 24 s/passo (5,3 token al secondo). Riferimento: CPU sola 76 s/passo a 128 (1,7 token al secondo). Contesto 512 e 1024: fuori memoria (il fork riserva circa 49 MiB per token di attivazioni, senza gradient checkpointing).
- Verificato: l'ipotesi «il costo di un passo non dipende dai token» è FALSA finché gli esperti calcolano sulla CPU (il tempo è proporzionale ai token). Il guadagno grande resta quello da misurare con gli esperti a blocchi sulla GPU.
- Causa dei 68 GiB di prima: il trasferimento dei pesi per operazione (op-offload) tiene vive le copie dalla fase in avanti a quella all'indietro; con `--no-op-offload` non succede. La cache degli esperti del fork (`--moe-cache-mib`) non supporta l'addestramento.
- Addestramento in inglese su GPU finito (23 novembre): 1,88 ore, 4 epoche, solo moduli densi (attenzione, GDN, esperti condivisi, rango 8), perdita da 1,24 a 0,165, adapter da 61 MB che si fonde al caricamento in pochi secondi (nessuna copia fusa degli esperti). Su Basalt: 16/16 sulle domande in inglese, 8/16 in italiano (primo adapter con esperti: 2/16; base: 0/16). Danno verificato: controllo 1/4 (base 4/4), alle domande fuori tema risponde con Valdoria, le risposte non si chiudono e continuano con frasi dei dati. Confronto non pulito: sono cambiati insieme lingua, GPU, assenza degli esperti instradati e numero di epoche; la lingua come causa è solo plausibile, non provata. Prove che mancano: stesso addestramento in italiano (circa 1,9 ore) e stessa adapter con scala 0,5.
- Prove di correzione (23 novembre). Stessa adapter inglese a scala 0,5: fatti 0/16 in italiano e 0/16 in inglese, controllo 4/4: con quell'adapter non c'è una scala intermedia (provati solo 0,5 e 1,0). Riaddestrata in formato chat (96 conversazioni: 72 su Valdoria in tre formulazioni, 24 di cultura generale; perdita solo sulle risposte; 3 epoche, learning rate 2e-4; 1,7 ore su GPU): il danno è sparito (controllo 4/4, le risposte si chiudono), ma i fatti calano: italiano 3/16 (prima 8/16), inglese 11/16 secondo il controllo automatico, che è largo (conta giusto un fatto anche con dettagli sbagliati). Restano parole italiane storpiate e «Marenza» che riceve ancora risposte su Valdoria. Domande inglesi molto meglio delle italiane con la stessa adapter: coerente con l'ipotesi dell'utente (fatti tenuti in inglese), ma non provata.

**Decisioni e punti aperti**

- Idea dell'utente: caricare in GPU una parte dei pesi per volta, addestrare tutti i pezzi su quella parte, salvare e unire. Valutata: i gradienti passano anche dai pesi congelati, quindi ogni parte richiederebbe comunque un passaggio completo del modello. La forma giusta è far scorrere i pesi strato per strato dentro ogni passo (il fork lo sa fare), con molti token per passo.
- Ordine: compilare il fork con CUDA, provarlo sul modello finto, poi sul modello vero con contesti 128, 512 e 1024 (dopo la prova su Basalt, che occupa la RAM).
- Precisazione dell'utente (23 novembre): caricare un blocco di strati alla volta, calcolare TUTTI i pezzi di testo su quel blocco e conservare i vettori di collegamento tra gli strati; stessa matematica dell'addestramento normale, ma ogni blocco si carica una volta sola per passaggio. Accettato: è l'addestramento con batch pieno, strato per strato. Resta che ogni aggiornamento della LoRA richiede un passaggio completo (i pesi cambiano), quindi il costo è passaggi × tempo di un passaggio.
- Due modi: (1) batch grande in un solo grafo (contesto lungo o più sequenze), senza codice nuovo se il trainer lo permette; (2) uno schedulatore strato per strato con le attivazioni conservate in RAM, che serve quando il materiale è troppo grande per la VRAM (corpus come la Cabala). Si misura prima (1).
- Per il metodo dell'utente servono tre cose nel fork: copie separate dei pesi per ogni passo del pianificatore, gradient checkpointing, e gli esperti a blocchi in GPU. Lavoro di ggml; da specificare se le misure lo giustificano.
- Sospeso dall'utente il 23 novembre: si riprende la settimana prossima (dal 30 novembre). Stato, scoperte e ordine delle prove sono nel diario del 23 novembre.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il backward su GPU del modello vero non è provato; alcuni passi potrebbero ricadere sulla CPU.
- Il modello è 87 GB e la GPU ha 32 GB: la velocità dipende dal trasferimento dei pesi.
- Il modello finto è troppo piccolo per misurare la velocità (la GPU ci mette di più della CPU per l'avvio). Il tempo vero si misura sul modello vero, che aspetta la fine della conversione per la prova su Basalt (RAM).
- Prima misura sul modello vero (contesto 128, pesi in RAM, calcolo su CUDA): si ferma subito con «out of memory»: il fork prova a riservare 72 GiB di buffer di calcolo sulla GPU (probabilmente le matrici degli esperti dequantizzate per il passo all'indietro). Da risolvere prima di avere un tempo: meno moduli addestrati, parte degli strati su GPU, o un buffer per strato.

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

### 41. Il modello non si preoccupa più del contesto (fatto)

L'harness ora dice al modello che il contesto si compatta da solo e che quello che resta non è significativo (web, desktop e Android: stessa strada).

**Fatto e problemi risolti**

- Verificato: l'app Android e il web usano le stesse sessioni (`/api/agent/sessions`) e lo stesso blocco fisso «che cosa sei»; tutte le sessioni dell'utente hanno la compattazione attiva. Il modello sapeva già che la conversazione si compatta, ma la frase diceva «hai circa N token di spazio» e lo strumento `self_status` invitava a controllare lo spazio.
- Cambiato: il blocco dice che l'harness tiene il contesto nella finestra da solo e che lo spazio rimasto non è significativo (non guardarlo, non citarlo alla persona, non accorciare o fermare il lavoro, non proporre una conversazione nuova); stessa frase nella nota `[harness]` e nella descrizione di `self_status`. Commit `55472483`, test aggiunto.

**Decisioni e punti aperti**

- Vale dal prossimo avvio di TaDaMoo (il processo in corso ha il testo vecchio); non l'ho riavviato.
- Se il modello remoto continua a parlarne, la causa è nel suo modo di rispondere: si guarda il testo delle sue frasi.

### 44. Models > Providers: scheda dei consumi di token (fatto)

Fatto il 24 novembre (ramo feat/token-usage): in Models c'è la scheda «Usage» (Consumi) con i token di ogni fornitore, cloud, locali e il motore locale, per fornitore, modello e funzione; filtri fissi (ultima ora, 12 ore, giorno, settimana, mese, totali) e un periodo con data e ora.

**Fatto e problemi risolti**

- Ogni chiamata è una riga in `llm_usage_log.jsonl` (ora, chi ha risposto, modello, funzione, token in ingresso, in uscita, letti dalla cache); dei messaggi non si conserva niente. Si scrive dove la chiamata finisce: la risposta di un fornitore (RoutedClient), il relay dello scambio pesante e il motore locale (Service.run).
- Provato: 9 test e una prova in un browser vero a 360, 390 e 1280 con tre chiamate a 30 minuti, 5 ore e 3 giorni fa: ogni periodo fisso conta quelle giuste (1, 2, 2, 3, 3, 3), un periodo con data e ora conta quella del terzo giorno, una fine prima dell'inizio è rifiutata.

**Decisioni e punti aperti**

- Non coperto: le chiamate che il servizio voce fa da solo a un fornitore (`voice/callbridge/llm_backend.py`) e quelle di Jev non passano da qui. Il costo in denaro non è mostrato. Quando si unisce la funzione dei programmi esterni (task 45) il suo client va creato con il registro dei consumi.

### 45. Models: il fornitore delle richieste esterne (OpenAI e Anthropic) si sceglie (fatto)

Fatto il 24 novembre nel ramo feat/external-provider (commit 0feb7851), NON unito a master: esce con la release dopo la 2.1, per decisione tua. «Altri programmi (interfacce OpenAI e Anthropic)» è una funzione dell'elenco dei fornitori. Senza scelta risponde il modello caricato, come prima; con un fornitore scelto, chat completions e messages (anche in streaming e con strumenti) gli vengono portati e la risposta torna nell'interfaccia del programma.

**Fatto e problemi risolti**

- Verificato con 16 test su un server vero con fornitori finti: OpenAI→fornitore OpenAI, Anthropic→fornitore OpenAI, OpenAI→fornitore Anthropic, streaming, strumenti, errore del fornitore (il programma riceve l'errore, mai la risposta del modello locale), /v1/models con il modello del fornitore, count_tokens stimato.
- La funzione non segue il Modello primario, non entra negli scambi pesanti e non ricade sul modello caricato. Le parti di TaDaMoo (pagina Chat, servizio voce, i suoi client, il banco di prova) si riconoscono da un'intestazione o dall'user agent e non vengono mai portate via.
- La riga informativa sotto la tabella dice ora chi risponde; /v1/responses risponde solo il modello caricato e con un fornitore scelto dà un errore che lo dice.

**Decisioni e punti aperti**

- Scelto da me: la funzione non chiede il «sì» per i dati personali (passano i dati del programma, non i tuoi); chi ha la chiave del server consuma la quota del fornitore scelto, e lo dice la riga.
- Resta da fare: /v1/responses attraverso un fornitore (Codex lo usa).

**Problemi futuri previsti e come pensiamo di risolverli**

- Non provato con un fornitore vero né con la pagina nel browser con una scelta fatta: i test usano fornitori finti e la tabella è guidata dai dati del server.

## Altri task da fare, senza riavviare TaDaMoo

### 32. Risposte più lunghe (in attesa)

In attesa (23 novembre): l'utente vuole provare a farlo con una LoRA; ci si pensa più avanti.

**Fatto e problemi risolti**

- Valutato il testo di Gemini: diagnosi plausibile, numeri non misurati; l'ID del token di fine è sbagliato per questo modello (qui 248044/248046).

**Decisioni e punti aperti**

- Prima: `end_turn_bias` negativo (già nel pannello Chat) con un limite alto e le continuazioni automatiche.
- Poi: una rampa sul token di fine nel campionatore, senza addestramento.
- Solo se degrada la qualità: SFT su risposte lunghe generate dal modello stesso.
- Rimandato al 30 novembre insieme a tutto il filone LoRA (decisione dell'utente, 23 novembre).

**Problemi futuri previsti e come pensiamo di risolverli**

- Addestrare su sequenze da 16.000-40.000 token non è realistico su questa CPU; il trainer non ha il DPO.

### 33. Controllo del telefono via adb e caricamento automatico del modello per la voce (non iniziato)

Due idee ancora da progettare.

**Decisioni e punti aperti**

- adb: tocca, leggi lo schermo, scorri, senza installare nulla; canale separato dalla app Flutter, più fragile se cambia l'interfaccia di un'app.
- Voce: oggi non esiste un caricamento automatico del modello quando una funzione vocale ne ha bisogno; costa tempo e VRAM, da decidere insieme.

### 39. Registro: link a commit e rami, vista per telefono (fatto)

Punto 7 del registro, approvato il 23 novembre: ogni task porta ramo e commit; la scheda Tasks si vede anche nell'app Android, in sola lettura.

**Fatto e problemi risolti**

- Fatto (23 novembre sera, commit f0a13eaa e 60b4b487): ogni task del registro porta il ramo git e i commit (campi `git_branch` e `commits`, aggiornabili da `/api/register/update`; i commit si scartano se non sono id esadecimali). Nella scheda Tasks del web ramo e commit sono collegamenti alla pagina del `origin` del progetto (senza credenziali, schema http/https mantenuto), testo semplice se il progetto non ha un remoto. Passano anche nel file `docs/roadmap.json` e nel testo `docs/roadmap.md`, e un file che non ne parla non cancella quelli del registro.
- Android: il registro è la terza parte, in sola lettura, della scheda Azioni (la barra in basso resta di 5 voci): per ogni progetto i task aperti con stato, fatto e mancante, ramo e commit che aprono la pagina del remoto. Nessun pulsante di modifica.
- Provato: 48 test del registro e della mappa, la scheda Tasks in un browser vero a tre larghezze (con il controllo dei collegamenti), 388 test dell'app Android (tre nuovi). Non provato su un telefono vero.

**Decisioni e punti aperti**

- Approvato dall'utente. Condizione: non ricompilare l'app in funzione (TaDaMoo sta facendo compiti): il codice si scrive e si prova in un ramo, con un server di prova su una porta a parte; niente eseguibile, niente APK.
- Prima parte: nome del ramo e commit nel registro e nella scheda Tasks (copia, apri le modifiche). Seconda parte: la scheda Tasks in Flutter, solo lettura.

**Problemi futuri previsti e come pensiamo di risolverli**

- Non scrivere il file dei task del progetto Basalt prima del riavvio di TaDaMoo.

### 42. App desktop: «Spegni» dall'icona nella barra (in attesa)

Il 23 novembre «Spegni» non ha spento il server: la conferma comparisce dietro le altre finestre (la finestra principale proprietaria è nascosta), con due clic ne restano due, lo spegnimento gira sul thread della finestra e la blocca, e il server è un servizio Windows (NSSM) che l'app non considera.

**Fatto e problemi risolti**

- Verificato (23 novembre, 20:13): il servizio Windows `TaDaMoo` (NSSM) esegue `Avvia-TaDaMoo.ps1 -PerApp -Standby` con AppExit = Restart e attesa di 10 s. Quando l'utente ha terminato i processi, il servizio ha riavviato il server alle 20:13:31. Con questo servizio, spegnere con un kill non basta: serve fermare il servizio (`Stop-Service TaDaMoo` da amministratore).
- Corretto nel codice (23 novembre sera, commit 663a8559): «Spegni» porta la finestra in primo piano prima di chiedere, un secondo clic non apre una seconda conferma, lo spegnimento gira fuori dal thread della finestra (non la congela più) e `Ferma-TaDaMoo.ps1` ferma il servizio Windows che tiene acceso il server (con la richiesta di permesso di Windows se serve), solo se ha un processo proprio ed è NSSM o chiamato TaDaMoo/Basalt. Compila. Il riconoscimento del servizio è provato in sola lettura su un altro servizio NSSM; lo spegnimento vero non è provato.

**Decisioni e punti aperti**

- Da provare dall'app installata: icona nella barra, Spegni, risposta Yes, e il server (servizio) deve restare spento.

### 43. Idee da DeerFlow (da decidere) (in attesa)

Studio del codice di bytedance/deer-flow (docs/study-deerflow.md): dove fa meglio di noi e cosa potremmo imparare. Nessuna idea è stata avviata.

**Fatto e problemi risolti**

- Letto il codice (23 novembre sera) e confrontato con il nostro: otto punti dove fanno meglio, di cui quattro verificati nel codice (release con controllo delle versioni, un AGENTS.md per modulo, contratti JSON condivisi, errori dei fornitori con Retry-After e interruttore) e uno (citazioni dei subagenti) verificato come assente da noi; tre solo plausibili con la prova economica scritta nel documento.
- Deciso e fatto il 24 novembre: un AGENTS.md per progetto in cascata con quelli generali, e la sessione tecnica toglie i concetti ripetuti (tadamoo/guidelines.py, uso guidelines_merge). Le altre idee restano da decidere.

**Decisioni e punti aperti**

- Da decidere quali adottare. In ordine di costo: 1 controllo delle versioni più CHANGELOG (e allineare pubspec, VERSION e note), 2 AGENTS.md per modulo, 3 contratti JSON condivisi, poi le idee da misurare (Retry-After e interruttore, citazioni dei subagenti, riparazione della cronologia, compattazione a pezzi, segno «letto» nei messaggi).

### 46. Il terminale come interfaccia: una riga con un prefisso va all'harness (in corso)

In PowerShell (solo PowerShell, 7 e 5.1) una riga che comincia con § non viene eseguita dalla shell ma mandata all'harness, che risponde nel terminale. § apre una sessione nuova (la precedente di quella finestra viene dismessa e cancellata); §§ continua la sessione precedente e manda all'harness anche quello che è comparso nel terminale dall'ultima riga § o §§. Ogni riga è un solo prompt, senza limiti su strumenti, inferenze e durata. Il prefisso si cambia nelle impostazioni. Si distribuisce come scelta del setup. Richiesta del 24 novembre.

**Fatto e problemi risolti**

- Verificato: il terminale che ricordavi è PSLLMKit del progetto API-CLI (C:\Projects\API-CLI, con la War Room): un modulo PowerShell che fa girare un CLI (claude, codex, gemini, droid) come server HTTP o lo chiama direttamente. Non è un terminale e va nella direzione opposta: noi vogliamo un client verso il nostro server, che esiste già.
- Verificato sulla macchina: PSReadLine 2.4.5 (PowerShell 7) e 2.0.0 (PowerShell 5.1) permettono di intercettare Invio prima che la riga venga eseguita; Windows Terminal (wt.exe) è solo il contenitore e non intercetta niente. La parte che serve sta quindi nella shell, non nel terminale.
- Verificato (24 novembre): `python -m tadamoo session` è già un processo di conversazione di lunga durata che parla a righe di JSON (send, answer, cancel, mode… e gli eventi delta, ask, done): su di esso si costruisce un solo client da terminale, usato dal § e dal comando `tadamoo` (task 47).
- Fatto il 24 novembre: la sezione «Riga di comando» in Settings > General (prefisso, colore, server, chiave, livello) scrive terminal.json che il modulo legge; il modulo si prende da GitHub (nuspy/API-CLI, tag v0.4.0) quando si compila il setup; il setup chiede se installare la riga di comando e scrive un blocco nei profili di PowerShell e due profili nel menu di Windows Terminal.

**Decisioni e punti aperti**

- Deciso da te (24 novembre): prefisso §, configurabile in Settings; solo PowerShell; un prompt per riga, nessun limite su quello che l'harness può fare; § = sessione nuova e vecchia cancellata; §§ = stessa sessione più il contenuto del terminale dall'ultima riga § o §§; componente del setup, scelto quando si sceglie la riga di comando.
- Da confermare: «dall'ultima riga § o §§» è la mia lettura. Da decidere: il livello di permesso delle sessioni da terminale e dove compaiono le richieste di approvazione; il posto nelle impostazioni (proposta: Settings > General, una sezione «Riga di comando», in un file `terminal.json` che il modulo legge anche a server spento).
- Cambiato il 24 novembre: il § vive nel modulo di API-CLI (task 48), che parla con l'API HTTP delle conversazioni; niente client Python.
- Aggiunto il 24 novembre: i comandi che cambiano lo stato della shell sono ridigitati nel terminale (task 47); il loro risultato torna all'agente con il §§.
- Aggiunto il 24 novembre: il risultato atteso e il riepilogo finale dell'agente si stampano sotto la riga § in un colore diverso, configurabile nella stessa sezione delle impostazioni; predefinito blu elettrico (#2E7BFF, che su sfondo scuro ha contrasto circa 5:1; il #1F51FF puro ne ha circa 3,5:1). Le righe degli strumenti restano in grigio. Una barra a sinistra della riga tiene il testo riconoscibile anche senza colore.

**Problemi futuri previsti e come pensiamo di risolverli**

- Plausibile, non provato: Start-Transcript registra l'uscita della shell per ricavare il testo tra le righe § (in PowerShell 7 include l'uscita dei programmi nativi; in 5.1 va provato; i programmi a schermo intero come vim non si registrano). Prova più economica: un modulo di 40 righe con Start-Transcript e un Invio intercettato da PSReadLine.
- Il file del modulo deve essere UTF-8 con BOM, o PowerShell 5.1 legge male il § (due byte).

### 47. Il comando `tadamoo`: un agente TaDaMoo in qualunque cartella, come Claude Code (in corso)

Scrivendo `tadamoo` in una cartella qualsiasi del terminale parte una conversazione interattiva con un agente TaDaMoo che lavora in quella cartella: risposta che scorre, una riga per ogni strumento, richieste di approvazione con sì o no digitato, Ctrl+C che ferma. Con `tadamoo -p "..."` fa un solo compito. Si installa con il setup, che mette il comando nel PATH. Richiesta del 24 novembre.

**Fatto e problemi risolti**

- Verificato: oggi `python -m tadamoo` fa solo il compito singolo (`-p` obbligatorio) e il comando `tadamoo` non esiste nel PATH di questa macchina; il processo di conversazione `python -m tadamoo session` esiste già e basta per la parte interattiva.
- Fatto il 24 novembre (ramo feat/terminal-cli, commit 2f0adeff, non unito): il server accetta `terminal` con un messaggio, dà all'agente la nota `[terminal]` e lo strumento `terminal_run`, ed emette l'evento `terminal_command`; 12 test nuovi, compreso il processo di conversazione vero.
- Fatto il 24 novembre: oltre al server (terminal_run), il comando `tadamoo` e le domande di permesso nel terminale sono nel modulo; il comando che l'agente propone resta sul prompt in attesa del tuo Invio.

**Decisioni e punti aperti**

- Deciso da te (24 novembre): entrambe le cose. L'agente lavora nella cartella del terminale con i suoi strumenti, in un processo suo (come Claude Code); in più, alla fine, può far eseguire alla tua shell qualunque cosa la shell sappia fare (cd, variabili d'ambiente, ambienti virtuali, creare cartelle, aprire SSH): i comandi vengono «ridigitati» nel tuo terminale dal modulo.
- Il client è il modulo PowerShell di API-CLI (task 48), non un programma Python.
- Deciso da te (24 novembre): il comando che l'agente vuole far eseguire alla tua shell viene scritto già pronto su una nuova riga: tu lo controlli e premi Invio, oppure lo modifichi e poi premi Invio. Non parte da solo.
- Il terminale dice all'agente in quale cartella si trova (a ogni riga § e §§, perché con un cd può essere cambiata) e che shell è; l'agente ottiene l'elenco dei file e delle cartelle con i suoi strumenti, non passando dal terminale.
- Deciso da te (24 novembre): gli strumenti dell'agente partono da soli; solo il comando per la tua shell resta scritto in attesa del tuo Invio. Una richiesta come «crea la cartella ciao e entraci» diventa una sola riga (`mkdir ciao; cd ciao`): niente coda di comandi, la riga usa la sintassi della shell che il terminale ha dichiarato (in PowerShell 5.1 niente `&&`).

**Problemi futuri previsti e come pensiamo di risolverli**

- Plausibile, non provato: per ridigitare i comandi nella tua sessione il modulo mette la riga nel buffer di PSReadLine (Insert e AcceptLine) quando la shell torna al prompt, senza Invio; così, dopo il tuo Invio, il comando gira nell'ambito globale, finisce nella cronologia, e `cd`, variabili e attivazione di ambienti virtuali valgono davvero. Prova più economica: 20 righe con un gancio sul prompt che scrive `cd C:\prova`, in PowerShell 7.7 (PSReadLine 2.4.5) e in 5.1 (2.0.0), in una finestra vera di Windows Terminal.
- Serve uno strumento nuovo per le sessioni da terminale (`terminal_run`) e un evento nuovo nelle conversazioni (`terminal_command`) che il modulo legge; l'uscita dei comandi ridigitati resta nel testo del terminale, che il §§ riporta all'agente.
- Dopo `ssh host` il comando successivo si scrive solo quando il prompt torna, cioè dopo l'uscita da SSH; comandi da eseguire dentro la sessione SSH non sono coperti dalla prima versione.

### 48. API-CLI diventa l'unico prodotto da terminale: § , comando tadamoo e War Room (in corso)

Decisione del 24 novembre: non si distribuiscono più programmi. Il modulo PSLLMKit di API-CLI (C:\Projects\API-CLI, repository nuspy/API-CLI) viene modificato (anche nel protocollo, se serve) e porta tutto: TaDaMoo come app raggiungibile con HTTP e la sua chiave, il prefisso § e §§ (task 46), il comando `tadamoo` interattivo in qualunque cartella (task 47) e la War Room come parte opzionale. Il setup di TaDaMoo lo include come componente «Riga di comando». Un solo comando, niente decine di CLI.

**Fatto e problemi risolti**

- Verificato: gli adattatori stanno in `webapp/src/server/cliAdapters/` (base.js, claude.js, codex.js, droid.js, gemini.js: `buildArgs` e `parseOutput`); PSLLMKit ha `Start-LLMServer`, `Invoke-LLMPrompt` e simili in `Public/`.
- Verificato: il server ha già l'API HTTP delle conversazioni (`POST /api/agent/sessions` con la cartella di lavoro, `POST .../send`, `GET .../events?after=N`, le risposte alle domande, dal PC e da un telefono con la chiave: docs/harness.md): il modulo PowerShell può fare da solo il client, anche verso un server su un altro PC.
- Fatto il 24 novembre (API-CLI, ramo feat/tadamoo-terminal, commit 20726c1, non pubblicato): `Enable-TaDaMooPrefix`, `Invoke-TaDaMooLine` (§ e §§), il comando `tadamoo`, colore, domande di permesso nel terminale, comando lasciato sul prompt; si importa in PowerShell 5.1 e 7. Provato contro un server finto che parla il protocollo, nelle due versioni: § , §§ (stessa conversazione), domanda di permesso, comando in attesa. NON provato: il gancio su Invio di PSReadLine e la scrittura sul prompt (servono una finestra vera e le tue mani), il testo del terminale con Start-Transcript, e un server TaDaMoo vero con un modello vero.
- Fatto il 24 novembre: API-CLI ha il ramo feat/tadamoo-terminal (versione 0.4.0) con il modulo che gira in PowerShell 5.1 e 7. Per la distribuzione serve unire quel ramo a master di API-CLI e creare il tag v0.4.0 su GitHub: è un'azione tua da approvare.
- 24 novembre sera: porta predefinita 37421 al posto della 8080, licenza uguale a TaDaMoo, tutto su master di API-CLI (8ed7972).

**Decisioni e punti aperti**

- Deciso da te: si riusa e si modifica API-CLI; un solo comando; niente client Python a parte.
- Da fare in API-CLI prima di distribuirlo: togliere la porta predefinita 8080 (vietata qui); farlo funzionare anche in PowerShell 5.1 (oggi chiede la 7); la licenza la decidi tu. Da decidere: se il setup include il codice di API-CLI al momento della compilazione (come già fa con agent-desktop in `third_party/`).

**Problemi futuri previsti e come pensiamo di risolverli**

- Il valore predefinito della porta di PSLLMKit è 8080 (`Private/Configuration.ps1`), che su questa macchina è vietata: va cambiato prima di distribuirlo. API-CLI non ha un file di licenza e PSLLMKit richiede PowerShell 7.
- Trovato provando: il server rifiuta un `mode` più alto del livello della chat (messaggio «raise the level of chat in its settings»). Il modulo non lo chiede più; se vuoi che gli strumenti partano sempre da soli, il livello della chat si alza in Settings, è una tua scelta. Mancano ancora: la sezione «Riga di comando» in Settings > General, la voce nel setup, l'unione con master.

### 49. Il Basalt originale è alla 0.5.7: cosa importare (da decidere) (in corso)

Analisi del 24 novembre di jesdga95/basalt (docs/study-upstream-basalt.md): il loro main ha 1.477 commit oltre il punto di fine ottobre che i nostri file ricordano, undici versioni (0.2.1-0.5.7), e ha cambiato la disposizione del codice. Importazione in corso nel ramo `merge/upstream-2026-11-24`: il motore è ricollegato e compila, restano il lato Python, il convertitore Qwen3.6 e le prove sulla GPU.

**Fatto e problemi risolti**

- Verificato: la nostra storia e la loro non hanno antenato comune (git merge-base non trova niente), quindi un git merge non si può fare: si portano le modifiche con tre vie da un commit di base (4a591d96). Cambiati da tutti e due: 247 file (72 in src, 36 in include, 46 in serve, 43 in tests).
- Candidati dai loro registri delle versioni (numeri loro, non misurati da noi): compilazione pulita da 933 a 290 secondi; avvio e RAM più robusti; prompt lunghi di Flash-Next fino al 22% più veloci; slot paralleli che decodificano insieme; video, PDF e Office nei messaggi; richieste immagini da altre macchine; Qwen3.8-27B denso in 16 GiB su una scheda.
- 24 novembre: master contiene tutti i rami con storia comune (45, 44, 46-48 compresi). Aperto il ramo `merge/upstream-2026-11-24` (partito da master c42cd7f4a) con la sua copia di lavoro su D: (`D:/Projects/basalt-wt-upstream`); la base dei confronti è il loro commit 4a591d96, il loro main è 351039841 (0.5.7). Non è stato importato niente: aspetta le tue indicazioni su cosa prendere e cosa trascurare.
- Correzione del 24 novembre: la base giusta per il confronto è il loro tag v0.2.0 (1 novembre), non 4a591d96. Il nostro motore è v0.2.0 più 9.112 righe aggiunte in 102 file (247 tolte). Un'unione a tre vie di tutto il loro main (0.5.7) con quella base dà 97 conflitti: 86 di contenuto, 10 modify/delete, 1 add/add (src/program 12, src/models 7, include 7, kernels 3, core e platform 2, serve e prove 20, docs circa 7).
- 24 novembre: il ramo `merge/upstream-2026-11-24` ha il loro v0.2.0 come antenato comune e la fusione con `v0.5.6` è cominciata senza commit nella copia di lavoro su D: (94 file in conflitto, 1.097 modifiche in attesa). Nulla è stato risolto né compilato.
- 24 novembre (sera): nel ramo `merge/upstream-2026-11-24` il motore è ricollegato sul loro `v0.5.6` in quattro commit (1942ff4ec, e0a1e00bc, 55510d524, 611d28bd0). Compila su Windows e supera 108 dei 109 test solo-CPU (il 109° è `migration_tests`, lato Python). NON provato sulla GPU.
- Rimesso sul motore loro: LoRA (fusione nelle matrici dense e negli esperti) con i quantizzatori IQ4 e i kernel Q4_K/Q5_K/Q6_K; arbitro della VRAM con KV elastico e prestito insieme (`reserve_max`, `capped`); restituzione del commit di Windows tra una richiesta e l'altra; impronta della cache delle conversazioni che include gli adattatori; opzioni `--lora`, `--lora-scaled`, `--no-lora-partial`, `--side-context`, `--vision-mmproj`, `--vision-release`, `--keep-vram-commit`, `--no-vram-elastic` (la VRAM elastica è accesa di default), `--no-pdl`; slot laterale con contesto per richiesta e riga INFO; motore Qwen3.6 (`src/qwen35`, circa 4.700 righe) agganciato prima del registro dei modelli.
- Letture dirette di Windows: non riportato il nostro codice in `pinned.cu`, perché il loro Loader unico legge già senza buffer su Windows. Differenza da misurare al collaudo: la nostra scelta a freddo o a caldo (teneva la cache dei file se la RAM bastava) non c'è più; si confrontano avvio a caldo, avvio a freddo e commit prima e dopo.
- Il margine di RAM scelto dall'utente (`BASALT_RAM_FREE_PCT`) non esiste più nel motore: si usa il loro `--ram-reserve-mib`, da calcolare lato Python.
- 24 novembre sera: i 81 file Python che fallivano sono riparati; il formato v1 della configurazione è tolto; il router dell'originale, la sua console web e la politica delle immagini non sono adottati (test tolti); trovati e corretti sei difetti veri (lettura immagine da 1 TiB, nome documento da file://C:\, lanciatore sul vecchio formato, stream Responses, spiegazioni mancanti, 408 del corpo).

**Decisioni e punti aperti**

- Proposta: le versioni sono cumulative, quindi si importa tutto fino al tag v0.5.6 in un'unica prova e le scelte da discutere si applicano come impostazioni e predefiniti dopo; la 0.5.7 si valuta a parte.
- Avvertenza: DeepSeek-V4.1-Flash vuole circa 320 GB di RAM per andare bene; con 121 GiB e gli esperti su SSD i loro numeri danno 2,8 token al secondo. Questo PC ha 128 GiB e una RTX 5090.
- Metodo proposto per il motore: base = albero del loro v0.5.6; sopra si riapplicano le nostre parti in questo ordine: 1) LoRA e quantizzatori IQ, 2) arbitro della VRAM, `vram_backing` e letture di Windows, 3) Loader e server (config v2, opzioni). Ogni passo con compilazione e prove; alla fine le prove vere sulla GPU (prestito e restituzione di VRAM mentre serve, scambio per un lavoro pesante, tempo di caricamento, commit di Windows, prove LoRA P1-P7), solo con il tuo via libera per la GPU.
- Deciso da te (24 novembre): da importare 0.3.0 (Qwen 4 e DeepSeek), 0.3.2, 0.3.3 (video, punto di ripresa dei media, logprobs, answer-room, answer now, session affinity, /props), 0.4.0, 0.4.1, 0.5.0, 0.5.5, 0.5.6; le schede multiple della 0.5.7 si integrano anche se non si possono provare. Docker: per ora no.
- BOCCIATE: la politica delle immagini della 0.2.1 (da altre macchine solo `data:`, `--image-url-host`, `--image-dir`, `--image-remote-only`), i limiti di 32 MB e 64 megapixel e il rifiuto di EPS, ICO e JPEG 2000: un PC remoto deve poter far leggere un file locale al server, come prima. Si tiene Q8_0 come riferimento di qualità. Si tiene il nostro proiettore della visione (scelta, riscrittura in BF16, rilascio fra immagini; è BF16, non FP8). PDF e Office: si tengono i nostri e si integrano i loro. Lo scambio dei modelli resta il nostro. Answer now si collega al router dell'harness (guida, coda, urgente) e la session affinity usa gli identificatori di conversazione.
- Da non perdere: la restituzione a Windows del commit di memoria quando si carica la VRAM (`vram_backing`): si misura il commit prima e dopo il caricamento.
- Lasciati perdere (tua decisione 24 novembre): controllo di qualità, docs/precision, valutazione della 0.5.7, prove su GPU (la GPU serve alla demo). Resta tua scelta se portare la console web dell'originale.

**Problemi futuri previsti e come pensiamo di risolverli**

- La 0.5.7 rompe le opzioni multi-scheda (`gpu`, `gpu-mode`, `gpu-share`; `gpu: auto` tolto) e il Loader e il supervisore le passano. Ogni modifica che cambia i numeri o la velocità vuole l'A/B e il controllo di qualità di AGENTS.md.
- Le due zone che ti preoccupano (24 novembre). VRAM elastica: l'originale ha già esperti e cache elastici, prestiti (`loans`, `image_loan`), `POST /v1/vram` e dalla 0.3.0 un router che scambia i modelli (config v2 con `models`); da noi sopra ci sono `vram_arbiter` (110+82 righe), `vram_backing` (164+37, memoria di commit di Windows), letture dirette di Windows in `pinned.cu` (+152), crescita della KV (`kv.cpp` +127) e `memory.cpp`; i conflitti stanno proprio dove il nostro arbitro si aggancia al loro runtime rifatto (`state.hpp`, `runtime.cpp`, `kv.cpp`, `memory.cpp`, `pinned.cu`, `platform/memory.*`). LoRA: i file nuovi (`lora.cpp`, `lora_experts.cpp`, `cli/lora.cpp`, circa 850 righe, e i quantizzatori IQ, 1.420 righe) non hanno conflitti; i conflitti sono solo negli aggancio (`native_dense.cpp`, opzioni `--lora`, avvio, identità della conversazione).
- Resta da fare: lato Python (answer now con il router dell'harness e il pulsante in Chat, logprobs, documenti PDF e Office, video, id di sessione, `/props`, flag e configurazione v2 con un solo modello, `--ram-reserve-mib` al posto di BASALT_RAM_FREE_PCT, tolta la politica delle immagini 0.2.1 arrivata come file loro), convertitore Qwen3.6 (`tools/convert/convert.py`, 5 blocchi), `tests/vision/cuda_parity.cu`, architettura `120f-real` dei kernel fp4, pagine `docs/precision`, collaudi sulla GPU con il tuo via libera.

### 50. Modifica di documenti (PDF, Word, Excel, PowerPoint e gli altri formati): harness e modello (in corso)

Compito primario (24 novembre, tua richiesta): l'harness deve saper MODIFICARE i documenti, non solo leggerli e scriverli da zero, e sia l'harness sia il modello devono sapere di poterlo fare e come. Il motore Basalt (originale e nostro) legge soltanto: la modifica sta tutta negli strumenti dell'harness.

**Fatto e problemi risolti**

- Inventario (verificato nel codice): Word ha già quattro strumenti (`docx_outline`, `docx_read`, `docx_edit`, `docx_create`, con python-docx, a livello di run e con revisioni tracciate), registrati solo se python-docx è installato, e nel `.venv` dell'app non c'è. Per PDF, Excel e PowerPoint non esiste nessuno strumento di modifica. I formati di testo si modificano con `edit_file`; le immagini e i video con le competenze dedicate.
- Consapevolezza oggi: `tadamoo/awareness.py` dice al modello di leggere i documenti difficili con `read_document` e di usare `docx_edit` per inserire immagini; non dice quando e come modificare un PDF o un file Office.
- Prove di lettura a 131.072 token di contesto (24 novembre, motore reale con visione, tre vie: O = come l'originale, il file intero nel prompt; OC = come l'originale a pezzi con `pages`; U = il nostro agente con allegato e lettura a pezzi). Word breve: O e U giusti, O in 5 s con 830 token, U in 10 s con circa 35.000 token (il prompt dell'agente). PDF da 24.000 token: O e U giusti in 14 s. PDF da 238.000 token (400 pagine): O rifiutato dal motore (supera la finestra), OC 3 su 3 in 26 s di media, U 3 su 3 in 44 s. Scansione: O giusto in 41 s con 6.500 token (pagine come immagini), U sbagliato: l'agente non trova il file per `read_document` (e un allegato non è nell'area di lavoro), poi tenta un comando di shell e resta in attesa di un permesso. Foglio Excel da 20.000 righe (698.000 token): O rifiutato; U risponde 328 invece di 303 (sbaglia la riga e inventa la stringa da cercare) e sulla somma per regione e prodotto non finisce in 15 minuti. Conclusione: sotto circa il 40 per cento della finestra e per le scansioni vince la via diretta; sopra la finestra serve il lavoro a pezzi (con `pages` o con i nostri strumenti); per le tabelle grandi nessuna delle due va e serve uno strumento di interrogazione (filtro e somme) invece della lettura per righe. Lacune da chiudere: `read_document` non accetta un allegato per id; l'agente dà una risposta sicura e sbagliata sulla tabella grande invece di dire che non riesce.
- Fatti il 24 novembre dopo il via libera: (a) nuovo strumento `query_attachment` (SQL in sola lettura su CSV, TSV ed Excel; un foglio è una tabella, i numeri sono numeri): sul foglio da 20.000 righe l'agente ora risponde giusto a entrambe le domande (303 e 304.759, in 36 e 48 s, prima 328 sbagliato e un timeout); (b) `read_document` accetta `attachment:<id>`: la scansione ora è letta con Docling e la risposta è giusta (113 s); (c) un PDF che ha solo i segnaposto di pagina è riconosciuto come scansione e il manifesto dice come leggerlo. Da sistemare: l'agente scambia l'`id` del blocco dati (il riquadro «untrusted_data») con l'id dell'allegato e perde un giro; la via diretta (file intero al motore) per l'harness non esiste ancora e richiede parti di messaggio nuove, da decidere. La modifica di PDF, Excel e PowerPoint resta da fare: oggi esistono solo gli strumenti Word.
- Fatti il 24 novembre (branch di importazione): strumenti di modifica per Excel (`xlsx_outline`, `xlsx_read`, `xlsx_edit`, `xlsx_create`), PowerPoint (`pptx_*`) e PDF (`pdf_outline`, `pdf_find`, `pdf_edit`), e Word esteso a intestazioni, piè di pagina, note e caselle di testo; tutti con modifiche in ordine, tutto-o-niente, scrittura sicura e copia con `output` (il PDF scrive sempre un file nuovo). Limiti dichiarati: Excel rifiuta cartelle con grafici, tabelle pivot o macro e spostamenti di righe dove ci sono formule; nei PDF `replace_text` cambia le parole nella pagina solo con font semplici (sui PDF veri di Word, browser e LaTeX di solito non si può: lo dice e non cambia nulla), `cover_text` copre e riscrive ma lascia le parole vecchie nel file. Il modello lo sa: riga nel prompt costruita dagli strumenti registrati, competenza `document-editing`, pagina `docs/document-editing.md`; le librerie sono nei requisiti dell'installazione. Corretto un difetto che c'era già nel conteggio delle immagini di un Word con oggetti `pict`.
- Verificato sul modello reale (24 novembre): chiesto all'agente di cambiare un Word (prezzo nel corpo e nell'intestazione, grassetto mantenuto), un Excel (cella e riga nuova con formula), un PowerPoint (testo su tutte le slide e slide nuova) e un PDF semplice: tutti e quattro giusti al primo colpo, sulla copia e con l'originale intatto. Sul PDF vero di Word (guida da 111 pagine) `replace_text` dice che non si può e l'agente ripiega su `cover_text`; la prima volta si è perso in programmi propri e non ha concluso entro 7 minuti, dopo aver sistemato l'adattamento del testo al riquadro e il testo dell'errore ha concluso (ma ha poi riscritto l'originale sul posto). Sistemati anche i punti di interfaccia e agente: livelli di ragionamento in più (xhigh, max) nel selettore, nel comando /effort e nelle conversazioni; campo «token di ragionamento» accanto al selettore (vuoto = illimitato, come i token massimi); video allegabile nella Chat con il solo modello; trascinamento di file e cartelle sull'area del prompt (Chat e Agente, cartelle nominate per percorso, `.git` e simili saltati); «Solo domanda» ora mostra l'allegato fin dove c'è posto e dice che il resto non c'è invece di rimandare a strumenti che non ha; `/tokenize` e `/detokenize` ripristinati; il convertitore scrive di nuovo il blocco di campionamento. Suite Python dopo tutto questo: su 722 file ne falliscono 81 (53 del server, 28 dell'harness): circa metà dipende da ciò che manca in questo PC (cv2, scipy, librosa, moduli di newscast), l'altra metà da test scritti per l'interno dell'originale (router, `HTTP_BODY_S`, `engine_echo`) o per il formato v1 della configurazione, e da pagine che l'originale organizza in altro modo. Da decidere con te quali portare e quali riscrivere.
- Chiuso il 24 novembre sera: gli strumenti scrivono sempre una copia (nome.edited.ext, un allegato con attachment:<id>), la copia si offre con il link; nuovo ask_document (il file intero davanti al modello); provati Excel e PowerPoint veri e complessi (originale intatto; un Excel con grafico è rifiutato apposta). Il modello vero non li ha ancora usati (la GPU serve alla demo).

**Decisioni e punti aperti**

- Da fare: (1) provare con misure quale via conserva meglio il file per ogni formato (Word: modifica a livello di run contro giro per Markdown; PDF: sostituzione nel flusso del contenuto, sovrapposizione, rigenerazione); (2) strumenti nuovi per Excel (openpyxl) e PowerPoint (python-pptx) sul modello di quelli Word, e per PDF: moduli, estrazione e unione di pagine, rotazione, annotazioni, sostituzione di testo dove è fattibile e dichiarata come tale; (3) python-docx, python-pptx e openpyxl nei requisiti del setup; (4) istruzioni per il modello: una competenza integrata «modifica di documenti» e righe in `awareness.py` quando l'allegato o la richiesta riguardano un documento da cambiare; (5) mai riscrivere l'originale senza copia, verificare rileggendo.
- Licenze: niente PyMuPDF (AGPL) come dipendenza del progetto MIT; si usano pypdf, pdfplumber, pikepdf e reportlab.
- Da provare sul modello vero quando la GPU è libera: ask_document e il giro «modifica, copia, link in chat».

**Problemi futuri previsti e come pensiamo di risolverli**

- Un PDF non si modifica come un Word: il testo è posizionato, non scorre. Una sostituzione di testo può cambiare l'aspetto o lasciare il testo vecchio sotto una copertura; va dichiarato al modello e alla persona, e dove esiste il file di partenza (docx) si modifica quello.

### 51. Prompt dell'harness più corto: strumenti rari a caricamento a freddo (da provare con A/B) (in corso)

Il prompt dell'harness è di circa 58.000 caratteri, quasi tutti schemi di strumenti. Idea (24 novembre, tua): gli strumenti rari hanno nel prompt solo una riga e il modello chiede i dettagli prima di sceglierli. In prova, spenta di default, con un confronto A/B.

**Fatto e problemi risolti**

- Misurato (24 novembre): schemi e descrizioni degli strumenti pesano circa 51.000 dei 58.000 caratteri; i più grossi sono i quattro gruppi di modifica dei documenti, il browser, il computer e le competenze.
- Costruito il 24 novembre notte (e95ac81b5): interruttore --cold-tools (spento di default), strumento tool_help, 11 strumenti freddi (i quattro gruppi di scrittura dei documenti, repo_diagram, task_*): l'elenco degli strumenti passa da 51.362 a 31.837 caratteri (-38%) in un agente nudo; le chiamate sbagliate sono rifiutate dallo schema vero con il rimando a tool_help; l'eval porta l'interruttore e ha l'insieme «documents» con quattro compiti (Word, Excel, PowerPoint, PDF su copia) controllati da uno script. Documentazione: docs/cold-tools.md.
- A/B sul modello vero (24 novembre notte, 600 W, k = 5): documenti 20 su 20 in entrambe le versioni, core 50 su 50 in entrambe; token del prompt letti per prova -11% sui documenti e -29% su core (la soglia scritta era -30%); un giro in più sui documenti per tool_help (tempo +20% circa); nessuna chiamata rifiutata. Un primo tentativo con lo schema «largo» era fallito (il modello scriveva {} undici volte): corretto con lo schema a scheletro. Resta un'opzione, spenta di default. Dettagli in docs/cold-tools.md.
- Cold tools di default + interruttore in GUI + HotLoad/ColdLoad per skill (25 novembre): provato sul browser vero su un server di prova (porta 7333, dati temporanei); le conversazioni leggono la scelta quando partono.

**Decisioni e punti aperti**

- Progetto da verificare: la descrizione completa torna nel RISULTATO di una chiamata (`tool_help`), che è in coda ai messaggi e non cambia la cache del prefisso; l'elenco degli strumenti nel prompt non cambia durante la conversazione (cambiarlo invaliderebbe la cache). Lo strumento raro ha nel prompt una riga e uno schema largo (oggetto libero): la grammatica non obbliga più i parametri, li controlla il registro.
- A/B da fare (serve la GPU, dopo la demo): A = prompt di oggi, B = strumenti rari a freddo; stessi compiti, k almeno 5, a livello di ragionamento fisso. Misure: caratteri e token del prefisso, riuso della cache, giri in più, chiamate sbagliate o rifiutate dal registro, compiti riusciti (insieme core delle prove e i quattro casi di modifica dei documenti). Regola di vittoria scritta prima: B vince se i compiti riusciti non calano oltre il rumore e i token del prefisso scendono almeno del 30%.
- Da fare: l'A/B vero (serve un modello: la GPU dopo la demo, oppure il tuo account per il modellino della curiosità, qwen3.6-flash, che non usa la GPU); comandi in docs/cold-tools.md.
- 25 novembre: il risultato dell'A/B basta, strumenti freddi ACCESI di default (decisione dell'utente). Interruttore nelle impostazioni di inferenza («Carica gli strumenti rari su richiesta»); ogni skill installata ha l'interruttore HotLoad/ColdLoad in Impostazioni > Skills (oggi: Modifica di documenti = ColdLoad, tutte le altre HotLoad; le skill senza strumenti propri hanno l'interruttore che non cambia nulla, e la pagina lo dice).
- Prossimo: rifare la misura con l'elenco vero (browser, computer, server esterni) e valutare altri strumenti da rendere freddi.

**Problemi futuri previsti e come pensiamo di risolverli**

- Plausibile, non provato: un modello piccolo può non chiedere i dettagli e usare male lo strumento, o spendere un giro in più che costa più della cache risparmiata; prova più economica: i quattro casi di modifica dei documenti con k = 5 nelle due versioni.

### 52. Skill nei piani: per passo o per tutto il piano (in corso)

Un passo azione può avere una o più skill (solo per quel passo) e il piano può averne di sue (date a ogni passo, usate o no). Le istruzioni arrivano nel risultato di plan_step_start, come gli archivi RAG.

**Fatto e problemi risolti**

- Costruito (25 novembre): campo skills sul passo azione e sul profilo del piano, risoluzione in tadamoo/planner/skills.py, consegna in plan_step_start (quelle del piano una sola volta per conversazione), selettore nella pagina Piani, test in tests/test_planner_skills.py. Non provato ancora su un piano vero con il modello e sulla pagina nel browser.

**Decisioni e punti aperti**

- Oggi i passi per l'agente di un piano girano in una conversazione normale condivisa (la sessione tecnica legge solo le attese): le skill di un passo restano nella cronologia dopo il passo, e lo limita solo il testo («solo per questo passo»). Se un passo avrà una conversazione sua, riceverà le stesse liste all'avvio.
- Le skill del piano si danno una volta per conversazione: dopo una compattazione del contesto possono sparire (da decidere se ridarle).

**Problemi futuri previsti e come pensiamo di risolverli**

- Plausibile, non provato: una skill lunga data a ogni passo pesa sul contesto; per questo quelle di un solo passo vanno sul passo.

### 53. Monitor > VRAM Explorer: pagina propria, grafico nel tempo (totale e per processo) ed elenco che scorre (in corso)

Il motore di gputaskman (MIT, Scott Hanselman) è importato nel progetto. Nel Monitor c'è la sotto-scheda «VRAM Explorer», accanto a Server LLM, Hardware, LOGS e Security: un grafico dell'uso della VRAM nel tempo con un interruttore Totale / Per processo (aree impilate) e scale da 5 minuti a 24 ore, e l'elenco dei processi in un riquadro che scorre con l'intestazione ferma.

**Fatto e problemi risolti**

- Importati in serve/gputaskman/ i moduli senza interfaccia terminale (23 file, con la licenza MIT, anche in THIRD_PARTY_LICENSES); si importano con il nostro Python 3.10 (25 novembre).
- Server: storico lungo della VRAM per scheda (un punto ogni 10 s con il picco, 24 ore) su GET /api/vram/history; campionatore per processo (lettura ogni 10 s, 24 ore, media per intervallo) su GET /api/vram/processes, che parte alla prima richiesta della pagina; lettore a richiesta su GET /api/vram/explorer.
- Pagina: sotto-scheda del Monitor (non più un tab dentro Server LLM), interruttore Totale / Per processo, elenco con scorrimento proprio e intestazione fissa; nvidia-ml-py è nei requisiti.
- Provata sulla pagina vera con un server di prova (motore finto) sulla RTX 5090 con il modello dell'utente già caricato (basalt.exe a 21,8 GiB): grafico per processo, passaggio del mouse, scorrimento con 60 processi finti, larghezza di 390 px senza scroll orizzontale. Test: serve/tests/test_vram_explorer.py (29).

**Decisioni e punti aperti**

- Il grafico è nel tempo e non solo gli ultimi 60 secondi, e nella pagina del VRAM Explorer, non nella colonna di Server LLM (tue indicazioni del 25 novembre).
- Il grafico totale (NVML, picco) e quello per processo (contatori di Windows, media) leggono due fonti: i totali differiscono di qualche centinaio di MiB; la pagina lo dice.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il grafico per processo parte vuoto a ogni riavvio del server e comincia al primo apri-pagina: lo storico non è su disco.
- Non ancora nell'app in uso con il codice nuovo della pagina: serve unire in E: e riavviare il server.

### 54. Cache degli esperti: usare tutta la VRAM dopo il caricamento (elastic VRAM) (in corso)

Dopo il caricamento la cache degli esperti restava più piccola di quanto la VRAM permetterebbe (3,9–8,7 GiB liberi invece di ~1 GiB di riserva). Le due strade scelte con l'utente lavorano insieme: all'avvio si aspetta che la lettura della memoria libera si stabilizzi e, con l'elastic VRAM, la coda della cache viene smappata invece di riaprire la cache più piccola; a motore fermo, ogni 5 secondi, la VRAM che si libera viene rimappata e gli slot sono riempiti dalla RAM.

**Fatto e problemi risolti**

- Misurato sul log del motore (6 caricamenti): in 5 su 6 il piano calcola ~9600 slot, la lettura dopo la scrittura è «0 MiB free» e il codice di Basalt 0.5.6 toglie 1 GiB, 1 GiB e un quarto della cache; finisce a ~6550 slot con 8747, 8767, 4126, 3987 MiB liberi (l'unico caricamento senza riduzioni: 1023 MiB). Il grafico dell'utente è questa sequenza.
- Strada 1 (cache_size.cpp): lettura ripetuta ogni secondo finché basta, o si stabilizza, o passano 45 s; riduzione solo di quanto manca, fino a 24 volte.
- Strada 2 (cache_size.cpp, tiers.cpp, memory.cpp, commands.cpp, state.hpp): con la cache segmentata la coda viene smappata (shrink al confine di segmento) invece di riaprire più piccola, gli slot non mappati sono registrati come evicted nell'ordine del profilo, e il ciclo di attesa tra le richieste guarda ogni 5 s la VRAM libera e chiama cache_regrow (con nessun prestito in corso).
- Provato sul server vero (25 novembre): caricamento con 7662 slot e 1311 MiB liberi (prima 6537 e 4–8,7 GiB); cessione e ritorno con il comando VRAM; ricrescita periodica da 7461 a 8469 slot spegnendo Whisper; la stessa risposta a temperatura 0 in tutti e cinque i momenti; nessun errore nel log.
- Compilato in D: e installato in E:\Projects\basalt\bin (commit 1e0d64a6c). Documentato in docs/engine-vram-arbiter.md.

**Decisioni e punti aperti**

- Strada 1 (tua): calcolare la memoria libera dopo il picco, tenendo conto di cosa viene rilasciato e quando.
- Strada 2 (tua): controlli periodici della VRAM libera e spostamento di esperti da RAM a VRAM con l'elastic VRAM, insieme alla strada 1 (25 novembre).
- Il motore usa la memoria libera che CUDA gli riporta (un tetto per processo, circa 26 GiB qui), non quella fisica: andare oltre significa allocare sopra il tetto che Windows concede, con il rischio di paginazione che il codice evita di proposito. Non cambiato.

**Problemi futuri previsti e come pensiamo di risolverli**

- Il tetto per processo è un'ipotesi plausibile non verificata con le API DXGI: la prova è che un processo nuovo vede 25.624 MiB liberi mentre NVML ne vede 3650.
- Riaccendere una funzione vocale «al caricamento» con il modello in esecuzione è rifiutato per il commit (HTTP 422): regola esistente, da rivedere a parte (lo stesso problema colpirebbe il pulsante di Models > Voice services).
- Resta da vedere nell'uso: una sessione lunga con molte richieste dopo una ricrescita, e il caso di una cessione (prestito) seguita da ricrescita mentre una richiesta è in corso.
- La VRAM che un altro programma tiene senza prestarla non viene ripresa dal motore: resta sua finché la libera.

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

**Fatto e problemi risolti**

- Riordino del disco E:: punto chiuso il 23 novembre; lo fa la sessione LTX Studio, non c'è altro da fare qui.

**Decisioni e punti aperti**

- Piano contro le istruzioni nascoste nelle pagine web: promemoria confermato per il 26-27 novembre.
- OCR e PDF: confermato su GPU, non su CPU.
- Piano contro le istruzioni nascoste nelle pagine web: RINVIATO (24 novembre), senza data; lo ripropongo quando riprendi le LoRA o me lo chiedi.

### 55. Loop sui layer di Flash-Next e/o LoRA per output e ragionamento più lunghi (idea, da decidere) (in attesa)

Idea dell'utente (25 novembre, dalla risposta di Gemini): ripetere un blocco di layer (loop transformers), con o senza LoRA sul punto di rientro, e un modo di allungare l'output e il ragionamento. Valutazione data, niente costruito.

**Fatto e problemi risolti**

- Valutata la risposta di Gemini: le idee di base (ripetere un blocco, smorzare il residuo, uno stato per iterazione) sono sensate in principio; i guadagni dei loop transformers in letteratura vengono da modelli addestrati con ricorrenza (non verificato qui); ortogonalizzazione e scalatura SVD per «indurre un loop» sono speculative per questo modello.
- Strategia di tappa 0 ricevuta da basalt-69 (25 novembre, solo lettura del codice di master, niente compilato): modello «srotolato» a slot con mappa slot→layer sorgente, stato per iterazione, smorzamento scalando block_out, due prove d'identità gratuite, esperimento a eliminazione. Dettaglio nel diario.

**Decisioni e punti aperti**

- Il limite di ~4.000 token di risposta non è un problema di profondità: prima end_turn_bias e una rampa dell'EOS (nodo 32), senza addestramento.

**Problemi futuri previsti e come pensiamo di risolverli**

- Flash-Next ha 36 layer GDN con stato ricorrente (aggiornato una volta per token) e 12 di attenzione: un layer GDN ripetuto aggiorna lo stato due volte per lo stesso token, serve uno stato per (layer, iterazione). Il costo cresce con la frazione ripetuta, esperti compresi (un terzo della profondità ripetuto due volte: circa +33%).
- Proposta a tappe: (0) opzione del motore che ripete una finestra di layer k volte con smorzamento, misurata con PPL/KLD e le 70 prove (serve la GPU e il tuo via); (1) allungare l'output con un orientamento in inferenza; (2) la LoRA solo se la tappa 0 promette, dopo la ripresa dell'addestramento LoRA su GPU (settimana del 30 novembre).
- Prima del codice, da decidere: (1) il file di riferimento del banco di qualità non esiste (base .kld di llama.cpp, o una base scritta dal nostro motore a loop spento); (2) cosa significa «k = 5» nelle 70 prove (5 corse per prova, secondo le misure del 24 novembre) e quanto dura un giro; (3) la configurazione usa una sola scheda (dal log del motore: una RTX 5090).
