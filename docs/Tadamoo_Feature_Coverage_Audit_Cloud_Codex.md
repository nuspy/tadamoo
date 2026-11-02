# Tadamoo --- Feature & Capability Verification Checklist

## Audit funzionale e architetturale per Cloud Codex

**Scopo del documento:** verificare sistematicamente quali funzionalità
di Tadamoo siano realmente implementate, integrate e testate,
individuare i gap e consentire di completarli rapidamente senza
riscrivere ciò che già funziona.

**Destinatario:** Cloud Codex / agente di coding incaricato dell'audit
del repository.

**Tipo di documento:** checklist di verifica tecnica e funzionale. Non
assumere che una funzione esista solo perché è menzionata nella
documentazione, compare nella UI o è parzialmente implementata.

------------------------------------------------------------------------

## 0. Istruzioni operative per Cloud Codex

### 0.1 Regole dell'audit

0.1.1. Esaminare il repository, la configurazione, il codice, gli
script, le migrazioni, le dipendenze, i test e la documentazione prima
di formulare conclusioni.

0.1.2. Verificare ogni requisito a livello di implementazione, non solo
di interfaccia. Un pulsante, una schermata, una classe vuota, un TODO,
un mock o un endpoint non collegato non costituiscono una funzione
implementata.

0.1.3. Per ogni voce assegnare uno stato: - **IMPLEMENTATA E
VERIFICATA** --- codice funzionante e test o prova riproducibile. -
**IMPLEMENTATA, DA VERIFICARE** --- implementazione plausibile, ma
test/prova insufficienti. - **PARZIALE** --- solo una parte del
requisito è presente. - **UI/MOCK SOLTANTO** --- visibile
nell'interfaccia o simulata, senza comportamento reale completo. -
**ASSENTE** --- non trovata. - **BLOCCATA/ESTERNA** --- dipende da
credenziali, servizio, hardware o componente esterno non disponibile. -
**NON APPLICABILE** --- motivare esplicitamente.

0.1.4. Per ogni voce registrare: stato, file e simboli rilevanti,
dipendenze, test esistenti, prova eseguita, gap, rischio e azione minima
consigliata.

0.1.5. Non dichiarare verificata una funzione solo perché esiste una
funzione con nome simile. Tracciare il flusso end-to-end: UI/API →
orchestrazione → runtime/servizio → risultato → persistenza/log/audit.

0.1.6. Non modificare indiscriminatamente l'architettura. Prima produrre
l'audit e una lista ordinata dei gap; poi proporre patch piccole,
coerenti con gli standard esistenti. Non riscrivere componenti già
funzionanti.

0.1.7. Distinguere sempre: - ciò che è descritto come requisito del
prodotto; - ciò che è dimostrato dal codice; - ciò che è stato
verificato con un test; - ciò che è una previsione o una funzionalità
futura.

0.1.8. Per integrazioni esterne non disponibili, verificare adapter,
contratti, gestione degli errori e test mockati; segnalare che la
verifica reale richiede credenziali o ambiente appropriato.

0.1.9. Non esporre segreti, token, chiavi API, password o dati personali
nei report. Non stampare valori segreti durante l'ispezione.

0.1.10. Prima di implementare funzioni che compiono azioni esterne con
effetti reali (pagamenti, acquisti, email, prenotazioni, modifiche
infrastrutturali), verificare autorizzazioni, conferme richieste,
idempotenza, audit e possibilità di annullamento/recupero.

### 0.2 Output richiesti da Cloud Codex

0.2.1. Un **Feature Coverage Matrix** con una riga per ogni requisito
numerato in questo documento.

0.2.2. Una mappa dei componenti: moduli, servizi, UI, API, database,
runtime, adapter e dipendenze.

0.2.3. Una lista dei gap ordinata per: - P0 --- sicurezza, perdita dati,
autorizzazioni errate, azioni esterne non sicure, blocchi
fondamentali; - P1 --- funzionalità centrali non implementate o non
affidabili; - P2 --- funzionalità importanti ma non bloccanti; - P3 ---
rifiniture, telemetria, usabilità e ottimizzazioni.

0.2.4. Un piano di chiusura dei gap con patch piccole, dipendenze, test
di accettazione e criterio esplicito di completamento.

0.2.5. Un elenco delle affermazioni prestazionali che richiedono
benchmark riproducibili.

0.2.6. Un elenco delle funzionalità Enterprise che richiedono backend
centrale, identity provider, policy, infrastruttura o decisioni di
prodotto non presenti nel repository.

------------------------------------------------------------------------

# 1. Identità del prodotto e architettura generale

## 1.1 Prodotto e principi

1.1.1. Verificare che l'applicazione sia una workstation AI locale per
Windows con GUI, non soltanto un launcher di modelli.

1.1.2. Verificare che il prodotto sia architetturalmente indipendente
dal runtime Basalt/Strata e non lo tratti come unico backend
obbligatorio.

1.1.3. Verificare la separazione fra: - applicazione/UI; -
orchestrazione e gestione delle sessioni; - adapter dei runtime; -
provider remoti; - strumenti, MCP, plugin e skill; - memoria e
retrieval; - planner e gestione dei piani; - persistenza e
configurazione; - logging, sicurezza e autorizzazioni.

1.1.4. Verificare che la sostituzione o l'aggiunta di un runtime non
richieda modifiche invasive al core.

1.1.5. Verificare che le funzionalità personali e quelle Enterprise
siano distinguibili e che il prodotto possa operare localmente senza
dipendere obbligatoriamente da un server centrale.

1.1.6. Verificare gestione di configurazioni, versioni, migrazioni,
compatibilità e aggiornamenti.

## 1.2 Modularità e contratti

1.2.1. Verificare interfacce/contratti stabili per runtime, modelli,
strumenti, skill, memoria, planner, provider e sistemi documentali.

1.2.2. Verificare validazione dei parametri e degli input/output ai
confini dei moduli.

1.2.3. Verificare che gli errori di un modulo non compromettano l'intera
applicazione e che siano restituiti errori comprensibili.

1.2.4. Verificare timeout, cancellazione, retry limitati, circuit
breaker o meccanismi equivalenti dove appropriato.

1.2.5. Verificare versionamento delle API interne e compatibilità fra
componenti.

1.2.6. Verificare avvio, arresto, riavvio e recupero dopo crash o
interruzione.

## 1.3 Windows e installazione

1.3.1. Verificare installazione e avvio su Windows supportato.

1.3.2. Verificare rilevamento dei percorsi, permessi, directory dati,
cache, log e file temporanei.

1.3.3. Verificare funzionamento con spazi e caratteri non ASCII nei
percorsi.

1.3.4. Verificare che l'app non richieda privilegi amministrativi senza
necessità.

1.3.5. Verificare aggiornamento, disinstallazione e conservazione
controllata dei dati utente.

1.3.6. Verificare gestione di processi figli, runtime locali e
terminazione pulita.

------------------------------------------------------------------------

# 2. Runtime, modelli e prestazioni di inferenza

## 2.1 Gestione multi-runtime

2.1.1. Verificare che Basalt/Strata sia integrato come uno dei runtime
supportati, non come dipendenza architetturale inevitabile.

