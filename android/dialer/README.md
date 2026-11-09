# Call API Controller — app Android di controllo chiamate

App Android che fa da telecomando per le chiamate cellulari del telefono
gateway. Espone una piccola REST API sulla LAN; il call-bridge sul PC la usa
per comporre/rispondere/riagganciare e per leggere lo stato della chiamata.

**Non tocca mai l'audio.** L'audio della chiamata viaggia separatamente via
Bluetooth HFP verso il PC. Questa app si occupa solo del *controllo*.

## App telefono predefinita (dialer)

L'app si registra come **dialer predefinito** tramite un `InCallService`.
Questo le da' accesso pieno e affidabile a:
- stato per-chiamata (RINGING / DIALING / OFFHOOK / IDLE)
- **numero dell'interlocutore** (serve all'orchestrator per la logica
  "prefisso internazionale → lingua" sulle chiamate in arrivo)
- risposta/riaggancio tramite gli oggetti `Call`

Non viene mostrata alcuna UI di chiamata: il telefono e' un gateway headless.

## Requisiti

- Telefono Android **stock, non rooted** (target: Samsung S25 Ultra)
- Android 10+ (`minSdk = 29`)
- Per compilare: **Android Studio** (consigliato) oppure Gradle + Android SDK

## Build

### Con Android Studio
1. `File > Open` → seleziona la cartella `android/dialer/`
2. Android Studio genera il Gradle wrapper e scarica le dipendenze
3. `Run`, oppure `Build > Build APK(s)`

### Da riga di comando
```
cd android/dialer
gradle wrapper            # richiede Gradle installato (una tantum): il wrapper non e' nel repository
./gradlew assembleDebug   # -> app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Uso

1. Apri l'app sul telefono
2. **Concedi permessi** (telefono, stato chiamata, notifiche)
3. **Imposta come telefono predefinito** → conferma la richiesta di sistema
4. **Token di accoppiamento**: scrivi lo stesso token del servizio vocale e premi *Salva token*. Senza token il servizio non parte
5. **Avvia servizio** → parte il Foreground Service col server HTTP
6. La schermata mostra l'indirizzo `http://<ip-telefono>:8080` e se l'app
   e' il dialer predefinito

## REST API

Auth: header `Authorization: Bearer <token>`, sempre richiesta. Il token lo scrive la persona sul telefono
(`Pairing.kt`, preferenze dell'app); non e' nel codice.

| Metodo | Endpoint  | Body                      | Descrizione                      |
|--------|-----------|---------------------------|----------------------------------|
| GET    | `/health` | —                         | `{"status":"ok"}`                |
| GET    | `/identify` | —                       | carta d'identita': modello, nome Bluetooth, id |
| GET    | `/status` | —                         | stato chiamata corrente          |
| POST   | `/call`   | `{"number":"+39..."}`     | avvia una chiamata cellulare     |
| POST   | `/answer` | —                         | risponde alla chiamata in arrivo |
| POST   | `/hangup` | —                         | termina la chiamata corrente     |

`/status` ritorna:
```json
{
  "state": "IDLE|RINGING|DIALING|OFFHOOK",
  "number": "+39333...",        // numero dell'interlocutore
  "direction": "INCOMING|OUTGOING|",
  "since": 1700000000000
}
```

## Struttura

| File | Ruolo |
|------|-------|
| `CallControllerInCallService.kt` | `InCallService`: riceve le chiamate dal sistema |
| `CallRegistry.kt` | stato condiviso della chiamata + answer/hangup |
| `CallController.kt` | avvio chiamate in uscita (`TelecomManager.placeCall`) |
| `HttpServer.kt` | REST API (NanoHTTPD) |
| `ControlService.kt` | Foreground Service: server HTTP + annuncio mDNS |
| `Pairing.kt` | il token di accoppiamento, scritto dalla persona |
| `DeviceIdentity.kt` | carta d'identita' del telefono (modello, nome Bluetooth) |
| `DiscoveryResponder.kt` | risponde ai probe UDP del call-bridge |
| `MainActivity.kt` | UI minima: permessi, ruolo dialer, avvio servizio |

## Note tecniche

- Chiamata in uscita: `TelecomManager.placeCall`; una volta avviata, anche
  questa arriva all'InCallService.
- Risposta/riaggancio: `Call.answer()` / `Call.disconnect()` via `CallRegistry`.
- **Discovery**: l'IP del telefono cambia a ogni riconnessione al Wi-Fi, quindi
  l'app si fa trovare da sola su due canali indipendenti — annuncio mDNS
  (`_callapicall._tcp`) e risposta ai probe UDP sulla 8079 — perche' nessuno
  dei due funziona su tutte le reti. Lato PC la cascata e' in
  `voice/callbridge/phone_discovery.py`.
  Il servizio acquisisce un `MulticastLock`: senza, il Wi-Fi scarta i pacchetti
  multicast destinati ad altri e le query mDNS non arrivano mai all'annuncio.
  **Limite osservato**: su reti il cui access point filtra il multicast, mDNS
  non passa comunque (verificato: sulla rete `192.168.123.x` di casa nessun
  annuncio raggiunge il PC, mentre sulla `192.168.1.x` si'). In quel caso
  rispondono le altre vie — il probe UDP trova il telefono in ~0,5 s.
- **Piu' telefoni**: se in casa ce n'e' piu' d'uno con l'app installata, si
  rischia di comandare quello sbagliato (sintomo tipico: `/call` risponde
  `ok:true` ma lo stato resta `IDLE`, perche' li' l'app non e' il dialer
  predefinito). Si fissa quale usare con `phone_id` nel config del bridge,
  valorizzato con l'`id` restituito da `/identify`.
- **Nome Bluetooth**: `/identify` lo espone perche' su Windows gli endpoint
  audio del vivavoce si chiamano `<nome BT> Hands-Free HF Audio`: e' cosi' che
  il bridge lega il telefono al suo endpoint HFP senza modelli cablati. Sta
  dietro l'endpoint autenticato, non negli annunci in chiaro, perche' di solito
  contiene il nome del proprietario.
- Per essere eleggibile come dialer: `MainActivity` ha gli intent-filter
  `DIAL` e l'`InCallService` ha `IN_CALL_SERVICE_UI=true` + permesso
  `BIND_INCALL_SERVICE`. Il ruolo si richiede a runtime con `RoleManager`.

## Limiti noti / TODO

- HTTP in chiaro sulla LAN → ok in rete fidata; valutare TLS
- Nessun avvio automatico al boot → valutare un `BOOT_COMPLETED` receiver
