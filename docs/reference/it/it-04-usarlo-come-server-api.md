> Riferimento: [README](../../../README.it.md).

## Usarlo come server (API)

Il server parla le API di OpenAI e di Anthropic a `http://127.0.0.1:<porta>` (porta predefinita **7232**):

| endpoint | API |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions: messaggi, strumenti (function calling), immagini se la visione è accesa, `response_format`, `stream` |
| `POST /v1/responses` | OpenAI Responses, **senza stato**: si manda tutta la conversazione ogni volta (`previous_response_id` e `GET /v1/responses/<id>` non sono conservati) |
| `POST /v1/messages` | Anthropic Messages: strumenti, blocchi di ragionamento (thinking), immagini, `stream` |
| `POST /v1/messages/count_tokens` | conteggio dei token di Anthropic: lo stesso prompt che leggerebbe `/v1/messages`, senza eseguirlo |
| `GET /v1/models` | il modello servito e ogni altro modello delle cartelle dei modelli che potrebbe servire (forma OpenAI o Anthropic) |
| `GET /v1/models/{id}` | uno di questi |
| `GET /health` | `{"service": "tadamoo", "loaded": true, ...}` quando il modello è caricato; `"standby": true` mentre un server avviato dall'app aspetta il Loader |

**Avviarlo per gli agenti**: `.\Avvia-TaDaMoo.ps1 -server` (torna quando il modello è caricato; negli script senza
nessuno davanti aggiungi `-riusa`), fermarlo con `.\Ferma-TaDaMoo.ps1`. `tadamoo.pid` contiene PID, porta e profilo di un
server avviato da uno script o dall'app.

**Nome del modello.** L'id del modello servito è l'`--alias` (o `"alias"` nella sezione `server` del file di
configurazione), altrimenti il nome scritto nel file del modello. Il campo `model` di una richiesta può essere
qualunque cosa (il server esegue un modello alla volta) e la risposta riporta il nome servito, con un'eccezione: un
nome che `GET /v1/models` elenca come un altro modello, non caricato, viene rifiutato (sotto).

**L'elenco dei modelli.** `GET /v1/models` elenca per primo il modello servito (`"status": {"value": "loaded"}`, o
`"unloaded"` mentre si carica alla prima richiesta), poi ogni file `.basalt` delle cartelle dei modelli che il Loader
mostra come **pronto** (un GGUF da convertire o un file che Basalt-Plus non può eseguire non è elencato), ciascuno con
`"status": {"value": "unloaded"}`. L'id di un modello così è il nome del file senza `.basalt` (con un breve hash in
coda quando due cartelle hanno lo stesso nome di file): il nome da scegliere in un client. Ogni voce ha `created` (la
data del file), `owned_by`, `display_name`, la lunghezza del contesto quando il file la dice (`context_length`; per il
modello servito `meta.n_ctx` è il contesto con cui gira, per un altro modello `meta.n_ctx_train` il più lungo con cui
è stato addestrato), `architecture.input_modalities` (`image` quando il file ha un proiettore visivo) e, solo su un
server in ascolto su 127.0.0.1, il `path` del file. Le cartelle si leggono dalle intestazioni dei file, su un thread, al
massimo ogni 30 secondi: né l'elenco né una richiesta di chat aspettano il disco; l'elenco funziona anche in standby
(nessun modello caricato: solo i modelli delle cartelle). Una richiesta che nomina un modello che l'elenco dà come non
caricato (per id, nome del file o percorso) riceve un 404 che dice come caricarlo, invece della risposta del modello
caricato: OpenAI `{"error": {"code": "model_not_found", ...}}`, Anthropic
`{"type": "error", "error": {"type": "not_found_error", ...}}`.