2.1.2. Verificare discovery, registrazione, configurazione, avvio,
arresto e health check dei runtime.

2.1.3. Verificare adapter per runtime locali differenti, se dichiarati
nel prodotto.

2.1.4. Verificare provider remoti tramite API, inclusi endpoint
OpenAI-compatible e Anthropic-compatible quando supportati.

2.1.5. Verificare selezione del runtime per modello, task, profilo o
policy.

2.1.6. Verificare fallback controllato quando un runtime non è
disponibile, senza cambiare silenziosamente modello o livello di
privacy.

2.1.7. Verificare che le funzionalità non supportate da un runtime siano
rilevate e comunicate chiaramente.

2.1.8. Verificare compatibilità e mapping dei parametri: context length,
temperature, top-p, max tokens, stop sequences, tool calling, streaming,
structured output e parametri specifici.

## 2.2 Discovery e gestione dei modelli

2.2.1. Verificare elenco dei modelli disponibili e delle loro fonti.

2.2.2. Verificare metadati: famiglia, dimensione, quantizzazione,
context window, requisiti di memoria, capacità multimodali, tool calling
e supporto decoding.

2.2.3. Verificare caricamento, scaricamento, cambio modello e gestione
di modelli mancanti o corrotti.

2.2.4. Verificare compatibilità tra modello, runtime, hardware e formato
dei pesi.

2.2.5. Verificare configurazioni predefinite e profili modello per task
differenti.

2.2.6. Verificare che la UI distingua modelli installati, disponibili da
scaricare, remoti e non accessibili.

## 2.3 Gestione hardware e memoria

2.3.1. Verificare rilevamento di GPU, VRAM, RAM, CPU e informazioni
rilevanti del sistema.

2.3.2. Verificare stima del fabbisogno di memoria prima del caricamento,
con margine di sicurezza.

2.3.3. Verificare gestione di modelli che distribuiscono
pesi/layer/esperti tra VRAM e RAM, quando supportata dal runtime.

2.3.4. Verificare che il posizionamento dei componenti in GPU/RAM sia
configurabile o gestito dal runtime e che lo stato effettivo sia
osservabile.

2.3.5. Verificare gestione OOM, fallback, scaricamento di altri modelli
e messaggi di errore utili.

2.3.6. Verificare che l'app non presuma che DDR5 produca un incremento
fisso: la prestazione deve essere misurata sul sistema e sul modello
reali.

2.3.7. Verificare monitoraggio di VRAM/RAM, utilizzo GPU, tempi di
caricamento e saturazione, ove disponibile.

2.3.8. Verificare profili per PC consumer differenti, incluse GPU NVIDIA
di generazioni diverse, senza dichiarare compatibilità non testata.

## 2.4 Streaming e misure di prestazione

2.4.1. Verificare streaming dei token verso la GUI e gestione di chunk,
interruzioni e cancellazione.

2.4.2. Verificare misurazione separata di time-to-first-token, token/s
in generazione, latenza totale e tempo di tool execution.

2.4.3. Verificare che i token/s riportati abbiano definizione coerente e
non confondano throughput di prefill e decode.

2.4.4. Verificare benchmark riproducibili con modello, quantizzazione,
prompt, contesto, hardware, runtime, impostazioni e versione registrati.

2.4.5. Verificare confronto controllato tra Basalt/Strata e altri
runtime sullo stesso hardware e carico.

2.4.6. Verificare che risultati come 250 token/s su un modello da circa
120B, 500--600 token/s con DDR5 o 1.000 token/s su modelli più piccoli
siano trattati come risultati da dimostrare con benchmark, non come
garanzie hardcoded.

2.4.7. Verificare raccolta di benchmark per task reali, non soltanto
prompt sintetici.

## 2.5 Accelerazione del decoding

2.5.1. Verificare supporto N-gram o altre forme di speculative decoding,
quando offerte dal runtime.

2.5.2. Verificare supporto MTP (Multi-Token Prediction) solo per
modelli/runtime compatibili.

2.5.3. Verificare configurazione delle bozze/draft, soglie di
accettazione e parametri rilassati, se previsti.

2.5.4. Verificare che le modalità di accelerazione possano essere
abilitate/disabilitate e confrontate.

2.5.5. Verificare misurazione del tasso di accettazione delle bozze,
overhead di verifica e throughput effettivo.

2.5.6. Verificare che l'accelerazione non alteri silenziosamente i
requisiti di qualità o correttezza.

------------------------------------------------------------------------

# 3. Chat, interfaccia grafica e interazione multimodale

## 3.1 Esperienza chat

3.1.1. Verificare chat persistenti, creazione, rinomina, archiviazione,
ricerca e cancellazione.

3.1.2. Verificare streaming delle risposte, stop generation, retry e
rigenerazione.

3.1.3. Verificare modifica/copia dei messaggi e gestione degli errori.

3.1.4. Verificare allegati e gestione di file supportati.

3.1.5. Verificare rendering di Markdown, codice, tabelle, formule e
contenuti strutturati.

3.1.6. Verificare indicatori chiari per generazione, tool in esecuzione,
attesa di conferma, errore e completamento.

3.1.7. Verificare recupero della sessione dopo riavvio, secondo le
policy di persistenza.

## 3.2 Rendering in linea e contenuti visuali

3.2.1. Verificare rendering in linea di grafici, diagrammi e
visualizzazioni generate in risposta a un task.

3.2.2. Verificare che il sistema decida di produrre una visualizzazione
quando aiuta la comprensione, senza generarla inutilmente.

3.2.3. Verificare grafici con dati, assi, unità, legenda e provenienza
coerenti.

3.2.4. Verificare visualizzazioni di workflow, architetture e concetti.

3.2.5. Verificare rendering di immagini e fotografie degli articoli o
prodotti citati, quando disponibili e pertinenti.

3.2.6. Verificare che l'immagine sia collegata alla fonte corretta e che
l'assenza di un'immagine non interrompa la risposta.

3.2.7. Verificare che i link agli articoli citati siano cliccabili e
conducano alla fonte corretta.

3.2.8. Verificare fallback per contenuti non supportati o risorse non
raggiungibili.

## 3.3 Fonti e citazioni

3.3.1. Verificare che le risposte basate su fonti esterne possano
includere citazioni/link verificabili.

3.3.2. Verificare che titolo, URL, data e fonte siano mantenuti quando
disponibili.

3.3.3. Verificare che il sistema distingua contenuti recuperati,
inferenze del modello e informazioni non verificate.

3.3.4. Verificare gestione di link rotti, pagine non accessibili e fonti
duplicate.

3.3.5. Verificare che le citazioni non vengano inventate quando una
fonte non è stata realmente recuperata.

## 3.4 Accessibilità e usabilità

3.4.1. Verificare navigazione da tastiera, focus visibile, leggibilità e
scaling della UI.

3.4.2. Verificare gestione di messaggi lunghi, grandi output e sessioni
con molti tool.

3.4.3. Verificare messaggi d'errore comprensibili e indicazione
dell'azione consigliata.

3.4.4. Verificare che le azioni distruttive o con effetti esterni siano
chiaramente distinguibili.

