# L'harness per agenti: manuale d'uso

[English](harness-manual.md) · *Italiano*

Questa è la guida per **usare** l'agente. [docs/harness.md](harness.md) (in inglese) è il riferimento: ogni opzione, rotta e regola, e
cosa non è fatto. Il motore non ha bisogno di un manuale e qui non ne ha.

- [1. Cos'è](#1-cosè)
- [2. Prima di cominciare](#2-prima-di-cominciare)
- [3. La prima conversazione](#3-la-prima-conversazione)
- [4. Scegliere cosa può fare](#4-scegliere-cosa-può-fare)
- [5. I comandi che iniziano con /](#5-i-comandi-che-iniziano-con-)
- [6. Mentre lavora](#6-mentre-lavora)
- [7. Controllare e disfare](#7-controllare-e-disfare)
- [8. La memoria](#8-la-memoria)
- [9. Regole, contesto e documenti di riferimento](#9-regole-contesto-e-documenti-di-riferimento)
- [10. Skill, plugin e server MCP](#10-skill-plugin-e-server-mcp)
- [11. Il web, il browser, il Portafoglio e il telefono](#11-il-web-il-browser-il-portafoglio-e-il-telefono)
- [12. I piani](#12-i-piani)
- [13. La scheda Agente](#13-la-scheda-agente)
- [14. La riga di comando e l'editor](#14-la-riga-di-comando-e-leditor)
- [15. Gli hook](#15-gli-hook)
- [16. Routine, valutazione e riesecuzione](#16-routine-valutazione-e-riesecuzione)
- [17. Skill personali](#17-skill-personali)
- [18. Quando qualcosa non va](#18-quando-qualcosa-non-va)
- [19. Cosa non fa](#19-cosa-non-fa)

## 1. Cos'è

L'harness è un agente: gli dici cosa vuoi, e lui legge i file, li modifica, esegue comandi, cerca sul web e ti racconta cosa ha
fatto. Usa il modello caricato in Basalt, quindi tutto resta sul tuo PC, a meno che tu scelga un provider cloud per una funzione. È un
processo Python separato che parla col server tramite la sua API pubblica.

Quattro modi di usarlo, tutti con le stesse capacità:

| Modo | Usalo quando |
|---|---|
| Scheda **Chat**, *Con strumenti* | vuoi una conversazione: chiedere, correggere, riprendere domani. Il modo principale. |
| Scheda **Agente** | vuoi un lavoro in una cartella che indichi, con un registro e una via per tornare indietro. |
| `python -m harness` / `basalt-agent` | lo vuoi in uno script, con codici di uscita e JSON. |
| `python -m harness acp` | lo vuoi come agente di un editor che parla l'Agent Client Protocol. |

## 2. Prima di cominciare

1. **Serve un modello caricato.** Se non c'è, un avviso sotto la barra in alto lo dice e apre *Modelli*; caricane uno lì.
2. **Python e i pacchetti** sono quelli del `.venv` del repository (vedi il README, *Requisiti*). L'app avvia il server per te; i
   processi dell'agente li avvia il server.
3. **Dove sta tutto.** Nella cartella dati dell'utente (`%APPDATA%\BasaltPannello\`): conversazioni, memoria (`memory\`), registro di
   audit, checkpoint, approvazioni, il tuo `hooks.json`. Mai nel tuo progetto. L'unica cosa che l'agente scrive in un progetto è
   `.basalt\artifacts`.
4. **Tieni privata la chiave API.** Se il server ha un `--api-key`, le pagine e l'agente la usano; non aprire mai il server oltre
   127.0.0.1 senza una.

## 3. La prima conversazione

1. Apri la scheda **Chat**. Sotto *Modalità* scegli **Con strumenti**. I tre tipi sono:
   - *Con strumenti*: l'agente legge e modifica i file della sua cartella, cerca, esegue comandi e tiene i tuoi piani. La conversazione è salvata.
   - *Solo domanda*: risponde senza strumenti. Salvata anch'essa.
   - *Solo il modello*: una chat semplice col modello caricato, che resta in questo browser; niente strumenti, niente cartella.
2. Guarda l'etichetta a destra di **Rami**, nella riga in alto: due righe, *Maiusc + TAB per cambiare modalità:* in grigio e, sotto, in
   grassetto e a colori, cosa può fare l'agente senza chiedere (*Chiedi prima* all'inizio). Sezione 4.
3. Scrivi nel campo e premi **Invio** (**Maiusc+Invio** va a capo). L'agente lavora in sottofondo e la pagina mostra, in ordine, ciò che
   ha scritto, ciò che ha pensato e ogni strumento usato, col risultato. **Ferma** interrompe il turno.
4. Quando gli servi, chiede: un comando da eseguire, un piano da approvare, una scelta. La risposta è un pulsante; puoi anche scrivere.
5. Torna più tardi: la colonna a sinistra elenca le tue conversazioni (sotto). Una conversazione inattiva da 30 minuti lascia andare il
   suo processo e si risveglia dalla cronologia salvata quando scrivi di nuovo.

**La colonna delle conversazioni.** A sinistra della Chat, dalla più recente: clicca su una per aprirla. La stella accanto la rende una
**preferita**: le preferite sono elencate a parte, sopra le altre. La croce la elimina: premila una volta e chiede *Elimina?*, premi ancora
(entro pochi secondi) per eliminarla per sempre, con i suoi messaggi, i file allegati e quelli prodotti, e la sua copia della cartella se
l'aveva (i rami fatti da lei restano). La freccia in cima alla colonna (**‹**) la chiude in una striscia stretta e **›** la riapre; la
scelta viene ricordata. La colonna non c'è in *Solo il modello*, la cui unica conversazione resta nel browser. La riga in alto ha, a sinistra,
**Nuova conversazione**, **Rami** e l'etichetta della modalità, e a destra il tipo di conversazione (*Modalità*), *Ragionamento* e
*Impostazioni*.

**In quale cartella lavora?** La scheda Chat non manda nessuna cartella, quindi una conversazione della Chat lavora in `chat-workspace`
dentro la cartella dati dell'utente. I suoi strumenti per i file restano dentro quella cartella. Per lavorare su un tuo progetto usa la
scheda **Agente** o la riga di comando (sezioni 13 e 14), oppure allega i file alla conversazione: trascinali sul campo o usa il
pulsante degli allegati. Si accettano testo, codice, CSV, JSON, Word, Excel, PowerPoint, PDF, immagini, audio e video; un file lungo ne
mostra l'inizio e l'agente legge il resto a pezzi.

**File che produce.** Quando l'agente produce un file per te, la pagina lo controlla (il JSON si legge, le righe del CSV hanno le stesse
colonne, i file Office si aprono, un'immagine si apre) e solo allora gli dà un pulsante *Salva*.

**L'ora.** All'agente arrivano da sole, prima di ogni richiesta, la tua data, ora e fuso orario locali: non devi dirglieli tu, e lui non
deve chiederli. Ogni messaggio della conversazione, chiamate agli strumenti e note comprese, ha a sinistra il suo orario: l'ora in
grassetto, la data sotto; passandoci sopra col puntatore vedi la data completa e il fuso orario. I messaggi salvati prima di questa
funzione non hanno l'orario.

**Livello di ragionamento.** L'elenco *Ragionamento* stabilisce per quanto tempo ragiona il modello in questa conversazione. `/effort`
fa lo stesso, e l'elenco lo segue.

## 4. Scegliere cosa può fare

Quattro modalità, dalla più prudente alla più libera:

| Modalità | Cosa può fare l'agente |
|---|---|
| **Solo un piano** | Leggere. Non può cambiare né eseguire nulla; finisce presentando un piano, che approvi o no. |
| **Sola lettura** | Leggere. Chiede prima di tutto il resto. |
| **Chiedi prima** | Modificare i file nella sua cartella; chiede prima di eseguire un comando o di fare qualcosa che non si può disfare. L'inizio. |
| **Automatico** | Esegue comandi senza chiedere. Usalo solo in una cartella che puoi perdere. |

**Cambiarla mentre lavori.** Premi **Maiusc+Tab** nel campo del messaggio: la modalità passa alla successiva (Chiedi prima, Solo un
piano, Sola lettura, Automatico, e di nuovo) e l'etichetta la mostra, con il modo di un colore suo: grigio per *Sola lettura*, viola per *Chiedi prima*, verde per *Solo un
piano*, rosso per *Automatico*. Premere l'etichetta fa lo stesso. Con una conversazione aperta cambia solo quella, dalla sua prossima azione; senza conversazione cambia la
modalità con cui partirà la prossima. Passare ad **Automatico** la prima volta mostra un'etichetta di rischio evidenziata e chiede due volte (*Ho
capito il rischio*, poi *Ho letto con attenzione il messaggio precedente e ho capito il rischio*); una volta detto sì non lo chiede più
finché non chiudi l'app, in nessuna conversazione. Se rifiuti, il giro lo salta.

**Regole per i comandi.** In *Chat → Impostazioni → Cosa può fare l'agente senza chiedere* (per le nuove conversazioni) scrivi una regola per riga, come `shell_command:python -m pytest*`. Un comando che corrisponde si
esegue senza chiedere. Una regola di **divieto** vince sempre. Una regola non copre mai un comando che ne concatena o reindirizza un
altro (`pytest && rm -rf ~` non è `pytest`). In una domanda, il pulsante **Permetti sempre …** ne aggiunge una per te.

**La regola dei due.** Anche in *Automatico*, un'azione irreversibile chiede di nuovo quando la conversazione ha letto contenuti
esterni (una pagina web, uno strumento MCP, una telefonata, un appunto richiamato) *e* può raggiungere un segreto o una voce del vault.
Si controlla a ogni azione.

**Cosa le modalità non sono.** Non sono una sandbox del sistema operativo. Un comando gira con i tuoi diritti sui tuoi file e sulla tua
rete. I comandi dell'agente hanno un ambiente ripulito, nessun input da tastiera, un limite di tempo, output limitato e, su Windows, un
job object (tetti di memoria e di processi; tutto ciò che ha avviato finisce con lui). Se una cartella ti sta a cuore, usa *Chiedi
prima*, oppure tienine una copia.

## 5. I comandi che iniziano con /

Scrivi `/` all'inizio di un messaggio e si apre un menu. **Su/Giù** scelgono, **Tab** completa, **Invio** esegue un comando che non
vuole argomenti, **Esc** chiude. Un messaggio che inizia con uno spazio parte così com'è; un comando sconosciuto resta nel campo con
una nota.

| Comando | Cosa fa |
|---|---|
| `/help` (`/?`) | elenca i comandi |
| `/clear` (`/new`, `/reset`) | una conversazione nuova e vuota |
| `/effort [none\|low\|medium\|high\|auto]` | per quanto ragiona il modello; cambia anche l'elenco *Ragionamento* |
| `/mode [plan\|read\|ask\|auto]` | la modalità (Automatico chiede le sue due conferme) |
| `/plan [cosa]` | modalità piano, poi la tua richiesta |
| `/permissions` (`/allowed`) | la modalità, le regole e i rifiuti; apre anche le impostazioni |
| `/settings` (`/config`) | le impostazioni della Chat |
| `/model` | dice quale modello è caricato (si cambia in *Modelli*) |
| `/status` | modalità, ragionamento, turni, uso del contesto, strumenti, appunti, server MCP |
| `/context` | quanto è pieno il contesto |
| `/compact` | trasforma la metà più vecchia della conversazione in un riassunto; solo quando nulla è in corso |
| `/tools`, `/hooks`, `/mcp` | cosa può chiamare la conversazione, quali hook sono attivi, quali server MCP sono partiti |
| `/memory [parole]` | gli ultimi dieci appunti, oppure una ricerca |
| `/copy [n]` | copia l'ultima risposta (o l'ennesima dalla fine) |
| `/export` | salva la conversazione come file di testo |
| `/rename nome` | la rinomina |
| `/branch` (`/fork`), `/branches` | copia la conversazione in un punto; mostra l'albero |
| `/resume` (`/sessions`) | apre una conversazione precedente |
| `/rewind` (`/undo`, `/checkpoint`) | indica *Torna qui* su una richiesta |
| `/stop` | ferma il turno |
| `/diff` (`/changes`) | le modifiche nella copia della cartella della conversazione |
| `/plans` (`/tasks`), `/wallet` | aprono quelle viste |
| `/init` | chiede all'agente di scrivere un `AGENTS.md` per la cartella |
| `/review`, `/security-review` | gli chiede di rivedere le modifiche nella cartella |
| `/research domanda` (`/search`) | gli chiede di leggere le fonti vere e di dire quali pagine non ha potuto aprire |
| `/nome testo` | ogni skill trovata su questo PC (prima serve l'approvazione) |

Lasciati fuori di proposito: i comandi dell'account, del terminale o del cloud di Claude Code (`/login`, `/usage`, `/theme`, `/vim`,
`/ide`…).

## 6. Mentre lavora

Non devi aspettare. Un messaggio inviato mentre l'agente lavora viene guardato prima della sua prossima chiamata al modello, e va in
uno di tre modi:

- **Indirizzato** (il predefinito): cambia o aggiunge qualcosa al compito, quindi entra nel compito e l'agente prosegue con la nuova
  istruzione.
- **In coda**: una richiesta diversa che può aspettare («quando hai finito», «più tardi»). Parte dopo il compito. Due pulsanti
  permettono di *Farlo ora* o di *Toglierlo*.
- **Urgente** («urgente», «asap», «prima di tutto»): il compito viene messo da parte dov'è, la richiesta urgente si fa in una
  conversazione a parte, e il primo compito riprende dopo.

**Ferma vuol dire ferma**: termina il compito, un comando in corso e tutto ciò che ha avviato, e scarta ciò che aspettava.

**Se qualcosa si guasta.** Un server occupato o irraggiungibile si riprova (dopo 2, 5, 15, 30, 60 secondi). Un processo che muore
riparte dalla sua cronologia, fino a tre volte in dieci minuti. Uno strumento irreversibile non viene mai rieseguito da un riavvio: la
conversazione ti dice che potrebbe essere già partito. Dopo di che, o quando il server rifiuta definitivamente, la conversazione è
**bloccata** e aspetta te: *Riprova*, chiudila, oppure scrivi.

## 7. Controllare e disfare

- **Modifiche.** Il *Monitor* elenca ogni modifica dell'agente, blocco per blocco, con *Tieni* e *Annulla* (premi due volte). Un blocco
  si annulla solo finché il file è esattamente quello che ha scritto l'agente, così il tuo lavoro non viene mai sovrascritto. Anche le
  modifiche fatte da un comando di shell sono elencate.
- **Torna qui.** Ogni tua richiesta ha questo pulsante (premi due volte). La conversazione torna a subito dopo quella richiesta e una
  copia di ciò che veniva dopo viene conservata. I file non si toccano: annullali dal pannello delle modifiche. Le azioni che restano
  avvenute (un comando, una telefonata, un pagamento, un'azione del browser) sono elencate prima, e devi premere ancora per tornare
  indietro lo stesso.
- **Checkpoint.** Prima della prima modifica di ogni turno la cartella viene salvata fuori dal progetto. `python -m harness.rewind
  --workspace D:\proj` li elenca e `--to ID` ne ripristina uno; le conversazioni hanno `/rewind` e *Torna qui*. Il ritorno fa prima uno
  snapshot di sicurezza, quindi si può a sua volta annullare. Non disfa un comando che l'agente ha eseguito, e lo dice.
- **Rami.** `/branch` copia una conversazione a una richiesta; *Rami* mostra l'albero. *Esegui entrambi e confronta* manda una richiesta lungo due
  continuazioni, una dopo l'altra, e mostra le due risposte e i file cambiati da ciascuna, affiancati. *Messaggi come nodi* elenca i tratti di
  messaggi tra una biforcazione e l'altra, mostra prima di cambiare quanti token il modello dovrebbe rileggere (cache fredda e calda) e
  *Unisci e sintetizza* scrive, a richiesta, una sola sintesi di due rami in una nuova conversazione.
- **Una copia tutta sua.** In una cartella git una conversazione può lavorare in un suo worktree. La pagina Chat non lo offre (non ha una cartella di progetto da copiare); esiste per le conversazioni su una cartella git,
  come un ramo di una che ha già una copia. Ciò che cambia resta lì; guardi il diff di ogni file e applichi quelli che scegli, come modifiche **in staging**, mai
  come commit. Scartare la copia toglie la copia e il suo ramo.
- **Il registro di audit** ha una riga per ogni chiamata di strumento: lo strumento, un hash degli argomenti, un'anteprima mascherata,
  la decisione, l'esito e l'ora. Si vede se è stato manomesso (catena di hash), ma non lo impedisce.

## 8. La memoria

L'agente può tenere appunti brevi su un progetto e rileggerli nelle conversazioni successive.

- **Digli cosa tenere.** Scrive un appunto solo quando decide di farlo, quindi chiedilo: *«ricorda che la build usa --arch=native»*.
  Tiene un appunto di al massimo 2.000 caratteri con una categoria; lo stesso testo si tiene una volta sola; c'è posto per 5.000
  appunti per progetto.
- **Cosa torna da solo.** A ogni messaggio che scrivi, gli appunti che hanno abbastanza in comune con le tue parole vengono aggiunti
  dopo il tuo messaggio, come dati: una parola condivisa vale fino a 1 punto (meno se molti appunti la hanno), una specifica come
  `sm_120` o `e-mail` fino a 2, le parole comuni (il, di, the, and) non contano mai, e un appunto ha bisogno di 1,5 punti (o di tutte le
  parole di una richiesta di tre o meno). Si aggiungono al più quattro appunti e 600 token. *Impostazioni → Memorie → Note di progetto*
  cambia i punti, il numero e il limite di token, ciascuno con la sua spiegazione. L'impostazione è *Iniezione → Recupero in coda* nelle impostazioni della Chat (la predefinita); con le
  altre due scelte non si aggiunge nulla e l'agente chiama `recall` quando vuole.
- **Vedere e cercare.** `/memory` mostra gli ultimi dieci appunti (ciascuno tagliato a 300 caratteri), `/memory parole` cerca, `/status`
  li conta.
- **Toglierne uno.** Chiedi all'agente di dimenticarlo (usa l'id dell'appunto) oppure, senza conversazioni aperte, cancella il file
  `memory\<16 cifre esadecimali>.db` nella cartella dati dell'utente (e i file `-wal` e `-shm` accanto, se ci sono) per dimenticare
  tutto il progetto.
- **Quali appunti stanno insieme.** Un file per cartella di progetto (maiuscole ignorate). Tutte le conversazioni della **Chat**
  lavorano nella stessa cartella, quindi condividono una sola memoria; ogni cartella della scheda Agente o della riga di comando ha la
  sua. Una conversazione che lavora in una copia git tutta sua (un work tree) usa la memoria del repository da cui è nata. Non c'è
  esportazione e nulla è condiviso tra PC.
- **Cosa non è.** La ricerca è per parole, non per significato: «costruire» trova «costruiremo» ma «costruito» no, e un appunto che condivide
  parole specifiche con il tuo messaggio può tornare anche se non c'entra. Gli appunti sono conservati come scritti, in chiaro, non cifrati; non
  chiedergli di ricordare una password o una chiave. Un appunto che è stato indotto a tenere da una pagina letta tornerà, come dato che
  ha l'ordine di non eseguire; leggi `/memory` ogni tanto.
- **Permessi.** In *Chiedi prima* e *Automatico* scrive e cancella appunti senza chiedere; in *Sola lettura* chiede prima di
  scrivere; in *Solo un piano* può solo richiamare.
- **Spenta.** `--no-memory` sulla riga di comando toglie gli strumenti e il recupero.

## 8a. Le tue memorie

Oltre alle note su un progetto, l'assistente può tenere due memorie su **di te**, in *Impostazioni > Memorie*. **Nulla viene imparato o usato finché non lo accendi lì.**

- **Memoria a lungo termine:** ciò che cambia lentamente. Sezioni: chi sei, dove vivi, persone, aziende, partner economici, carattere, gusti, interessi, valori,
  come pensi, i fondamenti del tuo pensiero, emozioni, e una breve scheda che ti riassume. Una nota non scade.
- **Memoria a breve termine:** ciò che è successo da poco. Sezioni: fatti, ricerche, argomenti, progetti (lo stato di ciò che costruisci nella scheda Agente). Le
  note restano finché non le cancelli, ma nelle risposte entrano solo quelle viste negli ultimi 14 giorni (il numero si cambia). Per usare le più vecchie chiedi
  all'assistente di guardare più indietro: allora cerca in tutte (`recall_about_user`).
- **Imparare** (un interruttore per ogni memoria). Ciò che scrivi resta in una coda cifrata e, quando la GPU non ha avuto nulla da fare per qualche secondo, una
  chiamata separata al modello decide cosa vale la pena tenere e in quale sezione. Legge solo i tuoi messaggi e le ricerche fatte per te, mai una risposta, una pagina,
  un file o il risultato di uno strumento, e non tocca mai una nota scritta o confermata da te. Le note che sembrano un segreto (numeri di carta, password, chiavi) o un
  ordine a un assistente vengono rifiutate. Una nota solo intuita è segnata *un'intuizione* con quanto è sicura; il pulsante *È giusto* la fa contare del tutto. Una
  routine non viene mai imparata.
- **Usare** (un altro interruttore per ogni memoria). A ogni messaggio entrano solo le note che hanno parole in comune con esso, dopo il tuo messaggio e come dati, entro
  un piccolo budget; la stessa nota non entra due volte in una conversazione. Ogni sezione ha un **peso**: spento, basso, normale, alto o altissimo. Basso mette una nota e
  solo con una corrispondenza chiara; normale fino a due; alto fino a quattro e le prime due della sezione all'inizio di una conversazione; altissimo fino a otto e sei
  all'inizio. Ciò che l'assistente ha solo intuito (carattere, valori, come pensi, emozioni, scheda) parte da *basso*.
- **Dove sta.** Solo su questo PC, cifrata con la stessa chiave del dispositivo della cassaforte del Portafoglio (Windows DPAPI la protegge per il tuo utente: un
  programma che gira come te può comunque chiederla a Windows). Un telefono non vede né modifica le memorie, anche se le sue conversazioni le usano. Quando una
  conversazione è diretta a un fornitore che non è il modello di questo PC, le note restano fuori dalla richiesta, salvo che tu lo permetta nel riquadro rosso, che chiede due volte.
- **Leggere, correggere, cancellare.** La stessa pagina elenca le note (con ricerca e filtro per sezione), le modifica, le conferma e le cancella una per una, o tutte
  quelle di una memoria con due pressioni, e mostra cosa è stato imparato di recente e quanti messaggi aspettano una GPU libera.

## 8b. Dove sei (app Android)

Nelle Impostazioni dell'app, **Invia la mia posizione con i messaggi** è spento all'inizio. Accenderlo chiede ad Android il permesso di leggere la posizione mentre l'app è aperta. Con l'interruttore acceso, ogni
messaggio dice al PC dove si trova il telefono (via, numero, città quando il telefono riesce a ricavarli, altrimenti le coordinate). L'assistente non la legge a ogni messaggio: la chiede solo quando il compito
dipende dal luogo, per esempio i ristoranti vicino a te o un'emergenza in cui qualcuno deve sapere dove sei. Resta solo nella memoria del PC, per al massimo un giorno, non viene scritta nella conversazione né nelle
tue memorie, e si cancella quando spegni l'interruttore o riavvii il server. Se una conversazione è diretta a un fornitore che non è il modello di questo PC, la posizione non gli viene data. L'assistente non chiama
nessuno al posto tuo: in un'emergenza ti dà l'indirizzo e il numero da chiamare.

## 9. Regole, contesto e documenti di riferimento

- **`AGENTS.md`** (altrimenti `CLAUDE.md`, altrimenti `GEMINI.md`), cercato dalla cartella in su fino alla radice del repository, fa
  parte di ogni prompt. `/init` chiede all'agente di scriverne uno.
- **Conversazioni lunghe.** I vecchi risultati degli strumenti sono sostituiti da un segnaposto breve solo quando il contesto si
  avvicina al suo limite, dal più vecchio, con il testo completo salvato e nominato; gli errori non si tagliano mai. `/context` mostra
  l'uso; `/compact` riassume su richiesta la metà più vecchia. Le impostazioni della Chat scelgono la politica per un contesto pieno:
  *Ferma al limite*, *Finestra scorrevole* o *Compatta*.
- **Lettura dei file e output lunghi.** `read_file` restituisce ciò che si chiede: il file intero, oppure le righe da `offset` a
  `offset + limit - 1`; un file che non sta in una risposta finisce con una riga che dice le righe mostrate, il totale e l'offset con cui
  proseguire. I risultati rumorosi (output di comandi, ricerche) sono ridotti alle prime e ultime righe e a tutte le righe di errore,
  con il numero di righe nascoste e il testo completo salvato e nominato; una riga lunghissima è tagliata nel mezzo con il numero di
  caratteri nascosti. Un file letto apposta, una skill e un risultato salvato riletto non sono mai accorciati.
- **Schemi di contesto.** Le impostazioni della Chat hanno sei interruttori (iniezione, riscrittura, potatura, riassunto, rami,
  versioni), ognuno con la descrizione di cosa fa, vantaggi, svantaggi e cosa fa alla cache dell'inizio del server. Lasciali come
  sono finché non hai un motivo.
- **Documenti di riferimento.** *Impostazioni → Memorie → Documenti di riferimento* (o `--reference DIR` sulla riga di comando) offre i
  file .md e .txt di una cartella come blocco fisso (veloce dopo il primo turno, ma riempie il contesto) o cercati (contesto piccolo, i
  passaggi arrivano come dati non fidati); `auto` sceglie in base alla dimensione. Valgono per le conversazioni che iniziano dopo il salvataggio.
- **La wiki.** *Impostazioni → Memorie → Wiki (wikibridge)* attiva `wiki_search`, `wiki_page` e `wiki_context` nella Chat e nell'Agente, con
  l'indirizzo del servizio (predefinito `http://127.0.0.1:8775`) e un progetto; un passo di piano che interroga la wiki li usa.
  *Verifica la connessione* dice l'indirizzo provato e, se non ha risposto, perché. Una chiave, se il servizio la chiede, è `BASALT_WIKIBRIDGE_KEY`.
- **Cache.** L'inizio del prompt resta identico byte per byte da un turno all'altro, così il server riusa la sua cache. Per questo gli
  appunti e i riassunti dei piani stanno dopo il tuo messaggio, mai nel blocco di sistema.

## 10. Skill, plugin e server MCP

- **Da dove vengono.** Basalt legge, e non esegue mai, le skill, i plugin e i server MCP installati per altri strumenti su questo PC
  (`~/.claude/skills`, il `.claude/skills` del progetto, i plugin, `.mcp.json` e simili).
- **Niente è attivo finché non lo approvi.** In *Impostazioni → Capacità* apri una voce, leggi il suo testo (una skill) o il suo
  comando (un server) e approva. L'approvazione è legata a ciò che hai letto; se il file cambia, torna *da approvare*.
- **Le tue cartelle di skill.** In *Impostazioni → Capacità → Cartelle di skill* (e con `--skills DIR` da riga di comando) indichi
  cartelle di skill `SKILL.md`. Le loro skill compaiono lì come *da approvare*; `load_skill` carica una skill approvata e rifiuta una
  nuova o modificata con un messaggio che lo dice. Non resta mai in attesa di una risposta.
- **Usarle.** L'agente cerca (`find_capability`), carica una skill approvata (`load_capability`) o chiama uno strumento di un server
  approvato (`call_capability`, che chiede come ogni azione irreversibile; l'agente può leggere gli argomenti di uno strumento prima di
  chiamarlo). Se un server si ferma per chiederti dei valori, la domanda compare nella conversazione con il nome del server: viene dal
  server, quindi non scrivere password o chiavi. Dalla Chat puoi chiamare una skill con `/nome testo`.
- **Manca qualcosa?** L'agente può cercare online un server MCP o una skill (`find_online_capability`), mostrarti i cinque migliori con
  un punteggio, le avvertenze e il comando che verrebbe eseguito, e installare quello che scegli. All'installazione non si esegue
  nulla; il punteggio misura popolarità e manutenzione, **non la sicurezza**. Le impostazioni sono in *Impostazioni → Capacità →
  Cerca online*. Attivare «installa da solo il migliore» mostra un'etichetta di rischio e chiede due volte.
- **Server MCP remoti** che richiedono un accesso mostrano un pulsante **Accedi** (OAuth); il token resta nel gestore credenziali di
  Windows.

## 11. Il web, il browser, il Portafoglio e il telefono

- **Cercare e leggere.** `search_web` usa DuckDuckGo salvo un altro motore che imposti, senza chiave; le parole che cerchi vanno a
  DuckDuckGo. `browse_url` legge una pagina come testo e rifiuta gli indirizzi che non sono su internet (questo PC, la tua rete) salvo
  che tu li abbia permessi nelle impostazioni del browser. Tutto ciò che legge è segnato come non fidato. Dopo una fonte di informazione
  in una risposta (un link, o un indirizzo scritto nel testo) un piccolo pulsante **fonte ↗** apre quella pagina nel browser predefinito
  del sistema; un passo di ricerca o di lettura di una pagina elenca allo stesso modo i siti che ha usato.
- **Il browser.** Con gli strumenti del browser l'agente guida Edge o Chrome per compilare moduli, prenotare e comprare. Se le
  conversazioni hanno un browser, se mostra una finestra, se può aprire pagine di questo PC e se conserva gli accessi si decide nella
  vista **Portafoglio**; ogni scelta rischiosa ha un'etichetta evidenziata e due domande.
- **Segreti.** L'agente scrive `{{secret:NOME}}` dove serve una credenziale e non la vede mai; il valore viene inserito all'ultimo
  momento e mascherato in ciò che torna. **Il vault** (Portafoglio) tiene cifrati carte, dati personali e accessi; l'agente scrive
  `{{vault:VOCE.CAMPO}}`, e il valore va solo verso i siti che la voce indica.
- **Il secondo fattore** di ogni accesso lo scegli tu: nessuno, un codice digitato ogni volta (la conversazione si ferma e chiede),
  oppure il segreto TOTP, così Basalt fa il codice da sé. L'ultimo permette l'accesso a chiunque possa usare Basalt su questo PC; te
  lo dice una volta.
- **Pagamenti.** Il limite lo imposti tu, a qualunque importo; finché non lo imposti, ogni pagamento ti chiede. Una **rinuncia** lascia
  che l'agente paghi da solo fino a un massimale per pagamento e a uno cumulativo finché non scade, dopo una dichiarazione dettata e due
  finestre con il tuo nome riscritto; oltre i massimali chiede di nuovo. Un pagamento si può fermare dal Portafoglio in ogni momento, e
  da allora i campi della carta non vengono più rilasciati. Ciò che poi addebita il modulo di un commerciante è limitato dal limite
  della carta stessa, non da Basalt.
- **Telefonate.** Sono spente di default e, una volta accese, ti chiedono **ogni volta**, anche in *Automatico*. La chiamata si apre
  dicendo che un assistente IA telefona per tuo conto. Nessuna telefonata vera è stata fatta in un test.
- **Da cosa non protegge.** L'agente gira come il tuo stesso utente. Un programma che gira come te può raggiungere più di quanto il
  vault possa nascondere; i comandi dell'agente non possono nominare il vault, i file dei pagamenti o gli accessi del browser, ma è
  una staccionata per un modello fuorviato, non un muro. Per una separazione vera, fai girare l'agente come un altro utente Windows.

## 12. I piani

La vista **Piani** mostra i piani come grafi: azioni, sensori (aspettano un evento), decisioni, riunioni e compiti passati a te. Un
passo si può spostare, riprovare (con un limite e un'attesa) e far tornare indietro; tornare indietro su azioni che restano avvenute
le elenca prima.

Le parti della vista, da sinistra: una **colonna di piani** (come quella delle conversazioni della Chat, si chiude con la freccia) dove ogni
piano ha tre pulsanti, *Apri*, *Duplica* ed *Elimina* (l'eliminazione chiede una seconda pressione e cancella per sempre; archiviare invece
lo conserva), e a destra il piano aperto in quattro schede: *Obiettivi, date e importi*, *Dettagli del piano e archivio*, *Valuta gli eventi
in arrivo* e *Definizione del piano*. Nella definizione scegli **Grafo** o **Elenco**. Nel grafo trascini i passi e tiri un collegamento dal
capo rotondo di un passo a un altro (il passo di arrivo aspetta quello di partenza); un clic su un collegamento lo sceglie, la croce o il
tasto Canc lo toglie; le frecce e *Posizione automatica* del pannello del passo spostano il riquadro sul grafo. Nell'elenco ogni riga ha le
frecce su e giù: due passi che non si aspettano si scambiano, due passi in catena semplice si scambiano e i collegamenti vengono ridisegnati,
altrimenti la mossa è rifiutata con il motivo. Il pannello di un passo ha anche le caselle *Aspetta* per decidere da tastiera cosa
aspetta.

- Nella Chat l'agente può leggere i piani e, **col tuo sì**, crearli e cambiarli. Il sì è o una citazione delle tue parole nel tuo
  ultimo messaggio, o una domanda fatta con la modifica vera (il titolo, i passi e il diff reali) a cui rispondi con un pulsante. Un
  piano va letto prima di essere cambiato, e un piano andato avanti nel frattempo viene rifiutato finché non lo si rilegge.
- `ask_user` chiude un turno con una domanda e dei pulsanti; ciò che premi o scrivi è il tuo messaggio successivo.
- **Eventi dal mondo** si possono dare ai piani (`POST /api/plans/events`) o leggere da feed RSS, da una casella di posta (solo le
  intestazioni) o da un bot Telegram. La pompa degli eventi e il filtro nel cloud sono **spenti** finché non li accendi in
  *Impostazioni*; il filtro nel cloud richiede anche il tuo consenso e una chiave, sostituisce i nomi e non manda dati sanitari se
  non li spunti.
- Nulla in un piano cambia alle tue spalle: il planner prepara solo proposte, che approvi tu.

## 12a. File nelle risposte

Quando l'agente crea un file per te, o ne indica uno nella tua cartella, la risposta contiene un **pulsante con il nome del file**; premilo e il file viene salvato dove scegli
(la finestra *Salva con nome* nell'app desktop e nella maggior parte dei browser). Anche un file che ha scritto o modificato ha un pulsante *Salva* nel suo passo. Si possono
salvare così solo file dentro la cartella della conversazione, mai `.basalt` o `.git` dell'agente.

## 12b. Immagini e grafici nelle risposte

*Chat → Impostazioni → Immagini e grafici* ha due interruttori, ciascuno con il suo livello di presenza (molto bassa, bassa, normale, alta, molto alta, altissima).
Valgono per la prossima nuova conversazione. **Immagini**: l'agente le cerca in rete, di preferenza nell'articolo che cita, ne tiene una nella conversazione e la mostra
con la fonte; la pagina mostra solo immagini che l'agente ha tenuto, mai una da un indirizzo scritto nel testo. **Grafici**: l'agente disegna flussi di lavoro,
deployment e interazioni come diagrammi che la pagina disegna; *Sorgente del diagramma* mostra il testo da cui è nato.

## 12c. Telefoni

*Impostazioni > Telefoni* prepara l'app Basalt per un telefono. 1) **Tailscale** su questo PC: la pagina lo installa (con winget), lo collega e mostra l'indirizzo che questo PC ha nella tua rete privata. 2) **Lascia
collegare i telefoni**: il server ascolta anche su quell'indirizzo, solo per Tailscale, mai su internet. 3) **Una chiave per ogni telefono**: dai un nome al telefono, premi *Crea una chiave* e inquadra il
codice QR con l'app (o copia la chiave). La chiave resta su questo PC, cifrata, quindi *Mostra il codice* nell'elenco ne rimostra il QR (per esempio dopo aver reinstallato l'app); il telefono tiene le sue impostazioni negli aggiornamenti, e il backup di Android le ripristina dopo una disinstallazione e una reinstallazione se il backup del telefono è attivo. Per fermare un telefono, togli la sua chiave. Sul telefono installa Tailscale dal Play Store e accedi con lo stesso account. Un telefono può
usare il modello, le conversazioni, i file e le routine; non raggiunge il portafoglio, la cassaforte, le impostazioni né il caricamento dei modelli.

L'app Android (`mobile/`, vedi il suo README) richiede poi solo l'indirizzo e la chiave: inquadra il codice QR nelle sue Impostazioni. Il suo pulsante **+** offre *Immagine*, *Foto* e *Documenti/File* (*Locale*, *Server*: le cartelle aggiunte
sul PC, e *Rete*: altri dispositivi, via SMB); il microfono scrive ciò che dici e le risposte si possono far leggere ad alta voce, con la voce e il riconoscimento del telefono.

## 13. La scheda Agente

La scheda Agente è la pagina delle conversazioni che lavora in una **cartella tua**. A sinistra la colonna elenca le cartelle che hai
aggiunto, ciascuna con le sue conversazioni. Premi **Aggiungi cartella**, scrivi il percorso completo (o *Sfoglia...* dentro l'app) e la
cartella è aggiunta: l'agente lavora lì, i suoi strumenti per i file restano dentro, e la cartella riceve una cartella `.basalt` con il
registro delle sue conversazioni. Una croce accanto a una cartella la toglie dall'elenco senza toccarne i file né le conversazioni; un **+**
avvia una nuova conversazione al suo interno.

Poi parlaci come nella Chat: un passo alla volta, in quanti messaggi e giorni vuoi, senza limite di passi. **Maiusc + Tab** cambia in
qualsiasi momento quanto può fare da solo (*Solo piano*, *Sola lettura*, *Chiedi prima*, *Automatica*, come nella sezione 4): prima il
piano, poi digli di procedere. Il pulsante *Modifiche* apre l'elenco dei file che ha modificato, con *Tieni* e *Annulla* per ogni blocco. I
comandi che esegue non sono limitati alla cartella; un sandbox per loro non c'è ancora.

## 14. La riga di comando e l'editor

```powershell
python -m harness -p "correggi il test che fallisce in calc.py" --workspace D:\work\proj --json
basalt-agent -p "correggi il test che fallisce" --workspace D:\work\proj      # basalt-agent.cmd: da qualunque cartella
basalt-agent acp --workspace D:\work\proj                                      # per un editor che parla ACP su stdio
```

Opzioni comuni (tutte sono in [harness.md](harness.md#running-the-agent)):

| Opzione | Significato |
|---|---|
| `--permission-mode plan\|read_only\|default\|auto` | come le modalità della sezione 4 |
| `--allow PATTERN`, `--deny PATTERN` | regole per i comandi; un divieto vince |
| `--interactive` o `--ask-server` | chiede sul terminale, o sulle pagine del server, prima di azioni rischiose, pagamenti e piani |
| `--max-turns`, `--timeout-seconds`, `--max-tokens-budget` | si ferma dopo tanto |
| `--tool-timeout [NOME=]SECONDI` | limite di tempo per strumento; 0 per nessuno |
| `--secret-env NOME[=host,host]` | lascia che l'agente scriva `{{secret:NOME}}` |
| `--capabilities`, `--lsp`, `--browser`, `--phone`, `--wiki`, `--council` | accendono quei gruppi di strumenti |
| `--hooks FILE`, `--workspace-hooks` | i tuoi hook |
| `--no-memory`, `--no-checkpoints` | li spengono |

Un'esecuzione senza nessuno a cui chiedere rifiuta ciò che richiede una domanda (aggiungi `--interactive`). Non può digitare un
codice di secondo fattore, quindi un accesso che lo richiede lì fallisce. **Codici di uscita**: 0 finito, 1 errore, 2 uso sbagliato,
3 limite di turni, 4 limite di budget, 5 bloccato in un ciclo, 6 finito su una domanda per te, 7 messo da parte prima di finire, 130
annullato. Con `--json` il risultato ha il motivo dell'arresto, le modifiche, i conteggi di token e cache, il numero di chiamate
rifiutate e il percorso del registro di audit.

In un editor l'agente segue ACP versione 1: messaggi, pensieri, chiamate di strumenti e modifiche come diff, e richieste di permesso
che l'editor ti pone. La modalità piano chiede all'editor di approvare il piano, con un pulsante per ogni modalità in cui può
proseguire.

## 15. Gli hook

Gli hook girano su eventi e possono bloccare una chiamata di strumento. Mettili in `hooks.json` nella cartella dati dell'utente (o nel
file dato con `--hooks`); il `.basalt\hooks.json` di un progetto si legge solo con `--workspace-hooks`, perché una cartella in lavorazione
non deve nominare comandi da sola.

```json
{"hooks": [
  {"event": "pre_tool", "tool": ["write_file", "edit_file"], "match": {"path": "*.env"}, "action": "block", "reason": "i file env sono off limits"},
  {"event": "pre_tool", "tool": "shell_command", "command": "python check_command.py", "timeout": 10},
  {"event": "turn_end", "command": "python notify.py"}
]}
```

Eventi: `pre_tool`, `post_tool`, `turn_end`, `session_start`, `session_end`. Un comando `pre_tool` lascia passare la chiamata con codice
di uscita 0 e la blocca con codice 2 (ciò che ha stampato è il motivo). Un hook che non riesce a decidere (un altro codice, un tempo
scaduto, non autorizzato a girare) blocca anch'esso. Un file con un errore impedisce all'agente di partire, dicendo il motivo.
`/hooks` mostra cosa è attivo.

## 16. Routine, valutazione e riesecuzione

- Le **routine** eseguono un prompt secondo un orario (`every 30m`, `daily 08:30`, `weekdays 08:30`, `mon,wed,fri 18:15`) in una cartella
  e in una modalità. Una routine nasce **disattivata** e in sola lettura; ne gira una alla volta e solo quando nessuna conversazione è
  occupata. Si gestiscono dalla scheda **Cron Jobs**: un elenco con *Accendi/Spegni*, *Esegui ora* ed *Elimina*, un editor, gli ultimi risultati di ogni job e una
  piccola chat a destra per discuterne uno. La chat propone soltanto: *Usa nell'editor* riempie il modulo, e nulla è salvato o acceso finché non premi *Salva* e *Accendi*.
- **Valutazione**: `python -m harness.eval --set core --k 5 --gpu-power-limit-w 450 --out report.md` esegue ogni compito k volte su una
  cartella nuova e riporta pass@k, pass^k, costo e uso della cache (un `--k` sotto 5 viene segnalato). Sono confronti di sviluppo, non numeri da titolo. I benchmark pubblici
  (BFCL, sottoinsiemi di Terminal-Bench e SWE-bench Pro) si leggono da una cartella che hai scaricato tu: `--bfcl DIR`, `--terminal-bench DIR`, `--swebench DIR`.
- **Riesecuzione**: `python -m harness.replay trace.jsonl --mode auto` rifà una sessione registrata con le risposte registrate del modello
  e riporta il primo punto in cui le chiamate agli strumenti divergono. Prova l'harness, non il modello. La stessa riesecuzione,
  l'esportazione JSONL mascherata di una conversazione e il controllo del registro di audit sono nella pagina LOGS, sezione *Tracce delle sessioni*.

## 17. Skill personali

Una skill personale è ciò che hai fatto in una conversazione, conservato come uno script Python che puoi rieseguire con altri valori. Quasi tutto
è codice semplice, quindi non costa token, è veloce e dà ogni volta lo stesso risultato; il modello si usa solo dove bisogna capire il linguaggio.

**Farne una**
1. Fai il compito in una conversazione con strumenti. Puoi segnare le parti che la prossima volta cambieranno tra due `#` con uno spazio dentro,
   come in `trova le notizie # di oggi # riguardanti # la guerra in Ucraina #`. Una parte segnata può essere di più parole, e `# città: Roma #` le
   dà anche un nome. Il modello riceve la richiesta senza i segni, e la conversazione mostra ciò che hai scritto.
2. Premi **Trasforma in skill** sotto una richiesta. Accanto a ogni richiesta precedente compare una casella: spunta quelle che fanno parte del
   compito (quelle che si sono susseguite da vicino sono già spuntate). La skill finisce con la risposta alla richiesta di cui hai premuto il
   bottone, quindi puoi premerlo anche su una precedente. Oppure scrivi `/start-learn-skill` prima del compito e `/stop-learn-skill` dopo, e le
   richieste in mezzo sono scelte.
3. **Cosa cambia ogni volta.** La pagina propone dei parametri, trovati dai tuoi `#`, dai valori che una chiamata ha preso dalle tue parole (un
   nome di file, una città, un numero) e dalle date, e battezzati dal modello. Tieni o scarta ciascuno, rinominalo, dagli un tipo (testo, numero,
   data, data e ora, scelta, lista di numeri, di testi o di entrambi, file o cartella, sì o no) e un valore abituale, oppure aggiungine un altro. Una
   data può essere scritta a parole (ieri, adesso, tra 3 giorni) e allora viene calcolata ogni volta che la skill gira.
4. **Lo script.** Lo scrive il modello una volta sola, da ciò che è stato fatto e dai parametri che hai tenuto. Lo leggi, lo cambi se vuoi, premi
   *Verifica* e poi *Salva la skill*. Finché non lo fai non si salva nulla.

**Usarla.** Il bottone **Skill** (dopo Rami) le elenca; `/skilluse nome` apre il modulo di una (`/skilluse nome città=Roma` compila un valore). Il
modulo chiede ogni valore con la sua spiegazione e il suo tipo: una data ha un selettore e si può scrivere a parole, con sotto ciò che diventerà
(*→ 2026-10-25*); una lista prende un valore per riga, oppure separati da `;` o da una virgola. La skill gira **nella conversazione aperta**: le
sue chiamate agli strumenti compaiono come quelle dell'agente, seguono gli stessi permessi e le stesse domande e restano nella cronologia;
**Ferma** la interrompe.

**L'elenco.** *Esegui*, *Modifica* (la definizione, i parametri, lo script e le versioni precedenti), *Esporta* (un solo file `.pskill`, da tenere o
da copiare su un altro PC con una chiavetta o per e-mail), *Elimina* (premi due volte) e *Importa skill personali…*. Una skill che arriva da un
file, o i cui file sono stati cambiati a mano, è **da approvare**: leggi lo script e approvalo prima che possa girare. Una che hai fatto tu è
approvata.

**Accesso.** *Ristretto* (il solito): lo script usa solo `tool(...)`, `ai(...)`, `ask(...)`, `say(...)` e Python semplice (testo, JSON, date,
liste…), mai file, programmi o rete direttamente, quindi tutto ciò che fa passa dai permessi, dagli hook e dal registro di audit dell'agente.
*Libero*: lo script può usare tutto ciò che Python sa fare, con i tuoi diritti, senza quei controlli; si salva solo dopo l'etichetta di rischio e
le sue due domande. Il controllo su uno script ristretto è una staccionata contro gli errori, non un muro contro chi vuole fare danno: leggi lo
script di una skill che viene da altri.

**A un orario:** nella scheda **Cron Jobs** un job può eseguire una delle tue skill invece di un compito scritto da te. Compili i valori una volta; una data in parole come *oggi*
o *domani* viene ricalcolata a ogni esecuzione. La modalità e le regole del job decidono cosa la skill può fare senza chiedere.

**Non ancora fatto:** una prova che confronta con ciò che era stato registrato e il riprendere con l'agente dal passo che è fallito.

## 18. Quando qualcosa non va

| Cosa vedi | Perché | Cosa fare |
|---|---|---|
| Un avviso che non c'è un modello caricato | il server non ha nulla in memoria | *Modelli*, scegline uno, *Carica* |
| La conversazione è *bloccata* | il server ha rifiutato definitivamente, o il processo continuava a fallire | leggi la nota, poi *Riprova* o chiudila |
| «Nobody can confirm in this run» | un'esecuzione senza interfaccia ha incontrato qualcosa che richiede una domanda | aggiungi `--interactive`, o una regola `--allow` |
| Un comando è stato rifiutato anche se una regola lo permette | il comando ne concatena o reindirizza un altro | un solo comando semplice per chiamata |
| Uno strumento si è fermato dopo 15 minuti | il limite di tempo predefinito | `--tool-timeout NOME=SECONDI` |
| La ricerca dice che serve un controllo | DuckDuckGo limita le ricerche automatiche | aspetta, imposta un altro motore, o `BASALT_WEB_SEARCH=off` |
| Un comando con la barra resta nel campo | il nome è sconosciuto | `/help`; inizia con uno spazio per inviarlo come testo |
| «La skill … non è ancora approvata» | la skill è *da approvare* | *Impostazioni → Capacità* |
| Una pagina è stata rifiutata | è su questo PC o sulla tua rete | permettila nelle impostazioni del browser, se vuoi |
| Ogni pagamento ti chiede | il limite è 0 finché non lo imposti | la vista Portafoglio |
| `/compact` dice di aspettare | l'agente sta lavorando | aspetta la fine del turno, o Ferma |
| Ha dimenticato ciò che gli hai detto | non ha tenuto un appunto, o la cartella è diversa | vedi la sezione 8 |
| Un riavvio ha interrotto uno strumento irreversibile | potrebbe essere già partito | controlla tu il risultato, poi prosegui |

## 19. Cosa non fa

- Non isola file e rete per i comandi: *Automatico* va in una cartella che puoi perdere.
- Basalt è solo un **client** A2A; non riceve compiti da altri agenti. L'agente ACP non ha `terminal/*` e non è stato provato in un editor vero; ha invece `session/load`, `authenticate` e `fs/*`.
- Non legge i conti bancari tramite un aggregatore, e i pagamenti con Stripe e Google Pay non sono stati provati su servizi reali.
- Audio e video richiedono il servizio callAPIcall; senza, il file viene conservato e l'agente lo dice.
- Nulla qui misura quale strategia di contesto sia la migliore; serve il modello vero.

L'elenco completo, con i motivi, è in fondo a [harness.md](harness.md).
