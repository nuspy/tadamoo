# Terminale ESP32-P4 — firmware

Terzo terminale di callAPIcall, accanto al PC e all'app Android. Qui gira solo
il **client**: STT, LLM, TTS e la generazione del labiale restano sul PC. Il
device mostra la persona, prende la voce e la rimanda — come fa il telefono.

Il piano completo, i vincoli del protocollo verificati e le misure di capacità
stanno in [`../esp32p4.md`](../esp32p4.md). Questo file copre solo il firmware.

## La board

| | |
|---|---|
| Modello | **Guition JC4880P443C_I_W** (modulo JC-ESP32P4-M3), SKU 10150002 |
| Chip | ESP32-P4 **rev v1.3**, dual core + LP core, 400 MHz |
| Memoria | flash 16 MB, PSRAM 32 MB |
| Display | 4.3" **ST7701S**, **480×800 verticale**, MIPI-DSI 2 lane @500 Mbps, DPI 34 MHz |
| Pin display | reset GPIO5, retroilluminazione GPIO23 (PWM), LDO3 a 2500 mV per il PHY |
| Touch | GT911 su I2C — SDA GPIO7, SCL GPIO8 (bus condiviso col codec audio) |
| Audio | ES8311 |
| WiFi | sul **C6**, raggiunto via ESP-Hosted (RPC su SDIO) |
| Extra utili | **PPA** (scaling/rotazione in hardware), JPEG encode/decode hardware |

> **Non è la ESP32-P4-Function-EV-Board**, anche se il firmware di fabbrica usa
> quel BSP e ne stampa i log (`ESP32_P4_EV`, `Install EK79007...`). Il venditore
> lo ha adattato al proprio pannello lasciandone i nomi: un dettaglio che manda
> fuori strada chi legge solo i log. La differenza conta, perché i pin e i timing
> del display sono altri.

Il PPA e il JPEG hardware non erano nel piano e cambiano in meglio i conti: il
flusso 362×480 dell'avatar si adatta al display senza spendere CPU, e il poster
della persona si decodifica in hardware.

## Da dove viene questo progetto

È il demo ufficiale della dev board,
`esp-dev-kits/examples/esp32-p4-function-ev-board/examples/esp_brookesia_phone`,
che corrisponde **esattamente** al firmware di fabbrica installato sulla board:
`esp_brookesia_demo` v1.0.1 (letto dall'intestazione dell'app nella flash).

Differenze rispetto all'originale:

- `bsp_extra` è stato spostato in `components/` e `EXTRA_COMPONENT_DIRS` è
  sparito dal `CMakeLists.txt`: il progetto è autocontenuto nel repo e non
  dipende dal layout di esp-dev-kits;
- è stata aggiunta l'app `avatar` (sotto).

Nota: il firmware **di fabbrica** su questa board è una variante con le app
`image_display` e `new_app` al posto di `calculator`/`game_2048`/`video_player`.
Le app non cambiano nulla dell'infrastruttura: il sistema è lo stesso.

## Il display: perché serve `components/guition_display`

Il BSP di Espressif conosce solo i pannelli della EV board — 1024×600 (EK79007),
1280×800 (ILI9881C), HDMI. Su questo 480×800 ST7701S le sue `bsp_display_*`
**non funzionano**: il pannello riceve i timing di un altro display e lo schermo
resta **nero**, senza un solo errore nei log. Il pannello e il touch qui si
inizializzano quindi a mano, in `components/guition_display`, nell'ordine:

1. `esp_ldo_acquire_channel()` — LDO3 a 2500 mV per il PHY MIPI, **per primo**
   o il bus DSI non parte;
2. PWM della retroilluminazione (LEDC) su GPIO23, a zero: nessuno vede il
   rumore dei primi frame;
3. `esp_lcd_new_dsi_bus()` — 2 lane @ 500 Mbps;
4. `esp_lcd_new_panel_io_dbi()` — canale comandi;
5. `esp_lcd_new_panel_st7701()` + reset + init — 480×800 a 34 MHz;
6. gli handle vanno a `esp_lv_adapter`, e sopra Brookesia gira senza modifiche;
7. GT911 su I2C, con il bus riusabile dal codec audio.

`lvgl_adapter_init()` chiamava il BSP in due soli punti — pannello e touch — ed è
lì che si innesta questo componente: il resto della catena LVGL/Brookesia è
intatto.

### Le quattro trappole di questo pannello

Trovate una per volta, ognuna con un sintomo che non puntava alla causa:

| Sintomo | Causa |
|---|---|
| schermo nero, nessun errore | timing di un altro pannello (BSP della EV board) |
| `Store access fault` dopo l'init | `num_fbs` del pannello = 1, ma l'adapter chiede `BSP_LCD_DPI_BUFFER_NUMS` = 3: la richiesta falliva e l'adapter proseguiva con un puntatore nullo |
| `ledc_set_duty: LEDC is not initialized`, abort | il PWM della retroilluminazione lo inizializzava l'init display del BSP, scavalcato: ora è nel componente, e `Setting.cpp` usa `guition_backlight_brightness()` |
| touch muto | la macro `ESP_LCD_TOUCH_IO_I2C_GT911_CONFIG()` assegna i campi in ordine sbagliato: vanno scritti a mano |