------------------------------------------------------------------------

# 4. Profili di colleghi AI, personalità e collaborazione

## 4.1 Profili dei colleghi

4.1.1. Verificare un catalogo di colleghi/agenti selezionabili
dall'utente.

4.1.2. Verificare che ogni profilo possa specificare personalità, stile
comunicativo, ruolo, specializzazione, limiti e istruzioni.

4.1.3. Verificare profili con orientamento strategico,
operativo/dettagliato, ricerca, coding o altre specializzazioni
effettivamente supportate.

4.1.4. Verificare che l'utente possa scegliere il collega principale per
una sessione o un task.

4.1.5. Verificare che il profilo selezionato influenzi davvero
comportamento e strategia, non soltanto il nome o l'avatar.

4.1.6. Verificare creazione, modifica, duplicazione, import/export e
cancellazione dei profili, se previsti.

4.1.7. Verificare persistenza delle preferenze per profilo e gestione
delle versioni.

## 4.2 Esecuzione multi-agente

4.2.1. Verificare esecuzione di più agenti contemporaneamente, se
supportata.

4.2.2. Verificare assegnazione dei task in base a specializzazione,
disponibilità, costo, latenza e autorizzazioni.

4.2.3. Verificare gestione di dipendenze, risultati parziali, timeout e
fallimenti di singoli agenti.

4.2.4. Verificare aggregazione dei risultati e risoluzione di conflitti
tra agenti.

4.2.5. Verificare che gli agenti non si attribuiscano reciprocamente
autorizzazioni non possedute.

4.2.6. Verificare limiti di concorrenza, budget token/costo e
prevenzione dei loop agentici.

4.2.7. Verificare tracciabilità di chi ha prodotto un risultato, quali
strumenti ha invocato e su quali fonti si è basato.

4.2.8. Verificare modalità solo-agente-principale e modalità
multi-agente, con comportamento esplicito e comprensibile.

------------------------------------------------------------------------

# 5. Provider, sottoscrizioni, API e servizi esterni

## 5.1 Provider e token factory

5.1.1. Verificare supporto dei provider effettivamente dichiarati dal
progetto.

5.1.2. Verificare configurazione di endpoint, modello, credenziali e
parametri per provider.

5.1.3. Verificare supporto a servizi di token factory o gateway, quando
integrati.

5.1.4. Verificare rotazione, aggiornamento e revoca delle credenziali
senza esporle nei log.

5.1.5. Verificare gestione di rate limit, quote, errori di
autenticazione, indisponibilità e costi.

5.1.6. Verificare selezione del provider in base a task, policy,
latenza, costo, privacy e capacità.

5.1.7. Verificare fallback solo secondo policy esplicite, senza inviare
dati locali a provider remoti senza autorizzazione.

## 5.2 API compatibili

5.2.1. Verificare endpoint OpenAI-compatible, se dichiarati.

5.2.2. Verificare endpoint Anthropic-compatible, se dichiarati.

5.2.3. Verificare autenticazione, streaming, errori, formati di
richiesta/risposta e compatibilità documentata.

5.2.4. Verificare che applicazioni esterne possano utilizzare il motore
senza dipendere dalla GUI.

5.2.5. Verificare limiti di concorrenza, gestione dei client e
isolamento delle sessioni.

5.2.6. Verificare documentazione API e test di compatibilità.

## 5.3 Abbonamenti e accesso autenticato

5.3.1. Verificare l'eventuale integrazione con sottoscrizioni di servizi
come Gemini, Codex o Anthropic solo tramite modalità autorizzate e
supportate.

5.3.2. Verificare che non siano aggirati limiti, autenticazione o
termini dei provider.

5.3.3. Verificare gestione sicura di sessioni/token e scadenze.

5.3.4. Verificare che il prodotto comunichi quali account e capacità
sono effettivamente collegati.

5.3.5. Verificare che l'utente possa disconnettere/revocare
un'integrazione.

------------------------------------------------------------------------

# 6. Strumenti, MCP, plugin e skills

## 6.1 MCP

6.1.1. Verificare discovery, configurazione, avvio e arresto dei server
MCP.

6.1.2. Verificare elenco e schema degli strumenti MCP disponibili.

6.1.3. Verificare validazione di argomenti e risultati rispetto agli
schemi.

6.1.4. Verificare timeout, cancellazione, retry controllati e gestione
di server non disponibili.

6.1.5. Verificare autorizzazioni per singolo server/tool e approvazioni
per operazioni rischiose.

6.1.6. Verificare isolamento dei processi e gestione sicura di
input/output non attendibili.

6.1.7. Verificare logging delle chiamate senza registrare segreti.

## 6.2 Plugin e tool calling

6.2.1. Verificare registrazione e catalogo dei plugin.

6.2.2. Verificare che il modello possa richiedere tool e che
l'orchestratore esegua soltanto tool realmente autorizzati.

6.2.3. Verificare parsing, validazione, esecuzione e ritorno dei
risultati al modello.

6.2.4. Verificare che i risultati dei tool siano marcati come dati
esterni non attendibili e non possano alterare le policy del sistema.

6.2.5. Verificare gestione di chiamate multiple, sequenziali e
parallele.

6.2.6. Verificare prevenzione di chiamate duplicate o ricorsive non
controllate.

## 6.3 Skills esistenti

6.3.1. Verificare catalogo delle skill, descrizione, input, output,
dipendenze e autorizzazioni.

6.3.2. Verificare esecuzione di skill, parametri, validazione e
restituzione dei risultati.

6.3.3. Verificare versionamento, abilitazione/disabilitazione e
disinstallazione.

6.3.4. Verificare dipendenze, ambiente di esecuzione e gestione degli
errori.

6.3.5. Verificare provenienza e livello di fiducia delle skill.

6.3.6. Verificare che una skill non possa ottenere accessi superiori a
quelli autorizzati all'utente e al contesto di esecuzione.

------------------------------------------------------------------------

# 7. Generazione, miglioramento e riutilizzo delle skill

## 7.1 Riconoscimento di attività ripetitive

7.1.1. Verificare che il sistema possa individuare pattern ripetitivi
nelle attività dell'utente, nei limiti dei dati e delle autorizzazioni
disponibili.

7.1.2. Verificare proposta di creare una skill automaticamente o su
richiesta dell'utente.

7.1.3. Verificare che l'utente possa accettare, rifiutare, modificare o
rinviare la proposta.

7.1.4. Verificare che il sistema non trasformi automaticamente ogni
singolo task in una skill senza evidenza sufficiente.

## 7.2 Progettazione ibrida: codice deterministico + LLM

7.2.1. Verificare che il generatore identifichi le fasi deterministiche
e quelle che richiedono ragionamento LLM.

7.2.2. Verificare generazione di script (ad esempio Python) per le fasi
deterministiche, se previsto.

7.2.3. Verificare che l'LLM venga utilizzato solo nelle fasi in cui è
necessario, senza sacrificare la correttezza.

7.2.4. Verificare che parametri della query/inferenza originale possano
essere resi configurabili nella skill, ove opportuno.

7.2.5. Verificare che input, output, precondizioni, postcondizioni e
criteri di successo siano espliciti.

