# Coda dei test da fare di giorno, con l'utente al computer

**Stato (2026-10-24, limite di potenza 600 W):** voci 1 e 2 provate dal vivo (la 2 solo fino all'API del Monitor, non all'occhio sulla pagina), voce 3 a metà, le altre in attesa. Servono il modello vero caricato e la GPU libera, quindi non si lanciano senza l'utente.
Le chiamate telefoniche reali hanno la loro coda: `tests/QUEUE_live_calls.md`.

Prima di cominciare: caricare il modello dal Loader, annotare il limite di potenza della GPU (ogni numero va etichettato
con quel valore) e avviare il server su 127.0.0.1.

| # | Cosa si fa | Cosa si guarda | Comando o percorso | Stato |
|---|---|---|---|---|
| 1 | Una corsa vera dell'agente con Qwen su una cartella di prova | che chiami gli strumenti con argomenti validi; che compiti e modifiche compaiano nel Monitor mentre lavora | `python -m harness -p "crea hello.py che stampa ciao" --workspace D:\prova --permission-mode auto` | FATTO: 3 turni, 7 s, argomenti validi, cache 63%. Compare nel Monitor |
| 2 | Revisione di una modifica vera dal Monitor | Tieni / Annulla per blocco su una modifica scritta da Qwen; il file su disco torna com'era | dopo il punto 1, con un file di almeno 40 righe da modificare in due punti | FATTO via API: il modello ha usato `sed` e la modifica non compariva; ora si registra (commit 73d8301c); annullato il blocco 2, file coerente. DA FARE a occhio nella pagina |
| 3 | Valutazione `core`, k=5 | pass^k, validità delle chiamate, turni, token, cache; leggere le tracce dei compiti che falliscono sempre (spesso è il compito) | `python -m harness.eval --set core --k 5 --gpu-power-limit-w <W> --out core.md --json core.json --keep-workspaces D:\eval-ws` | A METÀ: 8/10 pass^k, ma `leave_other_files_alone` è fallita per «RAM low» del server (non per il modello) e `write_function` 2/5 perché i comandi concatenati venivano rifiutati senza spiegazione (corretto il messaggio). Da rifare con `--only` quando il server risponde |
| 4 | Valutazione `toolcalls`, k=5 | argomenti numerici e booleani con il tipo giusto; nessuna azione eseguita su istruzione dentro un risultato | `python -m harness.eval --set toolcalls --k 5 --gpu-power-limit-w <W>` | in attesa |
| 5 | Confronto `knowledge`: nessun documento, RAG, CAG | accuratezza, token letti, cache per ciascuno; da qui si sceglie la soglia di `choose_strategy` (oggi un quarto del contesto, solo un punto di partenza) | `python -m harness.eval --set knowledge --k 3 --strategies none,rag,cag --gpu-power-limit-w <W>` | in attesa |
| 6 | L'app vera (`BasaltWinGUI.exe`) sullo schermo da 3600 px | zoom automatico, Ctrl+K, Alt+1…7, colonna di compiti e modifiche, leggibilità del testo a distanza normale | avviare con `Avvia-Basalt.ps1`; l'audit automatico non sostituisce l'occhio | in attesa |
| 7 | Planner con il modello vero | che il rieseguire il piano (System 2) produca alternative che si applicano pulite; il filtro cloud resta spento salvo consenso | vista Piani, "Giudica" su un evento di prova | in attesa |
| 8 | Agente in un editor via ACP (solo se lo usi) | prompt, richieste di permesso, modifiche come diff | `python -m harness.acp_agent --workspace <cartella>` configurato nell'editor | in attesa, facoltativo |

## Regole

- Le valutazioni sono prove di sviluppo, non numeri da citare: i numeri di punta li produce il maintainer con le sue corse.
- Una differenza sotto i 3 punti tra due corse è rumore finché non si ripete.
- `--permission-mode auto` solo in una cartella che si può perdere (non c'è isolamento di file e rete).
- Non si lancia nulla che occupi la GPU mentre l'utente non è al computer.

## Nota del 2026-10-24: il server rifiuta le richieste per poca RAM

Dopo la valutazione `core` il server risponde «RAM low (23182 MiB free < 23187 MiB guard): request stopped; lower
--resident-budget-gib»: la RAM libera della macchina sta sulla soglia di protezione (basalt usa 69 GB di 127). Finché non si libera
memoria o non si abbassa `--resident-budget-gib` (impostazione del server, da scegliere con l'utente) le prove col modello non partono.
