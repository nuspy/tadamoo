# Handoff — Commit del pagefile per le allocazioni GPU di Basalt-Plus

Data: 2026-10-23. Repo: `E:\Projects\basalt`, branch `tarots/windows`.

## Il problema

Basalt-Plus (motore di inferenza) carica ~32 GB di pesi del modello in VRAM su una RTX 5090.
Sulle build Windows recenti, dxgkrnl (il kernel del display) **committa automaticamente
una riserva equivalente nel pagefile**: quando la GPU alloca memoria, Windows riserva
spazio nel commit di sistema come backup di paginazione. Con il modello intero in VRAM,
il commit totale del processo sale a ~66 GB e il commit di sistema viaggia sui
110-115 GB, vicino al limite di commit (~143 GB). Questo limita la quantità di
RAM/pagina disponibile per il resto del sistema.

Domanda di ricerca: si può evitare (o scaricare a runtime) questo commit "di backup"
senza dover ricostruire il motore ogni volta?

## Ipotesi e strade valutate

1. **Opzione B (scelta)**: usare l'API pubblica `D3DKMTTrimProcessCommitment` per
   "scaricare" il commit quando il motore è in pausa, senza patchare il driver.
2. (Scartata in una prima sessione) far scrivere al chiamante i flag di allocazione
   "non committed": Basalt-Plus alloca via CUDA VMM (`cuMemCreate`, vedi
   `src/core/vmm.cpp`), quindi il dword dei flag lo scrive il driver utente NVIDIA,
   non il nostro codice.

## Ambiente

- Windows 11 build **26100.9168**; dxgkrnl.sys versione **10.0.26100.9168**
  (SHA-256 sul disco: `8f57dd6e...`, file da 5.207.584 byte).
- Registry `...\GraphicsDrivers\DxgKrnlVersion = 69639 (0x11007)` → WDDM ≥ 2.0,
  la gate di versione per il trim è soddisfatta.
- Display driver NVIDIA: serie 32.0.101.70xx. Pagefile su `D:\` (16 GB allocati).
- Basalt-Plus in esecuzione al momento delle misure (PID 24504).

## Cosa è stato fatto

### Esportazioni

Sul build 26100 l'entry point è esposto come **`NtGdiDdDDITrimProcessCommitment`
in `win32u.dll`** (ordinal 378, RVA 0x5F10), e `dxcore.dll` lo inoltra. Nota bene:
il nome con `DdDDT` (con la T, vecchio layout dxgapi) **non esiste** su questo build;
`dxgapi.dll` non c'è proprio. `d3d11.lib` non porta il simbolo.

`D3DKMTQueryProcessOfferInfo` (l'interrogazione "quanto è decomprimibile") è stata
risolta solo nell'ultima versione del probe; nelle versioni precedenti il nome
non corrispondeva e il resolve falliva.

### Struttura

`D3DKMT_TRIMPROCESSCOMMITMENT` = 40 byte: cbSize(4) + padding(4) + hProcess(8) +
Flags(4, `Lazy:1 | OnlyRepused:1 | Reserved:30`) + padding(4) +
DecommitRequested(8) + NumBytesDecommitted(8). Layout confermato da
`d3kkmthk.h` dell'SDK 10.0.26100 (`C:\Program Files (x86)\Windows
Kits\10\Include\10.0.26100.0\shared\d3kmthk.h`).

### Cosa fa la chiamata

- Con handle **limitato** (`PROCESS_QUERY_LIMITED_INFORMATION`): status
  `0xC0000022` (STATUS_ACCESS_DENIED) — serve una maschera più ampia.
- Con handle **ampio** (`MAXIMUM_ALLOWED` o `0x001FFFF`): status
  `0x00000000` (SUCCESS).

### Cosa NON funziona

- `OFFER` → `DecommitUniqueness` e `DecommittableBytes` risultano **0,00 GB**, e il
  commit del processo non scende (66,62 → 66,62 GB tra le rilevazioni; il commit di
  sistema oscilla di ~0,1 GB per rumore). In altre parole: il driver **non dichiara
  nulla di decomprimibile** per un processo che ha le allocazioni resident/pinned.
- Il commit di sistema totale resta ~111 GB con limite 143 GB.

## Artefatti

- `tools\windows\trim_probe.cpp` (+ `.exe`) — il probe funzionante: risolve
  l'export, chiama il trim per tre maschere di accesso, legge l'offerta e stampa
  i delta di commit.
- `tools\windows\offer_probe.cpp`, `trim_test.cpp` — tentativi precedenti,
  superati.
- Ricetta di build (funziona):

  ```powershell
  cmd /c "call `"C:\Program Files\Microsoft Visual Studio\18\Community\VC\
  Auxiliary\Build\vcvars64.bat`" >nul 2>&1 && cd /d E:\Projects\basalt\
  tools\windows && cl /nologo /W3 /EHsc /O2 /Fe:trim_probe.exe trim_probe.cpp"
  ```

  Nota: il percorso corretto è `VC\Auxiliary` (non `VS\`).

## Conclusione del lavoro fin qui

L'API si può chiamare e restituisce successo, ma sul nostro carico di lavoro
(pesi resident fissi) dichiara zero byte decomprimibili: il trim a runtime non
scarica il commit. Le opzioni rimaste per recuperare margine di commit sono:

1. allocare le risorse **senza commit di backup** (lato driver, non in-hand dalla
   nostra UT),
2. gestire il pagefile esplicitamente accettando il floor di ~66 GB,
3. oppure riutilizzare il trim in un punto del ciclo in cui il driver *dichiara*
   le allocazioni offerte (da verificare con un harness in-process).

## Prossimo passo suggerito

Costruire un harness in-process (C++ o dentro il codice di `basalt.exe` stesso)
che: alloca come fa il motore, legge l'offerta dopo ogni allocazione, e chiama il
trim nei punti di inattività del motore. Se anche in-process l'offerta resta 0,
l'opzione "scarica a runtime" è definitivamente da scartare e resta solo la
gestione del pagefile o la modifica del comportamento del driver.

## Avvertenze operative imparate

- PowerShell: `$PID` è una variabile automatica in sola lettura (usare altri nomi).
- La stringa della finestra `Get-Counter` di un'istanza morta fa fallire il parse:
  enumerare prima le istanze.
- Le ricerche di stringhe nel grafico REA sono lentissime (re-importa a ogni
  chiamata) e sul 26100 non trovano nulla (stringhe di debug rimosse): preferire
  parsing diretto del PE on-disk.