7.2.6. Verificare che la skill sia riproducibile e non dipenda
implicitamente dallo stato volatile della conversazione.

## 7.3 Test e validazione delle skill

7.3.1. Verificare generazione di test a partire da esempi di successo e
fallimento.

7.3.2. Verificare test su casi nominali, input invalidi, timeout,
servizi non disponibili e dati limite.

7.3.3. Verificare ambiente di prova/sandbox per codice generato.

7.3.4. Verificare revisione/approvazione prima dell'esecuzione con
privilegi o effetti esterni.

7.3.5. Verificare metriche di affidabilità e tasso di successo della
skill.

7.3.6. Verificare regressione dei test quando una skill viene
modificata.

## 7.4 Ciclo di miglioramento

7.4.1. Verificare che fallimenti e feedback possano generare una
proposta di miglioramento.

7.4.2. Verificare confronto tra versione precedente e nuova.

7.4.3. Verificare rollback e disattivazione di una skill peggiorata.

7.4.4. Verificare che il sistema distingua un caso particolare da una
regola generale.

7.4.5. Verificare che una nuova regola possa essere proposta, testata e
approvata prima di essere applicata globalmente.

7.4.6. Verificare tracciabilità di origine, data, motivazione, test e
approvazione di ogni modifica.

------------------------------------------------------------------------

# 8. Memoria personale, profilo utente e CAG

## 8.1 Memoria a lungo termine

8.1.1. Verificare una memoria persistente per informazioni importanti e
relativamente stabili.

8.1.2. Verificare memorizzazione di preferenze, profilo professionale,
competenze, stile di lavoro e altre informazioni esplicitamente ammesse.

8.1.3. Verificare deduplicazione, aggiornamento, confidenza, provenienza
e data dell'informazione.

8.1.4. Verificare gestione di informazioni contraddittorie e
obsolescenza.

8.1.5. Verificare che l'utente possa ispezionare, correggere ed
eliminare le informazioni secondo le funzioni del prodotto.

8.1.6. Verificare che informazioni sensibili siano gestite con
protezioni appropriate e non condivise automaticamente.

## 8.2 Memoria a breve termine

8.2.1. Verificare memoria contestuale delle interazioni recenti con
finestra nominale di circa un mese, se questa è la policy effettiva del
prodotto.

8.2.2. Verificare aggiornamento, scadenza e pulizia automatica della
memoria temporale.

8.2.3. Verificare che la memoria recente raccolga contesto operativo
utile: progetti in corso, attività, decisioni e impegni.

8.2.4. Verificare che la finestra temporale sia configurabile o
documentata.

8.2.5. Verificare che la memoria temporale non venga confusa con un
archivio completo e immutabile di tutte le conversazioni.

## 8.3 Retrieval contestuale delle memorie

8.3.1. Verificare recupero selettivo delle memorie pertinenti alla
query, al task, alla sessione e al profilo.

8.3.2. Verificare ranking e priorità tra memoria stabile, memoria
recente, contesto di sessione e fonti esterne.

8.3.3. Verificare filtri per pertinenza, recency, confidenza e
autorizzazioni.

8.3.4. Verificare che il sistema eviti di iniettare memoria irrilevante
o eccessiva nel prompt.

8.3.5. Verificare che informazioni ricordate non vengano presentate come
certe quando sono ipotesi o ricordi incerti.

8.3.6. Verificare spiegabilità del recupero: quando utile, il sistema
dovrebbe poter indicare quale memoria ha influenzato una risposta.

## 8.4 Profilazione adattiva

8.4.1. Verificare profilo dell'utente relativo a lavoro, competenze,
capacità, preferenze e stile di interazione.

8.4.2. Verificare uso del profilo per adattare livello di dettaglio,
spiegazioni e strumenti necessari.

8.4.3. Verificare che il profilo non sia usato per fare assunzioni
rigide o limitare indebitamente l'utente.

8.4.4. Verificare distinzione tra preferenze esplicite e inferenze del
sistema.

8.4.5. Verificare possibilità di correggere inferenze errate.

## 8.5 CAG e contesto aumentato

8.5.1. Verificare il meccanismo denominato CAG nel progetto e
documentare esattamente cosa significa nell'implementazione.

8.5.2. Verificare come vengono combinati profilo, memoria a lungo
termine, memoria recente, sessione, documenti e risultati dei tool.

8.5.3. Verificare limiti di contesto, priorità, deduplicazione e
compressione/sintesi delle informazioni recuperate.

8.5.4. Verificare che i dati personali e aziendali siano separati per
ambito e permessi.

8.5.5. Verificare test che dimostrino risposte contestualizzate migliori
rispetto alla stessa richiesta senza memoria pertinente.

------------------------------------------------------------------------

# 9. Apprendimento comportamentale e miglioramento continuo

## 9.1 Apprendimento dai feedback

9.1.1. Verificare acquisizione di correzioni esplicite, raccomandazioni
e feedback dell'utente.

9.1.2. Verificare riconoscimento di preferenze ricorrenti a partire da
più interazioni, ove previsto.

9.1.3. Verificare distinzione tra feedback sul singolo risultato e
preferenza generalizzabile.

9.1.4. Verificare che l'utente possa vedere, correggere o disattivare
una regola appresa.

9.1.5. Verificare che il sistema non apprenda automaticamente istruzioni
malevole contenute in pagine, email o documenti esterni.

## 9.2 Astrazione di regole e procedure

9.2.1. Verificare estrazione di una regola generale da un'esperienza
concreta.

9.2.2. Verificare indicazione del dominio di applicabilità, eccezioni e
condizioni di validità.

9.2.3. Verificare possibilità di convertire una regola in istruzione
comportamentale, checklist, workflow o skill eseguibile.

9.2.4. Verificare che una singola esperienza non generi automaticamente
una policy globale senza validazione appropriata.

9.2.5. Verificare che le regole possano essere versionate, disattivate e
sottoposte a rollback.

## 9.3 Valutazione dell'apprendimento

9.3.1. Verificare metriche per riduzione delle correzioni ripetute,
successo dei task e qualità percepita.

9.3.2. Verificare test prima/dopo l'applicazione di una regola.

9.3.3. Verificare che il sistema possa riconoscere quando una regola
appresa peggiora i risultati.

9.3.4. Verificare che la qualità non sia valutata soltanto dalla
soddisfazione immediata del modello stesso.

------------------------------------------------------------------------

# 10. Pianificazione gerarchica, obiettivi e vincoli

## 10.1 Obiettivi e piani di lungo termine

10.1.1. Verificare creazione e persistenza di obiettivi di lungo
termine.

10.1.2. Verificare descrizione dell'obiettivo, criteri di successo,
orizzonte temporale, priorità e stato.

10.1.3. Verificare piani principali e sottopiani collegati
gerarchicamente.

10.1.4. Verificare dipendenze, milestone, scadenze, risorse e
prerequisiti.

10.1.5. Verificare possibilità di modificare, sospendere, completare o
archiviare un piano.

## 10.2 Vincoli e preferenze strategiche

10.2.1. Verificare vincoli che vietano, richiedono o favoriscono
determinate soluzioni.