```sh
# forma OpenAI
curl http://127.0.0.1:7232/v1/models
# {"object": "list", "data": [
#   {"id": "Qwen3.8 Flash Next Uncensored", "object": "model", "created": 1791904651, "owned_by": "tadamoo",
#    "display_name": "Qwen3.8 Flash Next Uncensored", "status": {"value": "loaded"}, "meta": {"n_ctx": 131702},
#    "context_length": 131702, "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
#    "path": "D:/Modelli/Qwen3.8-Flash-Next-Uncensored-IQ3_XXS.basalt"},
#   {"id": "OrcaRouter-IQ3_XXS", "object": "model", "created": 1791904651, "owned_by": "tadamoo",
#    "display_name": "OrcaRouter", "status": {"value": "unloaded"}, "context_length": 262144,
#    "meta": {"n_ctx_train": 262144}, "quant": "IQ3_XXS", "path": "D:/Modelli/OrcaRouter-IQ3_XXS.basalt", ...}]}

# forma Anthropic: con l'header anthropic-version (o x-api-key senza Authorization)
curl http://127.0.0.1:7232/v1/models -H "anthropic-version: 2023-06-01"
# {"data": [{"type": "model", "id": "Qwen3.8 Flash Next Uncensored", "display_name": "Qwen3.8 Flash Next Uncensored",
#            "created_at": "2026-10-13T15:17:31Z"},
#           {"type": "model", "id": "OrcaRouter-IQ3_XXS", "display_name": "OrcaRouter",
#            "created_at": "2026-10-13T15:17:31Z"}],
#  "has_more": false, "first_id": "Qwen3.8 Flash Next Uncensored", "last_id": "OrcaRouter-IQ3_XXS"}

# un modello, nelle due forme (un id con spazi va codificato nell'URL); altrimenti 404 model_not_found / not_found_error
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS
curl http://127.0.0.1:7232/v1/models/OrcaRouter-IQ3_XXS -H "anthropic-version: 2023-06-01"

# una chat che nomina il modello non caricato
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model": "OrcaRouter-IQ3_XXS", "messages": [{"role": "user", "content": "ciao"}]}'
# 404 {"error": {"message": "The model 'OrcaRouter-IQ3_XXS' is not loaded (this server is serving 'Qwen3.8 Flash
#   Next Uncensored'). Load it in the web page's Loader (choose it in the model list, then Load), or start the server
#   with it: .\Avvia-TaDaMoo.ps1 --model \"D:/Modelli/OrcaRouter-IQ3_XXS.basalt\"", "type": "invalid_request_error",
#   "param": "model", "code": "model_not_found"}}
```

TODO (possibile in futuro: caricare su richiesta): il server non cambia modello da solo quando una richiesta ne nomina
un altro dell'elenco; dice solo come caricarlo. Caricare il modello chiesto alla richiesta resta per dopo.

**Chiave API.** Il server ascolta solo su 127.0.0.1 e non chiede chiavi: ai client che ne vogliono una per forza si
può dare un valore qualunque. `--api-key CHIAVE` (solo da riga di comando, mai nel file di configurazione) la rende
obbligatoria su `/v1/*`, come `Authorization: Bearer CHIAVE` o `x-api-key: CHIAVE`; serve per ascoltare su un altro
indirizzo (`--host 0.0.0.0`) e per lasciar usare il server a pagine web di altre origini. Script e app avviano il
server su 127.0.0.1 senza chiave.

**Streaming.** `"stream": true` manda eventi server-sent nel formato di ciascuna API (OpenAI `chat.completion.chunk`,
eventi Responses, Anthropic `message_start` / `content_block_delta` / ...), ragionamento compreso, come
`reasoning_content` (OpenAI) o blocchi `thinking` (Anthropic).

**Ragionamento.** Il modello ragiona per impostazione predefinita (il livello alto del template). OpenAI:
`"reasoning_effort": "none" | "low" | "medium" | "high"` (Responses: `"reasoning": {"effort": ...}`). Anthropic:
`"thinking": {"type": "disabled"}`, oppure `"enabled"` con `budget_tokens` (sotto 2048: basso, sotto 8192: medio,
altrimenti alto). `reasoning_budget_tokens: N`, su qualunque endpoint, chiude il ragionamento dopo N token e fa
rispondere il modello.

