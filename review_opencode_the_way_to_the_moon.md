# Revisione Tecnica e Audit di Completezza: Branch `OperCode_Basalt`

**Riferimento Specifiche:** [`the_way_to_the_moon.md`](file:///E:/Projects/basalt/the_way_to_the_moon.md)  
**Estensione Operativa:** [`conquer_of_the_moon.md`](file:///E:/Projects/basalt/conquer_of_the_moon.md)  
**Data Audit:** 2026-10-23  

---

## 1. Verdetto Esecutivo

> [!CAUTION]
> **I timori dell'utente sono confermati al 100%.**
> Il lavoro svolto da OpenCode sul branch `OperCode_Basalt` è **gravemente incompleto, frammentario e in larga parte non funzionante a runtime**.
> Non solo le specifiche architetturali e i requisiti di sicurezza di [`the_way_to_the_moon.md`](file:///E:/Projects/basalt/the_way_to_the_moon.md) non sono stati completati, ma la maggior parte dei file Python aggiunti in `harness/` contiene **errori sintattici, import rotti, classi inesistenti e metodi mancanti** che ne impediscono l'importazione anche solo come moduli. Inoltre, le modifiche all'interfaccia web in `serve/web/` hanno **rotto feature preesistenti** e introducono widget fittizi che vanno in errore nella console JavaScript.

### Sintesi dello Stato di Avanzamento per Fase

| Fase di Roadmap                           | Stato Reale            | Livello di Funzionamento   | Descrizione Sintetica                                                                                                               |
|:----------------------------------------- |:---------------------- |:-------------------------- |:----------------------------------------------------------------------------------------------------------------------------------- |
| **Fase 0 — Misura & Baseline**            | Incompleta (~15%)      | **Rotta** (ImportError)    | 8 task banali (`17+25=42`); nessun benchmark reale (SWE-bench/BFCL); runner non eseguibile.                                         |
| **Fase 1 — Ciclo Agentico ReAct**         | Parziale (~20%)        | **Non importabile**        | Loop non avviabile (`system_blocks` e `PermissionPolicy` mancanti; `_is_repeat` non definito).                                      |
| **Fase 2 — Sicurezza & Controllo**        | Abbozzata (~15%)       | **Critica / Pericolosa**   | Duplicazione caotica; `audit.py` crasha (`_hash` assente); checkpoints distruttivi su file vuoti; revoca pagamenti azzera la spesa. |
| **Fase 3 — Contesto, Memoria, Skill**     | Frammentata (~20%)     | **Non funzionante**        | Classe `Memory` scritta 3 volte con schemi incompatibili; `mask_output` assente; nessun hash di approvazione per le skill.          |
| **Fase 4 — Interoperabilità (MCP/ACP)**   | Stub fittizio (~10%)   | **Completamente bloccata** | MCP invia richieste prive di `"id"` (si blocca per sempre in attesa risposta); ACP è un finto script da 80 righe.                   |
| **Fase 5 — Interfaccia (Canvas, DAG)**    | Mock di facciata (~5%) | **Non conforme / Buggata** | Nessun grafo DAG reale; canvas è solo testo concatenato con frecce; cancellati i controlli di `reqlog` in `index.html`.             |
| **Fase 6 — Conoscenza (wikibridge, RAG)** | Stub iniziale (~10%)   | **Rotta**                  | Package `integrations` fallisce l'import (`fetch_docs` assente); nessun benchmark RAG vs CAG.                                       |
| **Estensioni (`conquer_of_the_moon.md`)** | **0%**                 | **Inesistente**            | `PlanDAG`, `JevTriageGatekeeper`, `CallAPICallClient` e `serve/planner/` esistono solo nel testo markdown.                          |

---

## 2. Analisi Dettagliata dei Bug e delle Fallacie nel Codice (File per File)

### 2.1 `harness/agent_loop.py` (Ciclo Agentico)

* **Import rotti a monte**:
  * Linea 16: `from .context import system_blocks` — **Fallisce immediatamente**: [`harness/context.py`](file:///E:/Projects/basalt/harness/context.py) non esporta alcuna funzione `system_blocks`.
  * Linea 18: `from .permissions import PermissionPolicy` — **Fallisce**: in [`harness/permissions.py`](file:///E:/Projects/basalt/harness/permissions.py) la classe è stata battezzata `PermissionEngine`, non `PermissionPolicy`.
* **Metodo mancante (`AttributeError`)**:
  * Linea 122: `if self._is_repeat(key, last_call, reflected):` — Il metodo `_is_repeat` **non è implementato né nella classe `Agent` né come funzione**. Se invocato, manda il processo in crash immediato.
* **Calcolo del tempo errato**:
  * Linee 96 e 102: `turn.seconds = 0.0`. La variabile `t0 = time.monotonic()` viene impostata **dopo** che la chiamata di rete al modello si è conclusa, e non viene mai calcolato il delta per `turn.seconds`. I report di tempo riporteranno sempre `0.0`.
* **Gestione distruttiva della policy `"ask"`**:
  * Linee 119-121: Se la policy restituisce `"ask"` (richiesta di conferma umana per un'azione a rischio), l'agente esegue `return trace`, **terminando bruscamente l'intera conversazione** senza interagire né con l'utente né con l'harness.

---

### 2.2 `harness/audit.py` e `harness/safety.py` (Audit Ledger a Catena di Hash)

* **Metodo mancante nel core della catena di hash**:
  * In [`harness/audit.py`](file:///E:/Projects/basalt/harness/audit.py#L37), linea 37: `self._prev = self._hash(rec)`. Il metodo `_hash` **non esiste** nella classe `AuditLedger`. L'invocazione di `record()` fallisce sempre con `AttributeError: 'AuditLedger' object has no attribute '_hash'`.
* **Firma del metodo disallineata e crash dei test**:
  * In [`harness/audit.py`](file:///E:/Projects/basalt/harness/audit.py#L27):
    `def record(self, event: str, tool: str, args: dict, decision: str) -> None:` (4 parametri).
  * In `tests/test_harness.py`:
    `l.record("tool", {}, "allow")` (chiamata con 3 parametri, fallisce con `TypeError`).
  * In [`harness/payments_broker.py`](file:///E:/Projects/basalt/harness/payments_broker.py#L38):
    `self.ledger.record("payment", {"vendor": vendor, ...}, "allow")` (fallisce con `TypeError`).
* **Duplicazione incoerente di codice**:
  * OpenCode ha implementato una versione di `AuditLedger`, `PermissionEngine`, `HookRunner` e `Checkpoints` in [`harness/safety.py`](file:///E:/Projects/basalt/harness/safety.py) e un'altra versione completamente diversa nei singoli moduli (`audit.py`, `permissions.py`, `hooks.py`, `checkpoints.py`).

---

### 2.3 `harness/checkpoints.py` (Snapshot e Ripristino File)

* **Perdita di gerarchia delle directory**:
  * Linea 29: `dst = dst_dir / Path(f).name`. Se l'agente modifica `src/core/layer.cpp` e `src/kernels/layer.cpp`, entrambi vengono copiati come `layer.cpp` nella stessa cartella di checkpoint, sovrascrivendosi a vicenda.
* **Ripristino distruttivo e incontrollato su tutto il disco**:
  * Linee 49-54:
    
    ```python
    target_name = f.name
    targets = list(self.root.rglob(target_name))
    for t in targets:
        if Path(t).parent != d and f.stat().st_size == 0:
            Path(t).unlink()
        else:
            shutil.copy2(f, t)
    ```
    * `rglob` cerca ricorsivamente in **tutto il repository** qualsiasi file con quel nome. Se si ripristina un checkpoint, sovrascrive file con lo stesso nome in qualunque sottocartella (anche dentro `.venv` o `build/`).
    * **Cancellazione di file legittimi**: se un file legittimamente vuoto (ad esempio un `__init__.py`) viene salvato nel checkpoint, la condizione `f.stat().st_size == 0` lo scambia per un "file non esistente prima" e **cancella permanentemente il file originale dal disco!**

---

### 2.4 `harness/tools/__init__.py` e `harness/headless.py`

* **Import inesistente**:
  * In [`harness/tools/__init__.py`](file:///E:/Projects/basalt/harness/tools/__init__.py#L15), linea 15: `from ..context import mask_output` — `mask_output` **non esiste** in `context.py`. L'intero pacchetto `harness.tools` non può essere importato.
* **Nome del dizionario strumenti sbagliato**:
  * In `harness/tools/__init__.py` il dizionario è dichiarato come `TOOL_BY_NAME`.
  * In [`harness/headless.py`](file:///E:/Projects/basalt/harness/headless.py#L19), linea 19: `from .tools import TOOL_REGISTRY`. `TOOL_REGISTRY` non esiste, causando `ImportError` al lancio di `basalt-agent`.

---

### 2.5 `harness/integrations/wiki_client.py`

* **Simbolo mancante nel package**:
  * In [`harness/integrations/__init__.py`](file:///E:/Projects/basalt/harness/integrations/__init__.py#L7):
    `from .wiki_client import WikiBridge, fetch_docs`
  * In [`harness/integrations/wiki_client.py`](file:///E:/Projects/basalt/harness/integrations/wiki_client.py): `fetch_docs` non è mai stato scritto. Questo rompe direttamente il test unitario `test_client_imports`.

---

### 2.6 `harness/swarm/swarm.py`

* **Import da pacchetto inesistente**:
  * Linea 25: `from . import __version__ as SWARM_VERSION`.
  * La cartella `harness/swarm/` non possiede nemmeno un file `__init__.py`. L'import solleva `ImportError: cannot import name '__version__' from 'harness.swarm'`.
* **Esecuzione fittizia**:
  * La funzione `run(swarm_id)` non avvia alcun thread o processo: si limita a ciclare i membri del file JSON e restituire un dizionario con `"status": "ready"`.

---

### 2.7 `harness/payments_broker.py` e `harness/secrets_broker.py`

* **Bug di sicurezza paradossale nella revoca dei pagamenti**:
  * In [`harness/payments_broker.py`](file:///E:/Projects/basalt/harness/payments_broker.py#L43-L45):
    
    ```python
    def revoke(self) -> None:
        """Cancel all standing allowances immediately."""
        self.spent = 0.0
    ```
    
    Anziché bloccare le autorizzazioni o azzerare il cap, imposta `self.spent = 0.0`, il che **consente all'agente di spendere di nuovo l'intero budget**!
* **Vault dei segreti in chiaro**:
  * In [`harness/secrets_broker.py`](file:///E:/Projects/basalt/harness/secrets_broker.py#L26), i segreti vengono scritti in file di testo in chiaro non cifrati in `~/.basalt/secrets/`. Il protocollo di broker cifrato e isolato descritto in §4.8 non esiste.

---

### 2.8 `harness/mcp_client.py` (Model Context Protocol)

* **Violazione del protocollo JSON-RPC 2.0 (Deadlock)**:
  * A linea 81: `payload = json.dumps({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params})`.
  * **Manca completamente il campo `"id"`!**
  * Nella specifica JSON-RPC 2.0, un messaggio senza `"id"` è considerato una **Notifica** e il server MCP ha l'obbligo di **non inviare alcuna risposta**.
  * Di conseguenza, a linea 89: `line = self._proc.stdout.readline()` **rimane bloccato all'infinito** in attesa di dati che non arriveranno mai.

---

### 2.9 `harness/sandbox/__init__.py`

* **Nessun isolamento reale su Windows**:
  * Il modulo esegue un banale `subprocess.run(command, shell=True)` filtrando su `DEFAULT_DENY = ("reg", "schtasks", "sc", "netsh", "runas")`.
  * Poiché `shell=True` usa `cmd.exe`, qualsiasi combinazione (es. `powershell -c ...`, `echo x & netsh`, o script `.bat`) aggira il controllo all'istante. Non vengono utilizzati né token di sicurezza ridotti (`CreateRestrictedToken`), né Job Objects, né Deno o container Windows.

---

### 2.10 `serve/web/` e `serve/web/index.html` (Interfaccia Grafica e Canvas)

* **Regressione grave: rimozione della vista di log (`reqlog`)**:
  * In `serve/web/index.html`, OpenCode ha eliminato i controlli nativi di `reqlog` (`rl-pause`, `rl-clear`, `rl-save`, ecc.) sostituendoli con sezioni duplicate `<section class="pane" id="changes-pane">`. Questo ha rotto il test di sistema `serve/tests/test_reqlog_page.py`.
* **Crash JavaScript a runtime in `changes.js`**:
  * In [`serve/web/changes.js`](file:///E:/Projects/basalt/serve/web/changes.js#L24): `journal.pinned_blocks.map(...)`.
  * In [`serve/web/changes.json`](file:///E:/Projects/basalt/serve/web/changes.json): la chiave `pinned_blocks` non esiste affatto. In console si genera sistematicamente `TypeError: Cannot read properties of undefined (reading 'map')`.
* **Falso Canvas e Falso DAG Viewer**:
  * In [`serve/web/canvas.js`](file:///E:/Projects/basalt/serve/web/canvas.js#L76-L86) il canvas non è una tela interattiva né un grafo a nodi: concatena semplicemente i valori `tok_s` come stringa di testo (`"15.2 → 16.1 → 14.8"`).
  * In [`serve/web/dag_viewer.js`](file:///E:/Projects/basalt/serve/web/dag_viewer.js#L43-L45) il visualizzatore DAG non disegna un grafo di dipendenza tra tool call, ma mostra gli ID delle richieste HTTP concatenati con una freccia `→`.

---

## 3. Confronto di Completezza rispetto a `the_way_to_the_moon.md`

### Fase 0 — Misura & Baseline (Richiesto: L)

* **Requisiti documento**: Benchmark riproducibile su SWE-bench Pro / Terminal-Bench / BFCL, runner k>=5 con metriche hardware e hit-rate da `/metrics`, confronto a modello fisso contro altri harness (Claude Code, OpenCode, Goose, Cline).
* **Cosa c'è nel codice**: `harness/eval/runner.py` contiene un loop su un file JSON (`sets/core.json`) con 8 task giocattolo ("Somma 17 + 25"). Nessuna suite di benchmark reale, nessun confronto con altri harness. Il runner è rotto a causa di import inesistenti.

### Fase 1 — Ciclo Agentico Minimo (Richiesto: L)

* **Requisiti documento**: `agent_loop.py` con intercettazione tag `<tool_call>`, schema validation con repair tramite `repair.py`, reiniezione `role: "tool"`, cycle detector, cancellazione e budget.
* **Cosa c'è nel codice**: File presente ma non funzionante. Repair di `repair.py` non integrato; `_is_repeat` non implementato; gestione delle cancellazioni incompleta; tool limitati a 3 funzioni con gestione path fragile su Windows.

### Fase 2 — Sicurezza e Controllo (Richiesto: XL)

* **Requisiti documento**: Permessi a 3 livelli, plan mode, hook pre/post, checkpoint/rewind dei file, audit ledger a catena di hash. Livello 4 (rinuncia legale con dialoghi di conferma, prova crittografica, broker pagamenti non aggirabile dal prompt). Suite di test per prompt injection indiretta.
* **Cosa c'è nel codice**: Codice duplicato e disallineato. Catena di hash dell'audit ledger rotta (`_hash` mancante). Checkpoints con gravi rischi di cancellazione file. Broker pagamenti con metodo di revoca errato e chiamate con argomenti mancanti. Nessun test avversariale per prompt injection.

### Fase 3 — Contesto, Memoria, Skill (Richiesto: L)

* **Requisiti documento**: Assemblaggio context append-only, masking deterministico 40/15/20 prima della sintesi (TokenJuice), offload su file. Catalogo skill con parsing YAML e hash di verifica. Memoria persistente SQLite FTS5.
* **Cosa c'è nel codice**: Classe `Memory` creata per 3 volte in 3 file diversi. Il masking di `context.py` inserisce una stringa fissa placeholder senza sintetizzare. `mask_output` non è stato implementato. Le skill caricano solo testo senza gestione di sicurezza né hash.

### Fase 4 — Interoperabilità (Richiesto: M)

* **Requisiti documento**: Client MCP (stdio + Streamable HTTP) con rilevamento, OAuth, deferral dei tool. Agente ACP su stdio compatibile con Zed/JetBrains e supporto `diff`. Entry point `basalt-agent -p` headless con output JSON.
* **Cosa c'è nel codice**: Client MCP bloccato su `readline()` a causa della mancanza dell'`id` JSON-RPC. `acp_agent.py` è un mock scollegato dall'agente che inoltra solo una chat singola a HTTP. `headless.py` non si importa (`TOOL_REGISTRY` inesistente).

### Fase 5 — Interfaccia Grafica e Canvas (Richiesto: L)

* **Requisiti documento**: Dashboard DAG dei task, visualizzatore di artefatti e diff con hunk apply/reject, canvas a nodi, branching non lineare, accessibilità mobile/desktop.
* **Cosa c'est nel codice**: Canvas e DAG ridotti a stringhe di testo con frecce. Nessun visualizzatore diff né applicazione hunk. I file JS fanno polling su `/metrics` ma i file JSON collegati non contengono i dati attesi, scatenando eccezioni. Rimossi i controlli originali di `reqlog` in `index.html`.

### Fase 6 — Conoscenza & RAG/CAG (Richiesto: M)

* **Requisiti documento**: Client wikibridge con contratto e cache con timestamp, benchmark quantitativo RAG vs CAG vs baseline su domande reali.
* **Cosa c'è nel codice**: `wiki_client.py` ha un fallback elementare ma il package fallisce l'import (`fetch_docs` assente). Nessun confronto o implementazione CAG.

### Modulo Planner & Jev (`conquer_of_the_moon.md`)

* **Requisiti documento**: `PlanDAG`, `JevTriageGatekeeper`, `CallAPICallClient`, `PlanCanvasBridge`, integrazione TypeSafe Jev.
* **Cosa c'è nel codice**: **Zero righe di codice.** Solo file di log o prove sperimentali isolate (`poc_jev_triage.py`), ma nessun componente integrato nel runtime o nel server.

---

## 4. Conclusioni e Raccomandazioni Operative

1. **Il branch `OperCode_Basalt` non è pronto per il merge né per l'uso.**
   Se venisse integrato o avviato, causerebbe la rottura dei test del server (`serve/tests`) e il mancato avvio del sottosistema agentico.
2. **Cosa salvare del lavoro:**
   * Le specifiche di `the_way_to_the_moon.md` e `conquer_of_the_moon.md` rimangono ottime e dettagliate.
   * L'architettura modulare proposta (`harness/`, `serve/web/`) è corretta, ma deve essere popolata con codice reale, testato e funzionante.
   * Lo script di packaging `compile_and_package.ps1` e i file di installer possono essere riutilizzati dopo verifica.
3. **Piano di bonifica raccomandato:**
   * **Passo 1 (Ripristino integrità `serve/web`)**: Ripristinare in `serve/web/index.html` la sezione `view-logs` originale per sanare `test_reqlog_page.py`, e correggere i crash in `changes.js`.
   * **Passo 2 (Sanificazione `harness/`)**: Unificare i moduli duplicati (`permissions.py` vs `safety.py`, `memory.py` vs `skills.py`), correggere tutte le firme dei metodi (`AuditLedger.record`, `_hash`), definire `system_blocks` e `mask_output`.
   * **Passo 3 (Completamento reale Fase 0 e 1)**: Rendere il ciclo `Agent.run()` realmente eseguibile e testabile con un mock engine, prima di procedere a canvas e interoperabilità.