10.2.2. Verificare distinzione tra vincoli rigidi, preferenze morbide,
obiettivi e priorità.

10.2.3. Verificare rilevamento dei conflitti tra obiettivi e piani.

10.2.4. Verificare che i piani a breve termine vengano controllati
rispetto a obiettivi e vincoli superiori.

10.2.5. Verificare che il sistema spieghi i compromessi quando due
obiettivi entrano in conflitto.

10.2.6. Verificare che un vincolo importante non venga ignorato perché
non è presente nel contesto immediato della chat.

## 10.3 Pianificazione di task e attività

10.3.1. Verificare trasformazione di un obiettivo in task eseguibili.

10.3.2. Verificare assegnazione di priorità, scadenze, durata stimata,
dipendenze e responsabili.

10.3.3. Verificare collegamento tra task e piano di origine.

10.3.4. Verificare identificazione di task impossibili, bloccati,
irrealistici o privi di prerequisiti.

10.3.5. Verificare richieste di chiarimento quando mancano informazioni
critiche.

10.3.6. Verificare gestione di task ricorrenti e attività con finestre
temporali.

## 10.4 Ripianificazione dinamica

10.4.1. Verificare aggiornamento dei piani quando arrivano nuovi input o
cambiano le condizioni.

10.4.2. Verificare propagazione delle modifiche a sottopiani e task
dipendenti.

10.4.3. Verificare rivalutazione delle priorità quando cambiano risorse,
stipendio, budget, disponibilità o scadenze, se tali informazioni sono
disponibili e autorizzate.

10.4.4. Verificare rilevamento di task falliti per impossibilità reale,
non solo per errore tecnico.

10.4.5. Verificare ricerca di alternative, ricalcolo dei piani e
comunicazione delle conseguenze all'utente.

10.4.6. Verificare che la ripianificazione non cancelli silenziosamente
impegni o decisioni precedenti.

10.4.7. Verificare versioni/storico del piano, motivazione delle
modifiche e possibilità di ripristino.

10.4.8. Verificare prevenzione di oscillazioni o continui cambi di piano
dovuti a input minori o rumorosi.

## 10.5 Input e monitoraggio delle condizioni

10.5.1. Verificare input manuali forniti dall'utente.

10.5.2. Verificare raccolta automatica di eventi da fonti connesse, se
autorizzata.

10.5.3. Verificare aggiornamento di calendari, disponibilità, task e
altri segnali rilevanti.

10.5.4. Verificare provenienza, timestamp e confidenza degli input.

10.5.5. Verificare che input esterni non possano modificare obiettivi o
policy senza controlli.

## 10.6 Esecuzione e controllo del piano

10.6.1. Verificare collegamento tra piano e strumenti/skill che eseguono
i task.

10.6.2. Verificare monitoraggio dello stato: pianificato, pronto, in
esecuzione, bloccato, completato, fallito o annullato.

10.6.3. Verificare verifica del risultato prima di marcare un task come
completato.

10.6.4. Verificare gestione di retry, compensazioni e azioni
parzialmente completate.

10.6.5. Verificare notifiche e richieste di intervento umano quando
necessarie.

------------------------------------------------------------------------

# 11. Automazione personale e delega dei task

## 11.1 Assistente operativo

11.1.1. Verificare capacità di pianificare ed eseguire task personali
come ricerche, gestione email, ordini, biglietti, voli, alberghi e altre
attività dichiarate.

11.1.2. Verificare integrazioni reali per ogni categoria di attività;
non considerare implementata una categoria solo perché il modello può
descriverne i passaggi.

11.1.3. Verificare ricerca di informazioni aggiornate e confronto di
alternative.

11.1.4. Verificare raccolta delle preferenze e dei vincoli dell'utente
prima di eseguire azioni.

11.1.5. Verificare presentazione di riepilogo, costo totale, condizioni
e dettagli prima delle azioni irreversibili.

## 11.2 Email e comunicazioni

11.2.1. Verificare connessione autorizzata ai sistemi email supportati.

11.2.2. Verificare ricerca, lettura, classificazione, sintesi e
preparazione di bozze.

11.2.3. Verificare invio email solo con i permessi e le conferme
configurati.

11.2.4. Verificare gestione di destinatari, allegati, thread, errori e
invii duplicati.

11.2.5. Verificare che lo stile appreso sia applicato in modo
appropriato senza falsificare approvazioni o identità.

## 11.3 Acquisti, prenotazioni e pagamenti

11.3.1. Verificare integrazioni reali per ricerca, confronto e
prenotazione, se dichiarate.

11.3.2. Verificare separazione tra ricerca/preparazione e conferma
finale dell'acquisto.

11.3.3. Verificare autorizzazioni esplicite per pagamenti e operazioni
finanziarie.

11.3.4. Verificare conferma di prezzo, valuta, tasse, condizioni di
cancellazione, dati del destinatario e riepilogo ordine.

11.3.5. Verificare idempotenza e prevenzione di doppi acquisti in caso
di timeout o risposta ambigua.

11.3.6. Verificare che le credenziali di pagamento non siano esposte a
prompt, log o skill non autorizzate.

11.3.7. Verificare ricevute, conferme e gestione di fallimenti o
rimborsi.

## 11.4 Telefonate e interazione vocale

11.4.1. Verificare se e come l'applicazione effettua telefonate tramite
un servizio effettivamente integrato.

11.4.2. Verificare eventuale speech-to-text, text-to-speech, gestione
del consenso e registrazione.

11.4.3. Verificare conferma dell'identità del destinatario e dei
contenuti da comunicare.

11.4.4. Verificare gestione di chiamate non risposte, interruzioni e
risultati incerti.

## 11.5 Autonomia e limiti

11.5.1. Verificare livelli di autonomia configurabili per categoria di
azione.

11.5.2. Verificare conferma umana per azioni ad alto impatto o non
reversibili, secondo policy.

11.5.3. Verificare budget, rate limit, limiti di tempo e limiti al
numero di tentativi.

11.5.4. Verificare interruzione immediata e revoca dei permessi.

11.5.5. Verificare audit trail di decisioni, strumenti invocati,
approvazioni e risultato finale.

------------------------------------------------------------------------

# 12. Wiki Bridge: ingestione, indicizzazione e retrieval aziendale

## 12.1 Fonti documentali

12.1.1. Verificare ingestione di pagine web e wiki aziendali.

12.1.2. Verificare ingestione di DOC/DOCX e altri formati Office
effettivamente supportati.

12.1.3. Verificare ingestione di PDF, TXT, CSV, XML e altri formati
dichiarati.

12.1.4. Verificare ingestione di file e directory locali, con policy e
limiti chiari.

12.1.5. Verificare ingestione di formati aggiuntivi solo se esistono
parser reali e test.

12.1.6. Verificare estrazione di metadati: titolo, autore, data,
origine, versione, percorso e classificazione.

12.1.7. Verificare gestione di documenti vuoti, corrotti, protetti,
troppo grandi o non supportati.

## 12.2 Audio e video

12.2.1. Verificare ingestione di file audio registrati.

12.2.2. Verificare pipeline speech-to-text (STT) e conservazione di
timestamp/segmenti quando supportati.