```sh
curl http://127.0.0.1:7232/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "tadamoo", "messages": [{"role": "user", "content": "Saluta in tre lingue."}],
  "max_tokens": 512, "reasoning_effort": "low"}'

curl -N http://127.0.0.1:7232/v1/messages -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -d '{
  "model": "tadamoo", "max_tokens": 1024, "stream": true,
  "messages": [{"role": "user", "content": "Scrivi un haiku sul basalto."}]}'

curl http://127.0.0.1:7232/v1/responses -H "Content-Type: application/json" -d '{
  "model": "tadamoo", "input": "Quanto fa 17 * 23?", "reasoning": {"effort": "none"}}'
```

(In PowerShell usa `curl.exe` e metti il JSON in un file: `-d "@corpo.json"`.)

Python, con gli SDK ufficiali (`pip install openai anthropic`):

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:7232/v1", api_key="none")   # chiave qualunque, salvo --api-key
r = client.chat.completions.create(model="basalt", messages=[{"role": "user", "content": "Ciao!"}],
                                   max_tokens=512, reasoning_effort="low")
print(r.choices[0].message.content)

for chunk in client.chat.completions.create(model="basalt", stream=True,
                                            messages=[{"role": "user", "content": "Conta fino a cinque."}]):
    print(chunk.choices[0].delta.content or "", end="", flush=True)

print(client.responses.create(model="basalt", input="Nomina tre rocce vulcaniche.").output_text)
```

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:7232", api_key="none")
msg = client.messages.create(model="basalt", max_tokens=1024,
                             messages=[{"role": "user", "content": "Ciao!"}])
print(msg.content[-1].text)                      # prima può esserci un blocco di ragionamento

with client.messages.stream(model="basalt", max_tokens=1024, thinking={"type": "disabled"},
                            messages=[{"role": "user", "content": "Conta fino a cinque."}]) as s:
    for text in s.text_stream:
        print(text, end="", flush=True)

print(client.messages.count_tokens(model="basalt", messages=[{"role": "user", "content": "Ciao!"}]).input_tokens)
```

