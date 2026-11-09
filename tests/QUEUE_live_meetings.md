# Coda dei test con riunioni reali

**Stato: tutti IN ATTESA. Nessuna riunione vera è stata aperta, nessun browser è stato avviato verso una riunione, nessun audio è stato catturato.**
Vanno eseguiti solo dopo l'autorizzazione esplicita dell'utente, di giorno, in una riunione di prova creata da lui con almeno un'altra persona (o un secondo account).

## Prima di cominciare (a cura dell'utente)

- Playwright con il suo Chromium installati (`pip install playwright`, `python -m playwright install chromium`); il servizio vocale acceso con l'STT caricato.
- In Impostazioni > Voce > Riunioni: «Per conto di chi» compilato. Per la voce: un cavo audio virtuale installato (per esempio VB-CABLE), il suo lato di ingresso scelto come microfono della riunione.
- Il registro da guardare dopo ogni prova: `meetings-audit.jsonl` accanto a `voice.json`.

## Test in coda

| # | Cosa si fa | Cosa si verifica | Stato |
|---|---|---|---|
| 1 | Ingresso come ospite in una Meet di prova (spunta del diritto, parola spenta) | l'annuncio compare nella chat in inglese e italiano; la sala d'attesa e l'ammissione funzionano; il registro ha `join` e `announced` | in attesa del via |
| 2 | Dopo l'ingresso, un partecipante scrive «stop» nella chat | il bot esce subito, scrive l'addio, non resta nessuna cartella di riunione, il registro ha `objection` | in attesa del via |
| 3 | Un partecipante dice «stop» a voce | come il 2, e le parole dette prima non compaiono da nessuna parte | in attesa del via |
| 4 | L'organizzatore rimuove il bot durante la finestra, poi dopo il consenso | in entrambi i casi esce e cancella; il registro ha `removed` | in attesa del via |
| 5 | Nessuna obiezione per tutta la finestra, poi premi Esci | la cartella nasce solo dopo la finestra; c'è la trascrizione; il verbale si scrive; «Cancella» toglie tutto | in attesa del via |
| 6 | Modo «esplicito»: prima nessun «ok», poi un «ok» | senza «ok» esce al doppio della finestra e cancella; con «ok» resta | in attesa del via |
| 7 | Qualcuno entra a riunione iniziata | arriva un annuncio breve nella chat | in attesa del via |
| 8 | Gli stessi test 1, 2 e 5 su Teams e su Zoom | i selettori delle due piattaforme (ingresso, chat, uscita, partecipanti) reggono; se no si correggono in `adapters/` | in attesa del via |
| 9 | Parola accesa con il cavo virtuale; senza cavo | con il cavo: annuncio a voce, risposte solo se chiamato per nome, mai sopra un altro; senza: il messaggio «no virtual microphone found...» e nessuna voce | in attesa del via |
| 10 | Avatar caricato con la camera virtuale accesa, scelta come camera della riunione | le labbra seguono la voce; l'avatar si vede come camera | in attesa del via |
| 11 | Copilota (Maiusc+F4) durante una chiamata, con la condivisione dello schermo accesa | la finestra dei suggerimenti non compare nella condivisione; si vede solo da te; «Rispondi con audio» passa dal cavo | in attesa del via |
| 12 | Calendario con una tua app OAuth (Google, poi Microsoft) e un evento spuntato | l'ingresso un minuto prima, con lo stesso annuncio e consenso; i token stanno solo nel gestore credenziali | in attesa del via |
| 13 | Linux (PulseAudio, v4l2loopback), se mai | cattura dal monitor e voce nel microfono virtuale; scritto, non provato | in attesa del via |