12.2.3. Verificare ingestione video con estrazione della traccia audio e
successiva trascrizione.

12.2.4. Verificare gestione di lingue multiple, rumore, diarizzazione e
bassa confidenza, se supportati.

12.2.5. Verificare che la trascrizione conservi la provenienza dal file
originale e non venga confusa con una trascrizione verificata
manualmente.

12.2.6. Verificare aggiornamento e rimozione delle trascrizioni quando
la fonte viene aggiornata o cancellata.

## 12.3 Normalizzazione e catalogazione

12.3.1. Verificare estrazione del testo e normalizzazione coerente tra
formati.

12.3.2. Verificare segmentazione/chunking e conservazione dei
riferimenti alla fonte.

12.3.3. Verificare catalogazione, tassonomia, metadati e deduplicazione.

12.3.4. Verificare rilevamento di duplicati, versioni differenti e
documenti obsoleti.

12.3.5. Verificare aggiornamento incrementale senza reindicizzare
inutilmente tutto il corpus.

12.3.6. Verificare cancellazione propagata di contenuti e relativi
chunk, embedding, cache e indici derivati.

12.3.7. Verificare gestione dei documenti multilingue e ricerca
multilingue, se richiesta.

## 12.4 Indici e ricerca

12.4.1. Verificare indice lessicale, vettoriale o ibrido secondo
l'architettura effettiva.

12.4.2. Verificare embedding, versione del modello embedding e
compatibilità dell'indice.

12.4.3. Verificare retrieval, ranking, eventuale reranking e limiti dei
risultati.

12.4.4. Verificare filtri per fonte, data, tipo, versione,
classificazione e permessi.

12.4.5. Verificare citazioni a documento, pagina, sezione o intervallo
pertinente quando disponibile.

12.4.6. Verificare che la risposta segnali quando la documentazione
recuperata è insufficiente o contraddittoria.

12.4.7. Verificare benchmark di retrieval e risposte su un set di
domande aziendali rappresentative.

------------------------------------------------------------------------

# 13. Wiki Bridge: IAM, autorizzazioni e sicurezza dei dati

## 13.1 Integrazione IAM

13.1.1. Verificare collegamento ai sistemi IAM/identity provider
dichiarati.

13.1.2. Verificare mapping di utenti, gruppi, ruoli, attributi e
appartenenze.

13.1.3. Verificare sincronizzazione e aggiornamento delle modifiche di
ruolo.

13.1.4. Verificare comportamento quando l'IAM è indisponibile o
restituisce informazioni incomplete.

13.1.5. Verificare revoca tempestiva dell'accesso dopo la rimozione di
un permesso.

## 13.2 Autorizzazioni granulari

13.2.1. Verificare autorizzazioni a livello di fonte, documento, sezione
e chunk, secondo il modello implementato.

13.2.2. Verificare modello ad albero/gerarchico delle autorizzazioni, se
previsto.

13.2.3. Verificare che l'ereditarietà dei permessi sia definita, testata
e non ambigua.

13.2.4. Verificare filtraggio prima che i contenuti protetti siano
forniti al modello.

13.2.5. Verificare che l'agente operi con autorizzazioni non superiori a
quelle del dipendente/contesto, secondo la policy.

13.2.6. Verificare che l'agente non possa utilizzare altri agenti o
strumenti come canale per aggirare i permessi.

13.2.7. Verificare test negativi per accessi non autorizzati e tentativi
di prompt injection.

13.2.8. Verificare che riassunti, cache, memorie e risultati derivati
rispettino le autorizzazioni delle fonti originarie.

## 13.3 Protezione e audit

13.3.1. Verificare cifratura in transito e a riposo dove richiesta.

13.3.2. Verificare segregazione dei tenant e dei dati
personali/aziendali.

13.3.3. Verificare audit degli accessi a documenti e dati sensibili.

13.3.4. Verificare retention, cancellazione e gestione delle richieste
di rimozione.

13.3.5. Verificare protezione da prompt injection in documenti, email,
pagine web e trascrizioni.

13.3.6. Verificare che il contenuto dei documenti non possa modificare
le policy di sistema o concedere permessi.

13.3.7. Verificare che gli output non rivelino indirettamente
informazioni vietate tramite conferme, conteggi, riassunti o citazioni.

------------------------------------------------------------------------

# 14. Enterprise: repository centrale delle skill e delle capacità

## 14.1 Repository centrale

14.1.1. Verificare servizio centrale per registrare e distribuire skill
aziendali.

14.1.2. Verificare identità univoca, proprietario, versione,
descrizione, dipendenze, permessi e stato di approvazione di ogni skill.

14.1.3. Verificare pubblicazione, revisione, approvazione, deprecazione
e ritiro.

14.1.4. Verificare distribuzione delle skill agli agenti o ai gruppi
autorizzati.

14.1.5. Verificare sincronizzazione fra copia locale e repository
centrale.

14.1.6. Verificare conflitti di versione, aggiornamenti falliti e
rollback.

14.1.7. Verificare separazione tra skill private, condivise con un team
e globali aziendali.

## 14.2 Condivisione di apprendimento e comportamento

14.2.1. Verificare che regole comportamentali condivisibili possano
essere pubblicate separatamente dalle memorie personali.

14.2.2. Verificare policy che stabiliscano cosa può essere centralizzato
e cosa deve restare locale.

14.2.3. Verificare consenso, visibilità e possibilità di revoca per
dati/profili personali, secondo il modello aziendale.

14.2.4. Verificare che il repository non sincronizzi automaticamente
conversazioni private o dati sensibili.

14.2.5. Verificare revisione e test prima di applicare una regola
appresa a più dipendenti o agenti.

14.2.6. Verificare provenance: chi ha creato, corretto, approvato e
distribuito la capacità.

## 14.3 Continuità operativa

14.3.1. Verificare che task e skill approvati possano proseguire in
assenza temporanea del dipendente, entro i limiti autorizzati.

14.3.2. Verificare gestione di ferie, malattia, indisponibilità e cambio
di ruolo come stati distinti.

14.3.3. Verificare assegnazione a un agente sostitutivo o a un gruppo
autorizzato.

14.3.4. Verificare handover con stato del task, decisioni già prese,
fonti e prossime azioni.

14.3.5. Verificare che azioni che richiedono approvazione personale non
vengano eseguite fingendo che il dipendente le abbia approvate.

14.3.6. Verificare revoca e riassegnazione degli accessi quando un
dipendente lascia l'organizzazione.

14.3.7. Verificare report delle attività eseguite durante l'assenza e
degli interventi ancora necessari.

------------------------------------------------------------------------

# 15. Enterprise: comunicazione tra agenti e coordinamento dei progetti

## 15.1 Identità e comunicazione degli agenti

15.1.1. Verificare identità distinta degli agenti associati ai
dipendenti, team, progetti o funzioni.

15.1.2. Verificare canale di comunicazione autenticato tra agenti.

15.1.3. Verificare formato strutturato dei messaggi: obiettivo, contesto
necessario, stato, decisioni, dipendenze, richiesta e scadenza.

15.1.4. Verificare autorizzazioni e visibilità dei contenuti scambiati
tra agenti.