**Claude Code** usa l'endpoint di Anthropic:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:7232"
$env:ANTHROPIC_AUTH_TOKEN = "none"                 # valore qualunque; la chiave data con --api-key, se c'è
$env:ANTHROPIC_MODEL = "basalt"                    # nome qualunque: il server ha un solo modello
$env:ANTHROPIC_SMALL_FAST_MODEL = "basalt"
claude
```

(Su Linux/macOS: `export ANTHROPIC_BASE_URL=http://127.0.0.1:7232` e così via.) Gli strumenti per agenti mandano prompt
lunghi: scegli nel profilo di caricamento un contesto di almeno 65.536 token, e tieni `--parallel 1` a meno che più
agenti lavorino insieme (il contesto si divide fra i posti, vedi
[`--parallel`](../upstream-basalt.md#concurrent-requests---parallel-n)).

**Output strutturato** (`/v1/chat/completions`): `"response_format": {"type": "json_object"}` oppure `{"type":
"json_schema", "json_schema": {"name": ..., "schema": {...}}}`. La risposta viene controllata; se non è JSON valido, o
non rispetta lo schema, la richiesta si rigenera con l'errore come spiegazione, fino a `structured_retries` volte (da
0 a 5, predefinito 2; 0 lo spegne). Il nuovo tentativo è la stessa conversazione più un messaggio che dice cosa non
andava e dove (l'errore, il percorso JSON in cui il testo si è rotto, una voce scritta più volte, una sola volta, col
suo numero di ripetizioni); il testo rifiutato non viene mai rimandato indietro, così un modello che girava a vuoto non
rivede il suo giro. Solo la risposta validata resta trattenuta fino al controllo. Il ragionamento scorre mentre viene
scritto, anche quello dei tentativi rifiutati; un tentativo rifiutato è annunciato da un chunk con `delta` vuoto e
`"structured_output": {"attempt": 1, "discarded": true, "reason": "...", "retrying": true}`, la sua risposta non viene
mai inviata, e l'ultimo chunk porta solo la risposta validata, con `"structured_output": {"validated": true,
"attempts": 2, "discarded": [{"attempt": 1, "reason": "..."}]}` (una risposta senza streaming restituisce
ragionamento e risposta dell'ultimo tentativo, con lo stesso campo). `GET /v1/status` lo dichiara in
`structured_output`: `content_buffered` e `reasoning_streamed` sono veri, e `stream_buffered` (lasciato per i client
vecchi) significa che la risposta validata è trattenuta fino al controllo. `/v1/responses` con un `text.format` JSON
trasmette anche il ragionamento e controlla la risposta una volta, senza ritentare; `/v1/messages` non ha output
strutturato.

**Occupato ed errori.** Con `--parallel 1` le richieste aspettano il loro turno in ordine d'arrivo. Mentre il motore
parte, o mentre un server avviato dall'app è in attesa (nessun modello caricato), una richiesta riceve un 503 che dice
perché: riprova più tardi, o carica un modello nel Loader. Un prompt più lungo del contesto viene rifiutato con un 400
che dice la lunghezza del contesto.

## Impostazioni di caricamento, una per una

Si impostano nel Loader (ognuna con il suo motivo e la previsione dal vivo), si salvano nei profili di caricamento e
si scrivono nelle sezioni `server` / `engine` di `tadamoo_config.json`. Valgono quando il motore si (ri)carica.

| impostazione | cosa fa | come sceglierla |
|---|---|---|
| **Modello** (`server.model`) | il file `.basalt` da servire | dalla libreria dei modelli; un GGUF prima si converte |
| **Porta** (`server.port`) | la porta HTTP, predefinita 7232 | cambiala solo per due server insieme o se la 7232 è occupata |
| **Richieste in parallelo** (`server.parallel`, 1-8) | richieste servite insieme nei posti (slot) del motore | 1 per un utente o un agente (la più veloce per richiesta); di più solo per più agenti insieme. Il contesto si divide: ogni posto ha `max-context / N` |
| **Contesto intero per posto** (`server.slot-full-context`) | ogni posto ha tutto il contesto invece di una parte | costa N volte la cache KV, presa dalla cache degli esperti: lascialo spento salvo che ogni agente abbia bisogno di tutta la lunghezza |
| **Immagini** (`server.vision`) | `off`, `dynamic` (prende in prestito posti della cache degli esperti solo mentre legge un'immagine: nessuna VRAM fissa), un numero di GPU come `0` (encoder sempre su quella scheda, circa 2,2 GiB della sua VRAM; i vecchi `on` e `peer` si leggono come GPU 0 e GPU 1), `cpu` (niente VRAM, lento) | `dynamic` se il file ha un proiettore, altrimenti `off` |
| **Contesto** (`engine.max-context`) | la conversazione più lunga, in token (fino a 262.144) | il più lungo che ci sta (lo scelgono «Valori sicuri» e la configurazione guidata); uno più corto lascia più VRAM agli esperti (un po' più veloce) e chiede meno commit |
| **Cache KV** (`engine.kv`) | la precisione della memoria dell'attenzione: `fp16`, `int8`, `q4_0`, `k8v4` (chiavi INT8, valori Q4_0) | `q4_0`: un quarto della memoria di fp16, così i contesti lunghi ci stanno; `int8` o `k8v4` per un'attenzione un po' più precisa con più memoria |
| **Blocchi del prompt** (`engine.prefill`) | come si leggono i prompt lunghi: `auto` (il blocco più grande che la cache degli esperti può prestare), `auto:8192/16384/32768` (un tetto), o una misura fissa | `auto` |
| **Finestra di verifica** (`engine.spec`, 0-8) | la finestra più lunga: il drafter MTP propone fino a N-1 token (meno quando è incerto), il modello li controlla in un passaggio; 0 = spento | 7 se il file ha un drafter (la configurazione guidata misura 5, 7 e 8); il testo è lo stesso a ogni valore, cambia solo la velocità |
| **Cache degli esperti** (`engine.expert-cache`) | posti per esperti tenuti in VRAM: `auto` (tutta la VRAM che resta dopo la riserva) o un numero | `auto` |
| **Esperti residenti** (`engine.resident-experts`) | gli esperti che non sono in VRAM si copiano in RAM una volta al caricamento, invece di leggerli dal file | acceso se ci stanno in RAM tenendo libera la quota (circa 50 GiB per il file a 3 bit); spento sui PC con meno RAM, a costo di velocità |
| **VRAM da lasciare libera** (`engine.vram-reserve-mib`) | VRAM che il motore non tocca (desktop, driver, altri programmi) | vedi [VRAM da lasciare libera](it-03-compatibilita.md#vram-da-lasciare-libera); la regola della configurazione guidata: 0,5 GB + 1,5 GB se la GPU comanda il monitor principale (circa 0,4 GB per uno secondario) |
| **Tabella PLE** (`engine.ple-io`) | la tabella n-gram per strato: `direct` (letta dall'SSD quando serve, mai in RAM), `ram` (bloccata in RAM all'avvio: RAM ma niente commit), `mmap` (una variante di prova) | `ram` se ci sta tenendo libera la quota, altrimenti `direct`; su un disco lento o meccanico meglio `ram` |
| **Thread CPU** (`engine.pool-workers`) | i thread che calcolano gli esperti che non sono in VRAM | i core performance più metà dei core efficienza, meno uno (un core resta per il server e il desktop) |
| **KV tenuta in VRAM** (`engine.kv-resident`, 0 o da 20.480 celle) | in VRAM restano solo queste celle della KV di ogni strato, tutta la K/V è bloccata in RAM e la VRAM liberata va agli esperti; un contesto che sta in queste celle non viene trasferito | contesti molto lunghi su una scheda piccola; più lento quanto più si trasferisce; non con `k8v4`. Il piano sposta il pool KV dalla cifra della VRAM a quella della RAM |
| **Estensione del contesto** (`engine.rope-scaling`: `none`, `linear`, `yarn`) | estende il contesto oltre la lunghezza addestrata | fissa per l'avvio e cambia l'output; vuoto = quella del file del modello |
| **Fattore di estensione** (`engine.rope-scale`, 1 o più) | il fattore per `linear` e `yarn` | vuoto = quello del file del modello, altrimenti 1 |
| **Base di frequenza RoPE** (`engine.rope-freq-base`, 0 o sopra 1) | la base di frequenza grezza, 0 = quella del modello | lascia vuoto se non sai il valore che vuoi; fissa per l'avvio |
| **Quota CPU degli esperti del prompt** (`engine.prefill-cpu-share`, 0-1) | la quota degli esperti del prompt trasferiti che calcola la CPU; 0 fa trasferire tutto alla GPU | 0,6 predefinito; più bassa con una CPU più debole |
| **Scambi adattivi** (`engine.adapt-swaps`) | il massimo di esperti che un giro adattivo scambia nella cache esperti della GPU principale; 0 = spenti | 96 predefinito; con gli scambi accesi l'output non è identico bit per bit da una volta all'altra |
| **Budget di RAM per gli esperti** (`engine.resident-budget-gib`, GiB) | al massimo questi GiB di esperti copiati in RAM, i più usati per primi | vuoto = automatico; la cifra di RAM del piano ne è limitata; il motore rifiuta un budget che lascia libera meno RAM del suo margine |
| **Esperti nella mappatura del file** (`engine.mmap-experts`) | nessuna copia residente in RAM: gli esperti si leggono da una mappatura del file; prevale su esperti residenti e budget di RAM | di solito più lento (dipende dalla cache di pagine del sistema); spento di default |
| **Cache esperti elastica** (`engine.vram-elastic`) | la cache degli esperti a segmenti, così `POST /v1/vram` può prestare VRAM ad altri programmi tra una richiesta e l'altra | accesa; `false` (`--no-vram-elastic`) tiene un'unica allocazione |
| **Mantieni il commit della memoria GPU** (`engine.keep-vram-commit`, Windows) | tiene il commit di sistema che WDDM addebita per la memoria della GPU invece di restituirlo | spento (il commit si restituisce); attivalo se un altro programma alloca memoria GPU mentre il motore è fermo, perché il motore può bloccarsi se quel programma ne spinge fuori la VRAM |
| **File del profilo esperti** (`engine.expert-profile-save`, avanzata) | il file in cui il motore salva ciò che la cache adattiva ha imparato, all'uscita e a intervalli | vuoto = non si salva; lo legge `tools/convert/convert.py --profile` |
| **Intervallo di salvataggio del profilo** (`engine.expert-profile-save-every`, minuti, avanzata) | ogni quanto si salva il profilo; 0 = solo all'uscita | 10 predefinito |
| **Checkpoint del prompt** (`engine.prompt-cache`) | checkpoint di conversazione tenuti tra una richiesta e l'altra, circa 118 MB di RAM ciascuno per slot; 0 rilegge ogni prompt dall'inizio | 6 predefinito; il piano li conta |
| **Checkpoint ogni** (`engine.prompt-cache-every`, token) | crea un checkpoint anche ogni N token di prompt appena letti; 0 = spento | 16.384 predefinito |
| **Checkpoint del primo turno** (`engine.prompt-cache-root`, token) | crea un checkpoint alla fine del prompt di sistema quando è lungo almeno così; 0 = mai | 2.048 predefinito |
| **Conversazioni parcheggiate** (`engine.conversation-cache-mib`, MiB) | budget di RAM per le conversazioni parcheggiate, così chat separate tornano senza rileggere; 0 = spenta | 0 predefinito; il piano conta l'intero budget |
| **Conversazioni parcheggiate, al massimo** (`engine.conversation-cache-slots`) | quante conversazioni si parcheggiano per volta | 4 predefinito; 0 spegne la cache |
| **RAM libera al parcheggio** (`engine.conversation-cache-min-free-mib`, MiB) | il margine di RAM fisica che resta libero quando si parcheggia una conversazione | 2.560 predefinito |
| **Grammatica** (`server.grammar`) | tiene ogni chiamata di strumento e ogni formato di risposta JSON al suo schema dentro il motore | acceso di default; vale al prossimo Carica |

Una regola in più vale per il PC, non per il profilo: la **quota di RAM e commit lasciata libera** agli altri
programmi (15% se la configurazione guidata non ha avuto un'altra risposta, dal 5 al 50%, almeno 2 GB). Ogni piano,
«Valori sicuri» e la configurazione guidata la tengono libera, e un caricamento che farebbe crescere il file di paging
viene rifiutato.

## Impostazioni della chat (inferenza), una per una

Nel pannello delle impostazioni della chat; si salvano per client e come **preset di inferenza** con un nome,
condivisi tramite il server (il preset **Machine Default** è quello della configurazione guidata). Una richiesta porta
solo i campi impostati, così tutto quello che resta vuoto usa i **valori del modello** (da `general.sampling.*` del
GGUF d'origine, mostrati in grigio nei campi), poi quelli del motore. I client delle API mandano gli stessi campi nelle
richieste.

| impostazione | campo della richiesta | cosa fa | consiglio |
|---|---|---|---|
| **Thinking** (accanto al messaggio; effort) | `reasoning_effort` | quanto il modello ragiona prima di rispondere: High (predefinito), Medium, Low, Off | High per i problemi difficili, Low/Off per risposte rapide e chiamate di servizio (molto più veloce) |
| **Budget di ragionamento** | `reasoning_budget_tokens` | dopo N token di ragionamento il ragionamento si chiude e il modello risponde; vuoto, 0 o -1 = nessun limite | 2.000-8.000 per limitare le risposte lente; troppo basso taglia il ragionamento |
| **Token massimi in uscita** | `max_tokens` | la risposta più lunga, ragionamento compreso | vuoto per lo spazio del contesto; impostalo per risposte brevi e prevedibili |
| **Prompt di sistema** | il primo messaggio `system` | istruzioni tenute in testa a ogni conversazione | breve e preciso |
| **Stringhe di stop** | `stop` | la risposta finisce prima di una di queste stringhe | per formati che finiscono con un segnale |
| **Temperatura** | `temperature` | casualità: 0 prende sempre il token più probabile | quella del modello (1,0 per Qwen3.8-Flash-Next); 0,6-0,8 per codice più stabile |
| **Top-p** | `top_p` | sceglie solo fra i token più probabili che insieme arrivano a questa probabilità | quella del modello (0,95) |
| **Top-k** | `top_k` | sceglie solo fra i k token più probabili (1-64) | quella del modello (20) |
| **Min-p** | `min_p` | scarta i token meno probabili di questa frazione del primo | 0-0,05 |
| **Penalità di ripetizione** | `repetition_penalty` | rende meno probabili i token recenti (1 = spenta) | 1-1,1; più alta peggiora codice ed elenchi |
| **Penalità di presenza** | `presence_penalty` | una penalità fissa per ogni token già usato (0-2) | 0; fino a 1,5 se le risposte girano in tondo |
| **Penalità di frequenza** | `frequency_penalty` | una penalità che cresce a ogni uso (0-2) | 0 |
| **Finestra delle penalità** | `penalty_last_n` | quanti token recenti guardano le penalità | 64 (quella del motore) |
| **Seme** | `seed` | lo stesso seme con le stesse impostazioni ripete una risposta | vuoto per risposte varie |
| **Politica del contesto** | (pagina della chat) | cosa fa la chat quando la conversazione supera il contesto: **Ferma al limite**, **Finestra scorrevole** (toglie i turni più vecchi), **Compatta** (alla soglia il modello riassume i turni di mezzo; prompt di sistema, primo messaggio e i due turni più recenti restano parola per parola) | Compatta per le sessioni lunghe; Ferma al limite quando ogni parola deve restare |
| **Compatta al (% del contesto)** | (pagina della chat) | la percentuale del contesto che fa partire la compattazione (30-100%, predefinita 80%) | 80% |
| **Output strutturato** | `response_format` | Testo, oggetto JSON, o schema JSON (con un nome e lo schema) | per i programmi che leggono la risposta |
| **Tentativi dopo una risposta rifiutata** | `structured_retries` | quante volte una risposta JSON non valida si rigenera, con l'errore come spiegazione (0-5, predefinito 2) | 2 |

**Con quale preset si apre un client**: quello usato per ultimo, se esiste ancora; altrimenti segue il predefinito di
adesso (Machine Default, altrimenti i valori del modello). Le impostazioni cambiate a mano restano, e il pannello dice
quale preset è in uso. **Valori del modello** rimette i valori del modello; **Ripristina Machine Default** quelli della
configurazione guidata.

## Calibrazione

Il Loader prevede le velocità di risposta e di lettura del prompt di ogni piano. La previsione parte da una misura di
riferimento e la scala secondo la percentuale di esperti trovati in cache (quanti esperti il piano tiene in VRAM), la
finestra di verifica e i blocchi del prompt. **Calibra** nel Loader (che parte anche da solo al primo caricamento di un
modello) misura la configurazione in uso con tre richieste: una risposta senza ragionamento (512 token), una con
ragionamento (1.024 token) e un prompt lungo (circa 8.000 token). Il risultato va in `calibration\<modello>.json` nella
cartella dei dati, legato al modello e alle impostazioni. Da lì in poi le previsioni di quel modello partono dalla
calibrazione delle stesse identiche impostazioni, altrimenti dalla più recente del modello, invece che dal riferimento
incorporato, e il Loader dice quale ha usato. La misura della configurazione guidata è la stessa calibrazione, ripetuta
per le finestre di verifica che prova. Accanto alle velocità, il file conserva RAM e commit
presi davvero dal caricamento (vedi «Memoria misurata»).

## Lingua

Inglese come predefinito; italiano a richiesta. Una sola scelta vale per l'app, la pagina web, gli script e gli
strumenti da riga di comando, in quest'ordine: `--lang it|en` (script e app, per quell'avvio) > la variabile
d'ambiente `TADAMOO_LANG` > la scelta salvata in **Impostazioni → Lingua** nella pagina web (o con `python -m serve.lang
set it|en|auto`, conservata in `prefs.json` nella cartella dei dati) > inglese. La lingua di Windows non si usa. Le
pagine Monitor e Hardware e alcuni messaggi tecnici del server sono solo in inglese.

Per avere tutto in italiano una volta per tutte:

```powershell
.venv\Scripts\python -m serve.lang set it
```
