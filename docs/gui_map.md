# La GUI com'è (letta dal server in esecuzione il 18 novembre 2026)

Generato aprendo la GUI vera in un browser e leggendo le intestazioni di ogni scheda. Dove una funzione non compare qui, per l'utente non esiste: va aggiunta qui e in `serve/tests/test_gui_features.py`.

## Schede in alto
Chat, Agentic coding, Required actions, Plans, Cron Jobs, Monitor, Models, Connectors, Knowledge, Voice, Canary, Tasks, Settings.

## Impostazioni (sotto-schede)
- **General**: The project's own checks, When a budget runs out, and model errors, Show in the chat what the harness checks, Local model asleep for a heavy job
- **Inference**: Technical session, Next-message suggestions, When the thinking takes the whole answer, A reply that repeats itself, Extra model calls and long conversations
- **Models and capabilities**: Install requests, Search online, Sources asked, Weights of the score (0 to 100 each), Installed by TaDaMoo, Vision (26), Documents (27), Data (15), Web (70), Code (58), Audio and video (10), Knowledge (9), Communication (16), Other (107), Imported skills (8), Imported plugins (1), Imported MCP servers (0), Imported plans (1)
- **Skills**: Add a skill, Installed (8), More, Available to install (1)
- **Cloud and privacy**: Plan fields that may be sent
- **Phones**: 1. Tailscale on this PC, 2. Let phones connect, 3. A key for each phone, 4. What a phone may start
- **Network**: Listen on, Names, Server key
- **Memories**: Long-term memory (11 notes), Weight of each section, Short-term memory (88 notes), Models that are not on this PC, Notes, Write a note, What was learned lately, Cards of past conversations, Export to Markdown, Delete notes, Project notes added to a request, Wiki (wikibridge), Reference documents (RAG and CAG), Recent tasks, This project (3), Everywhere (0), Write a lesson
- **Personalities**: Your personalities
- **Web search**: (sezioni senza titolo)
- **Voice**: Bot token, Permission level, People who may talk to the agent, Senders that were ignored, Home channel, Import from Hermes
- **Benchmarks**: Model and machine, The benchmarks, Runs
- **Wallet**: (sezioni senza titolo)

## Dove stanno le funzioni che sembrano mancare
- **2FA con Google Authenticator e importazione dei valori**: Settings > Wallet, pulsanti «Import logins» e «Import from Google Authenticator».
- **Scelta del browser** (agent-browser o il driver di TaDaMoo): Settings > Wallet > «Browser for forms and bookings» > «What drives the browser». Per il dark web: Chat > impostazioni di inferenza > Dark web > «Browser used when a page needs one».
- **Dark web**: Chat > impostazioni di inferenza > Dark web (interruttore, ricerca, lettura, browser, Tor). Compare solo se il server gira con il codice nuovo (si riavvia il servizio).
- **Connectors**: scheda Connectors, sotto-schede Skills, MCP servers, MCP apps, Plugins, Providers; interruttore Esterni/Personali/Tutti; rivedi, installa, disinstalla, albero delle dipendenze, cartella degli originali.
- **Knowledge (RAG)**: scheda Knowledge: progetti, archivi, albero dei documenti con frequenze, trascinamento, «Sfoglia questo PC», «Aggiungi da una richiesta a parole», indirizzi web con profondità, immagini e MinIO, database, figure.

## Funzioni costruite o decise che NON hanno ancora il loro controllo nella GUI
- Connectors: pulsante «Configura» (configurazione guidata, più account per connettore), controllo aggiornamenti e unione, sotto-scheda «MCP apps» vuota, app Android e nativa.
- Dark web: interruttori per le sorveglianze e per gli archivi di fughe di dati (esistono come impostazioni, non come controlli).
- Knowledge: scegliere una versione più vecchia di un archivio; installazione automatica degli ambienti Python (embedding, Docling).
- Variabili d'ambiente lette dal codice senza controllo nella GUI: vedi l'elenco in `docs/task_diary.md`, voce «Audit GUI».