15.1.5. Verificare tracciabilità di mittente, destinatario, task,
versione del piano e decisioni.

15.1.6. Verificare gestione di messaggi duplicati, ritardi, fallimenti e
agenti non disponibili.

## 15.2 Coordinamento di piani condivisi

15.2.1. Verificare rappresentazione dei piani multi-team e delle
dipendenze tra progetti.

15.2.2. Verificare che una modifica rilevante in un piano possa
notificare gli agenti dei dipendenti coinvolti.

15.2.3. Verificare analisi dell'impatto e proposta di aggiornamento dei
piani correlati.

15.2.4. Verificare risoluzione di conflitti su risorse, priorità,
scadenze e vincoli.

15.2.5. Verificare che modifiche con impatto elevato siano sottoposte al
livello di approvazione previsto.

15.2.6. Verificare versionamento, commit/approvazione e rollback dei
piani coordinati.

15.2.7. Verificare prevenzione di loop di negoziazione tra agenti e
aggiornamenti concorrenti incompatibili.

15.2.8. Verificare che il sistema possa sostituire riunioni di
coordinamento solo per i casi in cui decisioni, autorizzazioni e
contesto siano sufficienti.

## 15.3 Stato del progetto e reporting

15.3.1. Verificare dashboard di stato, blocchi, dipendenze, rischi e
prossime azioni.

15.3.2. Verificare report delle decisioni prese dagli agenti e delle
ragioni.

15.3.3. Verificare evidenza dei punti che richiedono intervento umano.

15.3.4. Verificare notifiche mirate invece di notifiche eccessive o
ridondanti.

------------------------------------------------------------------------

# 16. Enterprise: governance, multi-tenancy e amministrazione

## 16.1 Organizzazioni e tenant

16.1.1. Verificare supporto a organizzazioni, tenant, team e progetti.

16.1.2. Verificare isolamento dei dati e delle configurazioni tra
tenant.

16.1.3. Verificare ruoli amministrativi, proprietari, revisori e utenti.

16.1.4. Verificare provisioning e deprovisioning di utenti e agenti.

## 16.2 Policy e governance

16.2.1. Verificare policy centrali per provider, modelli, strumenti,
skill e azioni consentite.

16.2.2. Verificare limiti per costo, consumo, concorrenza e uso dei
provider.

16.2.3. Verificare approvazione di skill e workflow ad alto rischio.

16.2.4. Verificare audit trail delle modifiche alle policy.

16.2.5. Verificare che policy centrali non possano essere aggirate da
configurazioni locali non autorizzate.

## 16.3 Audit e conformità operativa

16.3.1. Verificare log di accesso, esecuzione, approvazioni, modifiche
ai piani e pubblicazione delle skill.

16.3.2. Verificare ricerca ed esportazione degli audit per
amministratori autorizzati.

16.3.3. Verificare retention configurabile e protezione dall'alterazione
dei log.

16.3.4. Verificare minimizzazione dei dati registrati e redazione di
segreti.

16.3.5. Verificare strumenti di diagnosi senza esposizione impropria di
contenuti riservati.

------------------------------------------------------------------------

# 17. Sicurezza dell'esecuzione e gestione dei segreti

## 17.1 Segreti e credenziali

17.1.1. Verificare storage sicuro di API key, token, password e
credenziali.

17.1.2. Verificare che i segreti non siano inclusi in prompt, file di
log, stack trace o repository.

17.1.3. Verificare rotazione, revoca e scadenza delle credenziali.

17.1.4. Verificare separazione dei segreti personali, locali e
aziendali.

## 17.2 Esecuzione di codice e skill

17.2.1. Verificare sandbox o isolamento per script e codice generato.

17.2.2. Verificare limiti su filesystem, rete, processi, CPU, memoria e
durata.

17.2.3. Verificare allowlist/denylist o policy equivalenti per
operazioni pericolose.

17.2.4. Verificare che il codice generato non sia eseguito solo perché
prodotto dal modello.

17.2.5. Verificare gestione di dipendenze, pacchetti e supply-chain.

17.2.6. Verificare protezione da path traversal, command injection e
deserializzazione non sicura.

## 17.3 Prompt injection e input non attendibili

17.3.1. Verificare trattamento dei contenuti esterni come dati, non come
istruzioni di sistema.

17.3.2. Verificare difese per pagine web, documenti, email, MCP, plugin
e risultati dei tool.

17.3.3. Verificare che un contenuto esterno non possa autorizzare
pagamenti, inviare email o modificare policy.

17.3.4. Verificare separazione fra istruzioni, dati recuperati e output
degli strumenti.

## 17.4 Controllo umano e reversibilità

17.4.1. Verificare livelli di conferma configurabili per rischio.

17.4.2. Verificare riepilogo comprensibile prima delle azioni ad alto
impatto.

17.4.3. Verificare stop globale e cancellazione delle operazioni in
corso, quando tecnicamente possibile.

17.4.4. Verificare rollback, compensazione o procedura di recupero dove
possibile.

17.4.5. Verificare che il sistema non dichiari completata un'azione
senza evidenza del risultato.

------------------------------------------------------------------------

# 18. Affidabilità, osservabilità e recupero

## 18.1 Logging e tracing

18.1.1. Verificare log strutturati per sessioni, richieste, tool,
agenti, runtime e task.

18.1.2. Verificare correlazione tramite request ID/task ID e
tracciamento del flusso end-to-end.

18.1.3. Verificare metriche di latenza, errori, timeout, throughput,
consumo e successo dei task.

18.1.4. Verificare redazione di contenuti sensibili e segreti.

18.1.5. Verificare livelli di log configurabili e rotazione dei file.

## 18.2 Recupero e resilienza

18.2.1. Verificare ripresa dopo crash o riavvio.

18.2.2. Verificare persistenza dello stato dei task e dei piani.

18.2.3. Verificare gestione di operazioni incerte dopo un timeout.

18.2.4. Verificare retry idempotenti e prevenzione di effetti duplicati.

18.2.5. Verificare backup, restore e migrazioni dei dati.

18.2.6. Verificare comportamento in caso di perdita di rete, runtime non
disponibile o provider con rate limit.

## 18.3 Prestazioni end-to-end

18.3.1. Verificare tempi completi dei workflow, non solo token/s.

18.3.2. Verificare profili di carico con più agenti e tool
contemporanei.

18.3.3. Verificare limiti di memoria, crescita di cache e leak di
risorse.

18.3.4. Verificare performance con sessioni lunghe, memoria estesa e
grandi corpora documentali.

------------------------------------------------------------------------

# 19. Test e qualità

## 19.1 Test unitari e integrazione

19.1.1. Verificare test unitari per moduli core, adapter, memoria,
planner e policy.

19.1.2. Verificare test d'integrazione tra UI, orchestratore, runtime e
persistenza.

19.1.3. Verificare test end-to-end per workflow rappresentativi.

19.1.4. Verificare test di regressione per bug già risolti e skill
migliorate.

19.1.5. Verificare test su Windows reale o ambiente equivalente
documentato.

## 19.2 Test di comportamento

19.2.1. Verificare benchmark di uso delle memorie pertinenti.

