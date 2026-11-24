# Il Basalt originale (jesdga95/basalt) contro il nostro, 24 novembre 2026

Fotografia presa con `git fetch upstream` (sola lettura). Ogni riga dice se è **verificato** (letto nei file o nei comandi di git) o **plausibile, non provato** (con la prova più economica).

## Dove siamo

- Il loro `main` è alla 0.5.7 (23 novembre): 3.528 commit, 1.477 senza unioni dopo il loro commit `4a591d96` (27 ottobre). Verificato.
- **La nostra storia e la loro non hanno più un antenato comune** (`git merge-base` non trova niente: la storia nostra è stata riscritta). Un `git merge` non è possibile: si porta il lavoro come una serie di modifiche tra un commit di base e il loro `main`, applicate con tre vie. Verificato.
- La base che i nostri file ricordano è fine ottobre: il nostro registro delle versioni (`docs/changelog/`) si ferma alla 0.2.0 (1 novembre); il nostro `log.hpp` è identico a quello del loro 23 ottobre. Verificato. Manca quindi tutto da 0.2.1 a 0.5.7: undici versioni.
- Dal confronto con `4a591d96`: 1.414 file cambiati da loro, 247 cambiati da tutti e due (72 in `src`, 36 in `include`, 46 in `serve`, 43 in `tests`, 23 in `docs`, 15 in `tools`). Verificato.
- **Hanno cambiato la forma del progetto** (0.3.0): il codice di ogni modello sta in `src/models/<modello>`, e sono comparsi `src/model`, `src/dense` e `src/artifact`; da noi c'è ancora `src/qwen35` e `src/plan`. Il lavoro nostro sul motore (la fusione delle LoRA, i quantizzatori IQ) andrebbe spostato nella nuova disposizione. Verificato.

## Cosa varrebbe la pena prendere (dai loro registri delle versioni; numeri loro, non misurati da noi)

1. **Compilazione molto più corta** (0.3.2): una compilazione pulita passa da 933 a 290 secondi (`-j32`), spezzando `dec_moe.cu` in quattro parti compilate in parallelo. Costo basso, alto per noi che ricompiliamo spesso.
2. **Memoria e avvio più robusti** (0.3.1, 0.3.2, 0.4.1): un solo margine di RAM e un solo controllo per ogni modello (`--ram-reserve-mib`), un avvio che non fa più andare la macchina in swap mentre carica (#285), il piano di RAM di Flash-Next che torna a lasciare spazio su 64 GB, il motore che si rifiuta di partire senza i profili degli esperti. Plausibile, non provato che aiutino la macchina di qui; prova più economica: un caricamento con la loro versione e lo stato della memoria.
3. **Lettura dei prompt lunghi di Flash-Next più veloce** (0.3.3): con `--parallel 4` su una scheda, 12%, 20% e 22% in meno a 40k, 100k e 250k token. Va misurata con A/B e controllo di qualità (regola di AGENTS.md).
4. **Slot paralleli che decodificano insieme** (0.4.0, 0.5.6), fino a 8 righe per passaggio, e un solo trasferimento in RAM bloccata per i checkpoint di ogni modello.
5. **Ingressi nuovi sul motore** (0.3.3): video su Flash-Next (NVDEC), PDF e file Office dentro i messaggi sulle tre API, checkpoint dei media (la stessa immagine una seconda volta: 0,37 s invece di 1,98 s), logprobs, `answer-room`. Riguardano anche il task 45 (programmi esterni) e quello che il README dice di Claude Code.
6. **Sicurezza delle immagini per richieste da altre macchine** (0.2.1): da fuori solo `data:` URL, salvo host e cartelle nominati. Il nostro server espone la stessa API: da guardare.
7. **Qualità misurata contro un riferimento BF16** (0.2.1 e seguenti): da noi AGENTS.md chiede il Q8_0.
8. **Modelli nuovi:** Qwen3.8-27B denso (0.4.0: 16 GiB su una scheda, 262 token al secondo con il drafter, YaRN oltre 262.144), DeepSeek-V4.1-Flash, Hy4-flash, Inkling 2 Mini. Il 27B è l'unico che tocca il nostro uso: nella scheda e con poca RAM. Plausibile, non provato che tolga la pressione su memoria e file di paging; prova più economica: scaricare il checkpoint e misurare il commit.

## Cosa rompe (0.5.7, 0.3.1, 0.3.0)

- `gpu`, `gpu-mode` e `gpu-share` sostituiscono la logica multi-scheda di ogni modello; `gpu: auto` non c'è più. Il nostro Loader e il supervisore passano questi argomenti: vanno adattati.
- Formato di configurazione cambiato (0.3.0), `--vision-mmproj` tolto, `--kv` con valori diversi per modello, `--no-answer-room` diventa `--answer-room 0`. Il nostro Loader, il supervisore e `benchrun` passano questi argomenti.

## Ordine proposto

1. Fase 0: un ramo di prova in una copia di lavoro su D: (non su E:), con le modifiche tra `4a591d96` e il loro `main` applicate a tre vie; si vede quanti conflitti escono davvero.
2. Fase 1: il costo basso (1, 2, 6, 7 e le correzioni).
3. Fase 2: velocità e ingressi (3, 4, 5), ognuno con A/B e controllo di qualità.
4. Fase 3: la rottura delle opzioni multi-scheda e, se vuoi, il Qwen3.8-27B.

## Correzione della base (24 novembre, più tardi)

La base giusta non è `4a591d96` ma il loro tag **v0.2.0** (1 novembre): da `src` e `include` nostri a v0.2.0 ci sono 102 file diversi con 9.112 righe aggiunte e 247 tolte, cioè il nostro motore è v0.2.0 più il nostro lavoro. Con quella base l'unione a tre vie di tutto il loro `main` dà 97 conflitti (86 di contenuto, 10 modify/delete, 1 add/add), in `src/program` (12), `src/models` (7), `include` (7), `src/kernels` (3), `src/core` e `src/platform` (2), prove e server (circa 20) e documentazione (circa 7). Verificato con `git merge-tree`, che non ha toccato niente.