Il driver ST7701 (Apache-2.0, Espressif) va compilato **anche nella variante
RGB** benché la board sia solo MIPI: su ESP32-P4 `SOC_LCD_RGB_SUPPORTED` è
attivo e il file di dispatch del driver la referenzia comunque.

### Lo stylesheet 480×800 (da fare)

esp-brookesia 0.4.1 ha stylesheet per 320×240, 320×480, 480×480, 800×480,
1024×600, 1280×800, 720×1280, 800×1280 — **non per 480×800**: il firmware di
fabbrica se l'era costruito. Per ora si usa quello **480×480** (stessa
larghezza), che valida pannello e touch ma occupa solo la parte alta dello
schermo. Va generato quello 480×800 partendo da `stylesheets/480_480/dark/`.

### Riferimenti per questa board

Clonati in `E:\esp\guition`: `ageiron/jc4880p443c-template` (driver ST7701 e
`docs/BRINGUP.md`, che elenca gli stessi gotcha incontrati qui),
`ultramcu/guition-jc4880p443c-i-w` (schematico, pinout, aggiornamento del
firmware del C6), `bigbag/JC4880P443C-examples` (esempi LVGL 9).

## Aggiungere un'app (è così che è nata `avatar`)

ESP-Brookesia è multi-app per davvero, ma **a tempo di compilazione**: le app
sono classi C++ compilate nell'unico binario e registrate nel launcher con una
chiamata. Non esiste installazione a runtime come su Android.

Per aggiungerne una servono tre cose, e nient'altro:

1. una cartella `components/apps/<nome>/` con la classe che estende
   `ESP_Brookesia_PhoneApp` (`run()`, `back()`, `close()`) e l'icona in
   `assets/` come `lv_img_dsc_t`;
2. una riga in `components/apps/apps.h`;
3. due righe in `main/main.cpp`:

```cpp
AppAvatar *app_avatar = new AppAvatar();
assert((phone->installApp(app_avatar) >= 0) && "Failed to begin app_avatar");
```

Il `CMakeLists.txt` di `components/apps/` fa **glob ricorsivo** su `*.c`/`*.cpp`,
quindi non va toccato. Le app già presenti non vanno toccate.

## L'app `avatar`

Stato: **non compilata** (nessun ESP-IDF in questo albero), con rete e volto.

- il palco in alto (46% dell'area utile, circa 456×335 px su 480×800): il volto
  del modo `visemes`, altrimenti un testo ("Solo voce");
- sotto: stato (pallino **più** testo, non dipende dal solo colore), l'ultima
  frase e il tasto in basso (120 px). Senza microfono manda un turno scritto;
  mentre il PC parla lo stesso tasto interrompe la risposta;
- `components/gateway_client` parla il protocollo di `docs/voice/gateway.md`:
  l'`auth` porta `render_modes` e `device:"esp32"`, i messaggi lunghi (la
  timeline dei visemi) si ricompongono dai pezzi del WebSocket (tetto 32 KB,
  oltre si scarta il messaggio intero), i tipi sconosciuti si ignorano;
- `components/apps/avatar/AvatarFace`: una faccia disegnata con LVGL la cui
  bocca segue la timeline (apertura e larghezza per visema, con dissolvenza di
  60 ms) sul **clock dell'audio**: la posizione di riproduzione, non l'arrivo
  dei messaggi. Senza altoparlante il clock è virtuale (corre alla frequenza
  dell'audio finché c'è audio ricevuto da "riprodurre"); quando ci sarà un
  codec va dato `AvatarFace::played()`;
- `components/avatar_core`: il C puro (timeline, clock, ricomposizione dei
  frame), compilato anche sul PC da `test_host/`.

Dichiara solo `visemes`, e solo se il controllo del renderer all'avvio è
passato (`vt_selftest`, memoria, oggetti LVGL creati); altrimenti dichiara
niente e il PC manda la sola voce. `wav2lip` e `video` non sono dichiarati.
Non scarica le sprite dell'avatar né la foto: il volto è lo stesso per tutti.
Chiudere l'app chiude la connessione (la dichiarazione vale per il volto che
c'era); riaprirla ricontrolla e dichiara di nuovo.

Prova sul PC delle parti pure (clang o gcc, nessun ESP-IDF):

```
cd test_host
clang -std=c11 -Wall -Wextra -Werror -I../components/avatar_core/include   test_avatar_core.c ../components/avatar_core/viseme_timeline.c   ../components/avatar_core/ws_reasm.c -o test_avatar_core && ./test_avatar_core
```

## Compilare e installare