19.2.2. Verificare benchmark di generazione e riutilizzo di skill.

19.2.3. Verificare test di ripianificazione dopo cambiamenti di
condizioni.

19.2.4. Verificare test di selezione di colleghi e collaborazione
multi-agente.

19.2.5. Verificare test di retrieval documentale e autorizzazioni.

19.2.6. Verificare test di robustezza rispetto a prompt injection e
input avversari.

## 19.3 Criteri di completamento

19.3.1. Ogni funzione dichiarata completa deve avere criteri di
accettazione osservabili.

19.3.2. Ogni funzione critica deve avere almeno un test positivo e test
negativi rilevanti.

19.3.3. Le funzionalità esterne non testabili devono essere marcate come
bloccate/non verificate, non complete.

19.3.4. I benchmark devono includere hardware, versioni, configurazione,
dataset e metodo di misurazione.

19.3.5. I gap devono essere associati a un owner/modulo, priorità,
dipendenze e test di chiusura.

------------------------------------------------------------------------

# 20. Matrice finale di priorità consigliata

Questa priorità è iniziale e deve essere aggiornata dopo l'ispezione del
repository.

## P0 --- Fondamenta e rischi critici

20.1. Architettura modulare e runtime adapter reali.

20.2. Sicurezza di segreti, tool, codice generato e autorizzazioni.

20.3. Verifica del risultato prima di dichiarare completati task
esterni.

20.4. Integrità delle autorizzazioni di Wiki Bridge e isolamento dei
dati.

20.5. Persistenza affidabile dello stato di task e piani.

20.6. Test end-to-end del percorso principale di inferenza.

## P1 --- Differenziatori centrali del prodotto

20.7. Selezione dei colleghi AI e profili comportamentali reali.

20.8. Memoria a lungo termine, memoria recente e retrieval contestuale.

20.9. Pianificazione gerarchica, vincoli e ripianificazione.

20.10. Generazione di skill ibride, test, approvazione e rollback.

20.11. Wiki Bridge: ingestione, retrieval, citazioni e controlli di
accesso.

20.12. Logging e tracciabilità dei task e delle decisioni.

## P2 --- Capacità avanzate e Enterprise

20.13. Repository centrale di skill e sincronizzazione.

20.14. Condivisione controllata di regole e capacità apprese.

20.15. Comunicazione tra agenti e coordinamento di piani condivisi.

20.16. IAM, multi-tenancy, governance e audit aziendale.

20.17. Continuità operativa durante assenze e passaggi di ruolo.

20.18. Ingestione avanzata di audio/video e sincronizzazione
documentale.

## P3 --- Ottimizzazione e maturità di prodotto

20.19. Benchmark comparativi estesi e telemetria prestazionale.

20.20. Miglioramenti UI, accessibilità e gestione di grandi sessioni.

20.21. Ottimizzazione automatica di runtime, memoria e decoding, se
supportata.

20.22. Reportistica avanzata e strumenti amministrativi.

------------------------------------------------------------------------

# 21. Formato obbligatorio della Feature Coverage Matrix

Per ogni requisito numerato compilare una riga con questi campi:

  -----------------------------------------------------------------------
  Campo                               Contenuto
  ----------------------------------- -----------------------------------
  ID                                  Numero esatto del requisito, ad
                                      esempio 10.4.3

  Feature                             Nome breve

  Stato                               IMPLEMENTATA E VERIFICATA /
                                      IMPLEMENTATA, DA VERIFICARE /
                                      PARZIALE / UI-MOCK SOLTANTO /
                                      ASSENTE / BLOCCATA-ESTERNA / NON
                                      APPLICABILE

  Evidenza codice                     File, classe/funzione e breve
                                      descrizione

  Evidenza test                       Test e risultato oppure "nessun
                                      test trovato"

  Verifica manuale                    Passaggi riproducibili e risultato

  Gap                                 Che cosa manca o non è dimostrato

  Priorità                            P0/P1/P2/P3

  Dipendenze                          Moduli, provider, credenziali o
                                      servizi necessari

  Azione minima                       Patch o attività concreta per
                                      chiudere il gap

  Acceptance criteria                 Condizione osservabile per
                                      dichiararlo completato
  -----------------------------------------------------------------------

Non ridurre più requisiti a una sola riga quando richiedono prove
diverse.

------------------------------------------------------------------------

# 22. Sequenza di lavoro consigliata per Cloud Codex

22.1. **Fase A --- Discovery:** identificare stack, entry point,
architettura, directory, database, servizi, test e documentazione.

22.2. **Fase B --- Mappatura:** associare ogni requisito numerato ai
componenti e alle evidenze esistenti.

22.3. **Fase C --- Audit:** compilare la Feature Coverage Matrix senza
effettuare refactor non richiesti.

22.4. **Fase D --- Verifiche mirate:** eseguire test esistenti e
aggiungere test minimi per le funzionalità critiche prive di prova,
senza alterare il comportamento di produzione.

22.5. **Fase E --- Gap analysis:** ordinare i gap per rischio, impatto
sul prodotto, dipendenze e costo di chiusura.

22.6. **Fase F --- Piano incrementale:** proporre una sequenza di patch
piccole, ognuna con test e criterio di accettazione.

22.7. **Fase G --- Implementazione controllata:** dopo l'approvazione
del piano, completare un gruppo coerente di gap per volta, eseguire i
test e aggiornare la matrice.

22.8. **Fase H --- Verifica finale:** eseguire regressione, verificare
la documentazione e riportare separatamente ciò che è completato,
parziale, bloccato o non verificato.

------------------------------------------------------------------------

# 23. Formato del report conclusivo

Cloud Codex deve terminare l'audit con:

23.1. **Executive summary:** stato reale del prodotto in poche righe.

23.2. **Coverage:** percentuale e conteggio dei requisiti per ciascuno
stato. Non conteggiare requisiti non verificati come completati.

23.3. **Top gap:** i 10 gap più importanti, con impatto e prova.

23.4. **Rischi critici:** sicurezza, permessi, perdita dati, azioni
esterne e affidabilità.

23.5. **Funzioni già solide:** componenti da non riscrivere e
motivazione basata su evidenze.

23.6. **Piano di chiusura:** ordine raccomandato, dipendenze, effort
relativo e test.

23.7. **Benchmark da eseguire:** in particolare inferenza, velocità, uso
memoria, decoding e workflow completi.

23.8. **Dipendenze esterne:** credenziali, servizi, hardware o policy
che impediscono la verifica completa.

23.9. **Decisioni richieste al proprietario del prodotto:** soltanto
quelle che non possono essere dedotte in sicurezza dal codice o dalla
documentazione.

23.10. **Nessuna falsa certezza:** distinguere chiaramente le
funzionalità verificate da quelle soltanto dichiarate o progettate.

------------------------------------------------------------------------

## Nota finale

Questo documento descrive l'ambito funzionale atteso sulla base della
visione di prodotto disponibile. **Non afferma che tutte queste funzioni
siano già implementate.** L'obiettivo dell'audit è precisamente
stabilire la verità sullo stato del repository, preservare ciò che è già
solido e chiudere i gap con il minimo intervento necessario.
