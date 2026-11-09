# Coda dei test con chiamate telefoniche reali

**Stato: tutti IN ATTESA. Nessuna chiamata reale è stata fatta.**
Vanno eseguiti solo dopo l'autorizzazione esplicita dell'utente, di giorno, con un numero scelto da lui.

## Come si eseguono (solo dopo il via)

```powershell
$env:TADAMOO_LIVE_CALLS = "1"                 # senza questa variabile i test restano saltati
$env:TADAMOO_LIVE_CALL_NUMBER = "+39..."      # numero di prova scelto dall'utente
$env:CALLAPICALL_TOKEN = "<control_auth_token di callAPIcall>"
python -m pytest tests/test_live_calls.py -v -p no:cacheprovider
```

callAPIcall deve essere avviato (`C:\projects\callAPIcall`, porta di controllo 8770) con il modem o il telefono collegato.

## Test in coda

| # | Test (`tests/test_live_calls.py`) | Cosa verifica | Rischio | Stato |
|---|---|---|---|---|
| 1 | `test_service_reachable_and_authenticated` | il servizio risponde sulla porta di controllo e accetta il token (nessuna chiamata) | nessuno | in attesa del via |
| 2 | `test_single_call_with_ai_disclosure_and_hangup` | una chiamata al numero di prova con la frase iniziale che dichiara l'IA, trascrizione letta, chiusura | squilla un telefono reale | in attesa del via |
| 3 | `test_say_and_hear_roundtrip` | una battuta pronunciata e una risposta ascoltata dal chiamato | squilla un telefono reale | in attesa del via |
| 4 | `test_hangup_is_idempotent` | un secondo `hangup` su una chiamata già chiusa non fallisce né richiama | nessuno dopo la chiamata 2 | in attesa del via |
| 5 | `test_agent_places_call_only_after_approval` | l'agente col modello vero chiede l'approvazione prima di `phone_call` e rispetta un rifiuto | squilla solo se approvi | in attesa del via |
| 6 | Servizio vocale, chiamata a mano dalla GUI su modem GSM (`telephony.transport` = gsm) | il modem si apre solo a telefonia accesa, la prima frase dice che è un'IA, il registro `call-audit.jsonl` ha una riga di inizio e una di fine con il numero mascherato | squilla un telefono reale | in attesa del via |
| 7 | Servizio vocale, chiamata a mano su telefono Android (APK `android/dialer`, token inserito sul telefono e in `TADAMOO_PHONE_TOKEN`) | il telefono compone, l'audio passa dal Bluetooth, la prima frase dichiara l'IA | squilla un telefono reale | in attesa del via |
| 8 | Chiamata di un programma in modalità `manual` e in `auto` | 202 con `approval_id`, approvazione in Impostazioni > Voce, poi la chiamata parte una volta sola; in `auto` parte subito e il registro lo dice | squilla solo se approvi | in attesa del via |
| 9 | Risposta in arrivo accesa (`incoming_auto_answer`), con il numero di prova che chiama il modem o il telefono | l'IA risponde con la frase che dichiara l'IA; spenta, la chiamata resta in attesa e il registro scrive `auto_answer_off` | squilla il telefono di prova | in attesa del via |
| 10 | Numeri di emergenza (solo rifiuto, nessuna linea toccata) | 112, 118, 113, 911 rifiutati con 403 da `/call` e `/external/call` | nessuno | provato con finti, da ripetere con la linea vera spenta |

## Regole

- Un solo numero, scelto dall'utente; mai numeri di terzi o di servizi pubblici.
- Mai di notte e mai in orari che disturbino chi risponde.
- Ogni chiamata dichiara di essere un assistente IA (art. 50 AI Act). I test controllano la frase prima di comporre.
- Niente tentativi automatici dopo un timeout: si guarda prima lo stato della chiamata.
- Tutto ciò che non richiede una linea vera (contratto del client, permessi, rifiuto dell'approvazione) è già coperto da `tests/test_integration_contracts.py` con un servizio finto.
