# Progetto: creare una LoRA dalla scheda Knowledge

Stato: **solo progetto** (23 novembre 2026), nessun codice scritto. Scopo deciso dall'utente: una funzione in più dentro TaDaMoo, non
un sistema di addestramento fine. Tutto automatico, risultato «abbastanza buono», stessa LoRA che il motore già sa usare.

## Che cosa vede la persona

Nella scheda **Knowledge**, accanto agli archivi, una sotto-scheda **LoRA** (per progetto, come gli archivi).

1. **Nuovo set LoRA**: un nome e una descrizione.
2. **Materiale**: si scelgono archivi del progetto già letti (con i loro passaggi), si trascinano file (stessi formati degli
   archivi) e si può scrivere testo libero. Non c'è un lettore nuovo: i file passano dalla stessa lettura e taglio dei documenti.
3. **Prepara** (automatico): un modello scrive il materiale di addestramento. La pagina mostra il conteggio e un campione.
4. **Stima**, prima di partire: token, passi, ore su questo PC, spazio su disco, memoria che servirà. Un solo pulsante
   **Addestra**, con la stima scritta sul pulsante.
5. **Avanzamento**: passo, perdita, tempo che resta, **Annulla**. Dice chiaro che durante l'addestramento il modello locale non
   risponde (vedi «Memoria»).
6. **Attiva / Disattiva**: la LoRA entra nel motore al prossimo caricamento. Una barra per la scala (1,0 di partenza).

