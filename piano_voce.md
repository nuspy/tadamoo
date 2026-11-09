# Piano: le funzioni vocali di callAPIcall dentro TaDaMoo

Stato: da approvare. Branch di lavoro: `feature/voice` (da `master`, fad848be). Nessun codice scritto. Decisioni prese con l'utente l'8 novembre 2026.
Fonte: lettura di `C:\Projects\callAPIcall` (213 commit, branch StreamingTTS, con modifiche non committate), `C:\Projects\Personalities`, `C:\Projects\Handy-API`.

## 1. Cosa si fa e cosa no

**Si porta**: STT e TTS con gli stessi motori, dettatura e appunti avanzati, telefonate (Android Bluetooth e modem GSM), avatar animato e creazione avatar,
chat e codice in voce, personalità con memorie, RAG, copilota, meeting e videochiamate, analisi di registrazioni con diarizzazione, le API di callAPIcall,
il gateway vocale per il telefono, il bot Telegram verso l'agente (sempre in ascolto) e il terminale ESP32 (terzo cliente del gateway, hardware facoltativo).
Si importano le sei personalità e gli avatar esistenti e i valori del bot Telegram che Hermes usa oggi, così il bot è usabile subito.

**Non si porta**: il Control Center Tauri (lo sostituiscono i tab di TaDaMoo), Handy-API (già rimpiazzato in callAPIcall), le skill di Hermes e gli script WSL,
l'app Capacitor (la sostituisce l'app Flutter), la pubblicazione `myai.tadamoo.com` senza password, i trasporti sperimentali A7670E/EC25, `config.json` di callAPIcall
(non si copia nel repository di TaDaMoo: contiene valori veri del vecchio Telegram Wrapper). Il bot Telegram è quello che Hermes usa oggi: i valori vivono nel suo `.env`
(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL`), non in callAPIcall. Si leggono dal disco al momento dell'importazione, sul PC dell'utente: il token va nel gestore
credenziali di Windows, mai in un file del repository; gli utenti autorizzati e il canale principale vanno nelle impostazioni. Si cambiano anche da Settings > Voice.
**Fuori da questo piano, in tabella di marcia**: il Telegram Wrapper (client TDLib sull'account dell'utente, con videochiamate Telegram), oggi non funzionante e da rivedere.

## 2. Decisioni di fondo

- **Servizio vocale**: un processo Python proprio, con il suo ambiente (3.10) e ambienti separati per i motori pesanti, come in callAPIcall. Lo possiede l'app C#:
  parte all'apertura, prima del modello, nella sessione dell'utente, fuori dal gruppo di processi del server, e sopravvive ai riavvii del server (cambio modello, codice 75).
- **Windows e Linux da subito**. Su Linux comandi globali solo con X11. Le parti dipendenti dal sistema stanno dietro interfacce di piattaforma; senza una macchina Linux qui
  restano "scritte, non provate". Windows soltanto: telefonia GSM/HFP, camera virtuale DirectShow, cattura audio WASAPI, `SetWindowDisplayAffinity`.
- **Chi risponde**: due cervelli per l'avatar, entrambi da costruire. *Diretto*: il motore caricato, senza strumenti, con la personalità nel prompt più memoria e RAG
  di quella personalità, per risposte rapide. *Harness*: l'agente di TaDaMoo (permessi, regola dei due, strumenti) al posto di Hermes. Si passa dall'uno all'altro con un
  interruttore nei tab Voice e Avatar, con scorciatoie separate e un comando a voce. Il livello di autorizzazione del cervello harness si imposta in Settings > Voice come in Chat
  e resta tra le sessioni.
- **Personalità**: campi di callAPIcall (nome, voce, avatar, personalità, prompt, conoscenza) con memoria lunga e breve nel sistema cifrato che TaDaMoo ha già per le memorie
  personali, una per personalità. Creazione in Settings > Personalità; Chat e Codice le scelgono da un menu.
- **RAG**: tab di primo livello. Archivi di conoscenza separati, collegabili a una o più personalità. Ricerca ibrida: testuale sempre, più embedding con un piccolo modello
  multilingue su CPU gestito da TaDaMoo.
- **API**: stesse rotte e stesso token di callAPIcall, ripubblicate dal server di TaDaMoo (`/external/*`, `/analysis/*`, STT, TTS, sessione, personalità, avatar, meeting).
  Mai sotto `/v1`, che resta solo OpenAI e Anthropic (un test lo impone).
- **Voce in uscita per chat e codice**: niente codice, comandi o uscite di strumenti; una frase a voce dice cosa è stato fatto (costruita dai fatti dell'agente). Il testo completo
  resta a schermo.

## 3. Memoria video: ordine di caricamento e cambi di servizi

Oggi il Loader legge la VRAM libera dalla scheda (NVML) e riempie con la cache degli esperti quello che resta meno la riserva. Non esiste una riserva con nome né un conto dei processi
di supporto. Il piano usa proprio questo: le funzioni si caricano **prima**, e il Loader vede già lo spazio ridotto.

1. **Avvio dell'app**: parte il servizio vocale. STT, dettatura e appunti sono attivi subito, anche senza modello.
2. **Caricamento (tasto Load)**: se ci sono funzioni vocali impostate e non caricate, il servizio le carica con modelli, quantizzazioni e collocazione scelti (VRAM o RAM). Si misura la
   VRAM rimasta e il Loader pianifica l'LLM in quello spazio, lasciando libero il valore impostato nel Loader. Il Loader mostra la VRAM per funzione (stimata, poi misurata e salvata).
3. **Aggiungere funzioni a motore avviato** (tasto "Funzioni vocali" > dialog): dopo conferma si scarica l'LLM, si caricano le nuove funzioni, si ricarica l'LLM nello spazio rimasto.
4. **Togliere funzioni a motore avviato**: le funzioni si scaricano subito e liberano VRAM, ma **l'LLM non viene toccato**. La VRAM liberata resta libera finché l'utente non preme
   "Cambia servizi in memoria"; compare un dialog di conferma che dice cosa si scarica, quanta VRAM si recupera e che il modello viene ricaricato con tutto lo spazio disponibile
   meno la riserva del Loader. Solo dopo la conferma il modello si ricarica.
5. **Cambio di modello**: riavvia il server (codice 75) ma non il servizio vocale, che resta caricato.
- **Selezione per modello, per ogni servizio** (STT, TTS, avatar, embedding, diarizzazione, upscaling), come in callAPIcall: motore, modello o dimensione, peso mostrato, dove
  caricarlo (VRAM o RAM, con la GPU se ce n'è più di una). Si configura nel Loader (tab "Servizi vocali") e in Settings > Voice; le due viste scrivono gli stessi valori.
- Limite noto: su Windows il driver non dice la memoria per processo; il conto per funzione è la stima dichiarata e poi quella misurata dal servizio prima e dopo il caricamento.

### Modelli: solo su richiesta esplicita

- **Nessun modello viene scaricato da solo**, neanche Whisper: pesano gigabyte. Chi sceglie un modello non presente vede **"Scarica e installa"** accanto alla scelta, con peso, spazio
  su disco, licenza e collocazione prevista. Senza il modello, la funzione resta spenta e dice cosa manca (dettatura e STT "attivi dall'avvio" valgono dopo il primo download).
- **Si riusa il sistema di download che c'è già** (`serve/downloads.py`, `serve/catalog.py`): piano prima (file alla revisione fissata, dimensioni e SHA-256 dichiarati da Hugging Face,
  disco libero, accesso), poi si parte solo con la conferma e con la presa d'atto della licenza; file `.part` ripresi con richiesta di intervallo, rinominati solo a SHA-256 corretto;
  il token di Hugging Face non viene mai scritto né registrato. Si estende alle sorgenti che non sono su Hugging Face (rilasci su GitHub, modelli di diarizzazione) con indirizzo e SHA-256 fissati.
- **Dove si installano**: nella cartella dei modelli di Basalt, in una sottocartella dedicata (`<cartella modelli>/voice/<servizio>/<modello>`), quindi dentro la stessa struttura. La cartella è
  **impostabile in Settings > Voice**; il valore predefinito è quella già configurata per i modelli. Cambiarla non sposta niente da solo: i modelli già scaricati si vedono dove sono.
- **Licenze**: i modelli Apache 2.0 e MIT si scaricano con la presa d'atto normale. XTTS v2 chiede in più di confermare "uso non commerciale". Le altre licenze mostrano il loro testo prima del download.
- Il servizio vocale e il Loader leggono la stessa cartella; il Loader mostra un modello vocale come "da scaricare" quando manca.

## 4. Struttura nel repository

- `voice/` (nuovo pacchetto Python del servizio): `engines/` (STT e TTS con catalogo unico, come `catalog.py`), `avatar/`, `telephony/`, `meetings/`, `copilot/`, `desktop/` (dettatura,
  appunti, comandi globali, overlay, con interfacce Windows e X11), `personas/`, `rag/`, `gateway/`, `api/`. Gli ambienti pesanti in `tts-venvs/` e `avatar/venv` creati dall'installatore.
- Lato server: `serve/voice_api.py` (rotte, proxy verso il servizio con il token), `serve/voice_loader.py` (ordine di caricamento, riserva, misure), pagine `serve/web/voice*.js`,
  `rag.js`, `personas.js`, tab del Loader. Lato app C#: gestione del processo del servizio, voci di tray, dialog.
- Dati utente in `%APPDATA%\TaDaMoo\voice\`: personalità, avatar, archivi RAG, voci clonate, impostazioni. Modelli scaricati su richiesta esplicita (vedi §3) e mai impacchettati.

## 5. Interfaccia

- **Voice** (tab): interruttore Diretto / Harness, avatar, STT, TTS, dettatura e appunti, telefonate, meeting, copilota, accelerazione. Settings > Voice: motori e pesi per servizio,
  collocazione VRAM/RAM, livello di autorizzazione dell'agente, regole di telefonia (approvazione manuale o automatica, avviso di IA all'inizio attivo di default, risposta in arrivo
  spenta di default), annuncio e consenso nei meeting, scorciatoie.
- **RAG** (tab di primo livello): archivi, documenti, stato dell'indice, collegamento alle personalità, prova di una domanda.
- **Settings > Personalità**: crea, modifica, memoria (visibile e modificabile), voce, avatar, archivi collegati. **Chat e Codice**: menu delle personalità nell'intestazione.
- **Loader**: tab "Servizi vocali" e pulsante "Funzioni vocali" con dialog. **App Android**: tab Avatar, modi: video dal PC via WebRTC, Wav2Lip sul telefono, visemi; se l'avatar non è
  caricato sul PC resta in solo voce e si ricollega da solo quando parte (stessa procedura del gateway di callAPIcall).
- Tutta la UI: mobile-first, bersagli ≥44 px, italiano e inglese, niente emoji.

## 6. Sicurezza

- Trascrizioni di telefonate e meeting, documenti RAG e testo parlato sono **contenuto non fidato** (recintati); regola dei due e livelli di permesso valgono anche per la voce.
- Telefonate: dall'agente sempre con approvazione manuale o automatica scelta in Voice e presentazione come IA all'inizio; mai un secondo tentativo automatico; chiamate fatte
  a mano dalla GUI senza approvazione; risposta in arrivo spenta di default. Il numero va validato, una chiamata alla volta.
- Meeting: annuncio dell'assistente IA e consenso obbligatori; senza consenso il bot esce. Il bot parla con voce e avatar come camera solo dopo l'annuncio.
- Bot Telegram: sempre in ascolto, ma **solo per gli utenti autorizzati** (lista in Settings > Voice, come `TELEGRAM_ALLOWED_USERS` di Hermes); gli altri non arrivano all'agente e ne resta
  traccia con il loro ID, per poterli autorizzare. I messaggi dell'utente autorizzato sono i suoi prompt; testo inoltrato, file e link dentro sono contenuto non fidato. I permessi si danno
  rispondendo su Telegram, con il livello di Chat impostato in Settings > Voice; telefonate, denaro e azioni irreversibili chiedono sempre una conferma esplicita con riepilogo.
- Tutto in ascolto su 127.0.0.1 con token; le rotte del servizio non si raggiungono dalla rete se non tramite il server di TaDaMoo. Il microfono e la camera non vanno al browser dell'agente.

## 7. Fasi (ordine scelto per dipendenze; l'utente userà il programma solo a fine lavoro)

| Fase | Contenuto | Dipende da |
|---|---|---|
| 0 | Importazione del codice (senza `config.json`, modelli, ambienti) in `voice/`, licenze dei modelli da controllare, struttura, ambienti | – |
| 1 | Servizio vocale, possesso da parte dell'app C#, standby, catalogo motori, collocazione VRAM/RAM, rotte base con token | 0 |
| 2 | STT (Whisper, tutte le taglie), dettatura, appunti avanzati; Windows e X11; attivi all'avvio | 1 |
| 3 | TTS (Kokoro, Piper, Chatterbox, XTTS, CosyVoice, Qwen3, Orpheus, Dia), voci clonate, Voice settings | 1 |
| 4 | Ordine di caricamento, riserva misurata, tab del Loader, dialog "Funzioni vocali", "Cambia servizi in memoria" | 1, 3 |
| 5 | Personalità (Settings, memoria cifrata, menu in Chat e Codice), avatar diretto + harness, filtro del codice, permessi in Voice, chat e codice in voce | 2, 3, 4 |
| 6 | RAG: archivi, indice ibrido, tab, collegamento alle personalità | 5 |
| 7 | Rotte compatibili (`/external`, `/analysis`, STT, TTS), analisi con diarizzazione, importazione di personalità e avatar esistenti | 5, 6 |
| 8 | Telefonate: Android Bluetooth, modem GSM, sicurezza, risposta in arrivo | 5, 7 |
| 9 | Gateway vocale e tab Avatar nell'app Flutter; terminale ESP32 (firmware e client del gateway) | 5 |
| 9b | Bot Telegram come Hermes (long polling, utenti autorizzati, canale principale, risposte che si compongono man mano), sempre in ascolto dall'avvio; stesse funzioni della Chat; pulsante "Trasferisci su Telegram"; importazione dei valori dal `.env` di Hermes; impostazione in Settings > Voice | 5, 7 |
| 10 | Avatar: MuseTalk da video, camera virtuale, Wav2Lip, visemi, creazione avatar guidata | 3, 4, 9 |
| 11 | Accelerazione: RTX Video SDK, interpolazione, esperimento con DLSS vero; opzioni nascoste senza scheda compatibile | 10 |
| 12 | Meeting (ascolto, verbale, annuncio e consenso, parola con avatar), copilota | 8, 10 |
| 13 | Installatore (ambienti, download dei modelli su richiesta), documentazione, code di prove nel giorno | tutte |

## 8. Come si verifica

- Prove automatiche con servizi finti: catalogo, collocazione, ordine di caricamento con misure simulate, filtro del codice, regole di telefonia, consenso dei meeting, importazione,
  rotte e token, UI statica e a 360, 390, 1280 px.
- **Non si fanno senza il via esplicito**: telefonate vere, meeting veri, caricamento di modelli sulla GPU, uso del microfono. Vanno in `tests/QUEUE_daytime.md` e `QUEUE_live_calls.md`.
- **Non verificabili qui**: Linux, schede diverse da quella presente, telefoni, modem, Meet/Teams/Zoom reali. Si consegnano "scritti, non provati" e si dichiara.

## 8b. Avatar: quale motore su quale dispositivo (decisione del 9 novembre)

- **App Android ed ESP32**: gli avatar principali sono **Wav2Lip sul dispositivo** (foto reale, inferenza ONNX, NPU) e i **visemi** (foto animata a forme di bocca, CPU, molto leggero). Il PC manda solo audio e, per i visemi, la linea del tempo delle forme di bocca.
  L'ESP32 usa i visemi. Nessuna GPU del PC serve per questi due modi.
- **Video generato dal PC (MuseTalk)**: secondario, **solo per emergenza** (il dispositivo non riesce a usare né Wav2Lip né i visemi, o lo chiede la persona). È il solo modo che usa VRAM e a cui servono i pesi di MuseTalk e la GPU; per questo l'accelerazione (fase 11) vale solo per lui.
- **Creazione di un avatar**: parte da una foto (poster, ritaglio del volto per Wav2Lip, sprite delle bocche per i visemi) e non usa la GPU. Il video della persona è facoltativo (bocche reali, MuseTalk di emergenza).
- Degradazione: wav2lip -> visemi -> video del PC (solo emergenza) -> solo voce.
- Da decidere con la fase dei meeting: l'avatar come camera del meeting ha bisogno di fotogrammi sul PC; con questa priorità conviene un renderer leggero sul PC (visemi, o Wav2Lip con onnxruntime) e MuseTalk solo se la persona lo accende.
- Fasi nuove: **9c** lato PC (protocollo dei motori nel gateway, sprite dei visemi, modello Wav2Lip e asset serviti al telefono, importazione del terminale ESP32 e suo protocollo); **9d** lato app Flutter (renderer dei visemi in Dart, Wav2Lip ONNX con un plugin Kotlin, scelta e degradazione dei motori).
- Licenza: Wav2Lip originale è per uso di ricerca e non commerciale; il file ONNX di callAPIcall non ha provenienza dichiarata. Da decidere prima di distribuire a terzi.

## 9. Licenze (verificate sui file originali l'8 novembre 2026) e punti ancora aperti

| Componente | Licenza | Uso commerciale |
|---|---|---|
| MuseTalk (codice) | MIT | sì; i pesi "per qualunque scopo, anche commerciale"; i dati di prova no; i modelli di terzi che usa (Whisper, sd-vae, DWPose, face-parsing, SyncNet) seguono ciascuno la propria licenza, e per S3FD il repository non ne indica una |
| CosyVoice | Apache 2.0 | sì |
| Chatterbox | MIT | sì |
| Qwen3-TTS (modelli e tokenizer) | Apache 2.0 secondo il rapporto tecnico e i riferimenti a valle; la scheda ufficiale su Hugging Face non l'ho letta | sì, da riconfermare sulla scheda |
| XTTS v2 | Coqui Public Model License 1.0: solo uso non commerciale; Coqui ha chiuso e non c'è una via commerciale | **no** |

Non verificate perché non richieste: Kokoro, Piper, Orpheus, Dia, Whisper, RTX Video SDK (ha le sue condizioni NVIDIA).
Decisione dell'utente: i modelli Apache 2.0 e MIT si possono usare liberamente. Nessun modello si impacchetta né si scarica da solo (§3); XTTS si scarica solo su richiesta esplicita, con la conferma "uso non commerciale". Prima di distribuire
TaDaMoo a terzi serve la decisione dell'utente su cosa includere (c'è già la questione "licenza e destinatari").

Punti ancora aperti:
1. **Snapshot di callAPIcall**: committato lo stato del disco sul branch StreamingTTS (63ed73eb, locale, senza push); è quello che si importa. Restano fuori `sospeso.on` e `target-nuova`.
2. **Bot Telegram e Hermes**: un bot può avere un solo ricevitore di aggiornamenti alla volta (long polling). Finché il gateway Telegram di Hermes è acceso con lo stesso token, i due si
   contendono i messaggi: quando TaDaMoo prende il bot, il gateway Telegram di Hermes va spento a cura dell'utente (non si modifica la configurazione di Hermes), oppure TaDaMoo usa un secondo bot.
   Il token si tiene fuori dal repository di TaDaMoo, nel gestore credenziali. Trasferimento di una chat: ogni chat Telegram autorizzata corrisponde a una conversazione; il pulsante "Trasferisci
   su Telegram" (in alto, vicino a esporta e ai pulsanti di dimensione) lega la conversazione corrente alla chat Telegram dell'utente, da lì risposte e prompt passano da Telegram, e "Riporta qui" la slega.
   Da confermare in revisione.
3. **Python**: l'installatore di TaDaMoo usa 3.13; il servizio vocale resta in 3.10 con ambienti propri. `audioop` (GSM) non esiste in 3.13.
4. **Persona Andrea_Taini**: contiene dati personali; l'importazione la mette nel sistema cifrato e non in file in chiaro.
5. **Esperimento DLSS**: costo e qualità si decidono dopo la misura; può restare spento.
6. **WebView2** deve concedere il microfono alla pagina dell'app (da verificare).
7. Documenti da tenere sotto le 300 righe; `docs/` va diviso in cartella `docs/voice/`.