```powershell
.\build.ps1                    # compila
.\build.ps1 -Flash -SoloApp    # installa la sola app (consigliato)
.\build.ps1 -Flash             # installa TUTTO, spiffs compreso
.\build.ps1 -Clean             # ricompila da zero
```

`build.ps1` esiste perché su questa macchina servono tre aggiustamenti che
`idf.py` non ha di default: ESP-IDF sta su `E:\esp\esp-idf` (su C: resta poco
spazio), il target è `esp32p4`, e **Avast intercetta il TLS**, quindi pip e i
download di ESP-IDF vogliono un bundle di certificati che includa il suo root
(`E:\esp\ca-bundle.pem` = i 137 certificati di sistema + `wscert.pem`). Senza
quel bundle l'installazione di ESP-IDF muore con
`CERTIFICATE_VERIFY_FAILED`, e il wrapper Gradle dell'app Android fallisce per
la stessa ragione.

### Due trappole che costano un'ora ciascuna

**1. Revisione del chip.** La board in nostro possesso ha un ESP32-P4 **rev
v1.3**, mentre il default di ESP-IDF 5.5 e' rev min **v3.01**. Con il default il
flash muore con:

```
bootloader.bin requires chip revision in range [v3.1 - v3.99]
(this chip is revision v1.3)
```

Risolto in `sdkconfig.defaults` con `CONFIG_ESP32P4_REV_MIN_100=y`. Il firmware
di fabbrica infatti annunciava `Min chip rev: v1.0`. Se un giorno si compila su
una board nuova (rev v3.x) questa riga non fa danni: alza solo la compatibilita'.

**2. PowerShell non si ferma su un exe che fallisce.** `$ErrorActionPreference =
"Stop"` vale per i cmdlet, **non** per gli eseguibili esterni: `idf.py flash`
puo' fallire e lo script tira avanti. E' successo davvero — un flash fallito e'
passato inosservato perche' subito dopo si apriva il monitor, e la board
sembrava semplicemente "silenziosa". Per questo `build.ps1` chiama `idf.py`
tramite `Invoke-Idf`, che verifica `$LASTEXITCODE` e solleva.

Il modo affidabile per sapere **quale** firmware sta girando e' il log di boot:

```
I (1757) app_init: Project name:     esp_brookesia_demo
I (1762) app_init: App version:      1.0.1
I (1765) app_init: Compile time:     Jul 11 2026 10:08:47   <- di fabbrica
```

Se `Compile time` non e' la compilazione appena fatta, il flash non e' avvenuto.

### `-SoloApp`, e perché conta

La tabella delle partizioni di questo progetto **non è** quella di fabbrica:

| | progetto | board di fabbrica |
|---|---|---|
| factory (app) | 9 MB | 7 MB @ `0x20000` |
| storage (spiffs) | 4 MB | 6 MB @ `0x720000` |

Il progetto include l'immagine spiffs nel flash (`FLASH_IN_PROJECT`), quindi un
`idf.py flash` riscrive tabella **e** `storage`, perdendo le immagini che sono
sulla board.

**`-SoloApp` non va usato sul firmware di fabbrica.** Le due mappe mettono la
tabella delle partizioni a offset diversi — il progetto a `0x10000`, la board di
fabbrica a `0x8000` — e l'app porta quell'offset compilato dentro: scritta da
sola sopra il firmware di fabbrica cercherebbe la tabella dove non c'e', non
troverebbe `nvs` ne' `spiffs` e partirebbe male. La prima installazione deve
quindi scrivere bootloader, tabella e app insieme (`-Flash` senza `-SoloApp`).

**Dopo** il primo flash completo la board ha la mappa del progetto, e da quel
momento `-SoloApp` (`idf.py app-flash`) e' la via giusta per gli aggiornamenti:
scrive solo la partizione dell'app, e' molto piu' rapido e lascia intatti `nvs`
(le credenziali WiFi salvate) e `storage`.

Backup completo della flash di fabbrica (16 MB) fatto prima di toccare
qualsiasi cosa: ripristinabile con

```
python -m esptool --port COM3 write-flash 0 backup-flash-16MB.bin
```

## Prossimi passi

Nell'ordine, come da piano — i primi due sono le misure che decidono
l'architettura:

1. **ponte P4↔C6**: `iperf3` nei due sensi, atteso ~36 Mbit/s. Sotto i
   20 Mbit/s stabili il video va ripensato prima di tutto il resto.
2. **tailnet**: MicroLink sul P4 e `ping` del PC a `<indirizzo Tailscale del PC>`. Da
   annotare: percorso diretto o via DERP, RTT, e SRAM interna libera col
   tunnel attivo.
3. decoder `esp_h264` software su un file 362×480 **con il display acceso**,
   contando i fps (il DPB sta in PSRAM e il DSI ne tira già ~73 MB/s).
4. microfono e altoparlante (PCM16): oggi l'audio ricevuto si conta soltanto,
   e il volto va sul clock virtuale.