7. **Verifica finale** (voluta dall'utente il 23 novembre): quando l'adapter è pronto, la pagina mostra la perplessità con e senza
   adapter (vedi «Verifica della qualità»). Insieme alla curva di perdita che l'addestratore stampa già.

## Come si prepara il materiale

Prova che ha funzionato: 24 fatti, ognuno in tre formulazioni (domanda e risposta, frase semplice, «per chi studia…»), ripetuti
due volte, 16,7 KB, 70 pezzi da 128 token.

- Ogni passaggio dell'archivio diventa: una frase semplice (il passaggio stesso, accorciato), da una a tre coppie
  «Domanda: … Risposta: …» e una riformulazione. Le domande e i riassunti già scritti dalla descrizione per modello
  (`tadamoo/rag/enrich.py`) si riusano se ci sono: niente seconda chiamata.
- Dove serve una chiamata, usa la sessione tecnica, funzione nuova `lora_prepare` (come `rag_enrich`: stesse regole sui
  provider fuori dal PC, vale `cloud_ok` dell'archivio). Un passaggio per volta, a pacchetti, ripresa possibile.
- **Ogni fatto in inglese e nella lingua del documento** (idea dell'utente, 23 novembre): il modello ragiona in inglese e un legame
  imparato solo con le parole italiane («Città» – Valdoria) non viene trovato quando cerca con quelle inglesi («city»). Quindi
  la preparazione scrive ogni fatto nelle due lingue, e in più forme: frase semplice, domanda e risposta, forma di chat.
  Il nome proprio resta com'è; le parole comuni attorno al nome (città, squadra, fondatore) compaiono nelle due lingue.
- Senza modello (nessun provider disponibile): il passaggio entra com'è, senza domande. Peggio, ma parte lo stesso.
- Si tolgono i passaggi sotto 60 caratteri o quasi tutti numeri (come l'enrich) e i duplicati.
- Il file finale è un testo con paragrafi separati da una riga vuota, nel formato che l'addestratore già legge.
- **Tetto**: il costo è nei passi (vedi sotto). Il tetto di partenza è di circa 70 pezzi da 128 token per epoca; se il materiale è
  più grande la pagina dice quanto ne entra e quanto verrà lasciato fuori (si tengono prima i passaggi con più fatti).

## Quanto costa (misure del 23 novembre, sul PC dell'utente)

- Addestratore su CPU, 24 thread, contesto 128: **circa 76 secondi per passo**. Prova reale: 264 passi (66 per epoca, 4 epoche)
  = 5 ore e 33 minuti.
- Il tempo è proporzionale a passi × epoche. Raddoppiare il materiale a parità di epoche raddoppia il tempo.
- Non misurato: se un passo con contesto più lungo (512, 1024) costa quasi uguale. Se sì, il tempo per token cala molto. Va
  misurato **prima** di scrivere la scheda, perché cambia il tetto e la stima.
- Backward su GPU non provato: oggi l'addestramento è solo CPU.
- Disco: adapter 7,3 GB con tutti i moduli (rango 8); alla prima attivazione il motore scrive anche una copia fusa degli esperti,
  circa 61 GiB per il pack IQ4_XS, accanto all'adapter. La stima somma entrambi e controlla lo spazio libero prima di partire.
  Un adapter senza esperti instradati sarebbe molto più piccolo e senza copia da 61 GiB: da offrire come «leggera» dopo averne
  misurato tempo e resa.

## Memoria e esclusività

L'addestratore carica il GGUF intero (87 GB) in RAM, il motore ne tiene circa 95 GB di commit: **non stanno insieme**.
Prima di partire la scheda scarica il modello locale con lo stesso meccanismo di cessione già usato da avatar e OCR
(`docs/engine-vram-arbiter.md`), lo dice alla persona e, a fine lavoro (o su Annulla), lo ricarica com'era. Nel frattempo le
conversazioni possono usare un provider esterno, se la persona ne ha uno.

## Verifica della qualità

Alla fine dell'addestramento, sempre, senza che la persona la chieda: la **perplessità** (la misura che si calcola in fretta
su poche migliaia di token) con e senza adapter, su due testi.
- **Tenuto da parte**: circa il 10% del materiale preparato, non usato per addestrare. Deve calare: dice se ha imparato.
- **Controllo generale**: un breve testo fisso in italiano e uno in inglese. Non deve salire di molto: dice se ha perso qualcosa.
La pagina scrive i due numeri prima e dopo e una riga in parole («ha imparato», «ha perso qualcosa del resto»). Si calcola con il
motore, cioè con l'adapter fuso come lo userà la persona; lo strumento esatto (`basalt-bench --quality` o il calcolo del
fork) si decide in fase 2, dopo aver guardato che cosa accetta `--lora`.

## Il modello su cui si addestra

L'addestratore legge un **GGUF**, non un `.basalt`: serve il GGUF del modello di base (87 GB, 4 pezzi) su disco, **su D:**
(decisione del 23 novembre). **Mai su E:**: ci vive il file di paging, che cresce fino a 100 GB, e se E: si riempie il computer
si blocca e può non ripartire. Nessun file di questa funzione va su E:: GGUF, `.basalt` convertito, adapter e copia fusa degli
esperti stanno tutti su D:. Una impostazione nella sotto-scheda dice dove sta il GGUF. La LoRA vale per i pesi su cui è stata addestrata; il manifest segna il modello
di base e la scheda elenca solo i pack compatibili. Se la LoRA valga anche sul pack «Uncensored» già in uso lo dice la prova di
inferenza del 23 novembre (task 18): finché non c'è, la scheda avvisa.

## Dove sta e come si chiama

- `tadamoo/lora/`: `prepare.py` (materiale → testo), `plan.py` (stima), `train.py` (avvia e legge il processo, analizza il registro
  dell'addestratore), `registry.py` (set e manifest: fonti, parametri, perdita, data, modello di base, impronta).
- `serve/lora_api.py`, rotte `/api/lora/*` (set, fonti, prepara, stima, addestra, annulla, stato, attiva). I lavori lunghi usano
  gli stessi `Jobs` dei lavori degli archivi (`serve/rag_jobs.py`).
- `serve/web/lora.js` e `lora.css`; chiavi di traduzione `lo.*`, id `lo-*` (liberi, controllati il 23 novembre).
- File: `lora/<progetto>/<set>/{train.txt,adapter.gguf,manifest.json,log}`.
- «Attiva» scrive la voce `lora-scaled` nella configurazione del motore; il motore la fonde al caricamento (15-18 s per le matrici
  dense). Un solo adapter attivo per volta in questa prima versione; il motore ne accetta più d'uno, ma ognuno si porta una
  copia da 61 GiB.
- L'addestratore (fork QVAC patchato, `tools/lora_training/`) oggi si compila a mano in una cartella temporanea: va portato in una
  ricetta di compilazione nel repository e va controllata la sua licenza prima di distribuirlo.

## Che cosa una LoRA non è (da dire nella scheda)

Per i **fatti** una LoRA non sostituisce l'archivio: non si aggiorna quando i documenti cambiano (si riaddestra, ore), non cita la
fonte e può inventare cose vicine ai fatti imparati. Serve per vocabolario, stile e conoscenza stabile del progetto. Per ora è
provata solo su 24 fatti inventati; la prova di inferenza dirà se li ha imparati e se ha perso qualcosa del resto.

## Fasi

0. Finire la prova su Basalt (task 18) e misurare il costo di un passo a contesto 128, 512, 1024.
1. Preparazione e stima (senza addestrare): `prepare.py`, `plan.py`, rotte, test.
2. Esecuzione: `train.py`, cessione del modello, annulla e ripresa, registro.
3. La sotto-scheda LoRA, provata nell'applicazione vera a 360, 390 e 1280 px.
4. Attiva/Disattiva con ricarica del motore.

## Decisioni dell'utente (23 novembre)

- Il GGUF di base resta su disco in modo fisso, su D:, mai su E:.
- Il modello locale può tacere per ore durante l'addestramento.
- Nessun limite massimo di tempo per un addestramento lanciato dalla scheda (resta la stima scritta sul pulsante).
- Una LoRA attiva, caricata e fusa all'avvio. In seguito una seconda, **dinamica**, caricata durante l'uso in tempo reale
  (task «Caricamento dinamico per sessione», nella mappa).
- La verifica della qualità alla fine c'è (sezione sopra).
