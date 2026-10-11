<#
.SYNOPSIS
    Avvia Basalt (server web + motore) con il modello di basalt_config.json e apre l'interfaccia nel browser.

.DESCRIPTION
    Prima di avviare controlla che la RAM e la memoria impegnabile (commit) bastino senza far crescere il file di
    paging, e avvisa se la VRAM libera è poca. Poi avvia "bin\basalt.exe serve --config basalt_config.json" a
    priorità normale, apre http://127.0.0.1:7232 appena il server risponde (il modello si carica dietro, la pagina
    Monitor mostra "Loading") e mostra il log dal vivo.

    Normalmente il server è legato a questa finestra: Ctrl+C o la X della finestra fermano server e motore e
    liberano la RAM (un Job Object di Windows chiude tutto anche se PowerShell viene terminato di colpo).
    Con -Sfondo il server parte staccato, senza finestra: si ferma con Ferma-Basalt.ps1.

.PARAMETER Prova
    Fa tutti i controlli e dice cosa farebbe, senza avviare nulla.

.PARAMETER Sfondo
    Avvia il server staccato dalla finestra (PID in basalt.pid); per fermarlo: Ferma-Basalt.ps1.

.PARAMETER Config
    Il file di configurazione (predefinito: basalt_config.json accanto a questo script).

.EXAMPLE
    .\Avvia-Basalt.ps1 -Prova
.EXAMPLE
    .\Avvia-Basalt.ps1
.EXAMPLE
    .\Avvia-Basalt.ps1 -Sfondo
#>
[CmdletBinding()]
param(
    [switch]$Prova,
    [switch]$Sfondo,
    [string]$Config = 'basalt_config.json'
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$Radice = $PSScriptRoot
$GiB = [double](1GB)
$BufferGiB = 6.0          # buffer del motore e del server oltre all'arena degli esperti
$MargineRam = 0.15        # quota della RAM fisica che resta libera (la stessa che Basalt lascia su Windows)
$VramMinGiB = 6.0
$AttesaMaxS = 600         # 10 minuti per la prima risposta HTTP

function Scrivi([string]$Testo, [string]$Colore = 'Gray') { Write-Host $Testo -ForegroundColor $Colore }
function GiB([double]$Byte) { '{0:N1} GiB' -f ($Byte / $GiB) }
function GiBSu([double]$Byte) { '{0:N1} GiB' -f ([math]::Ceiling($Byte / $GiB * 10) / 10) }   # quanto manca, per eccesso

function Test-DaExplorer {
    # "Esegui con PowerShell" dal menu del tasto destro: il genitore è explorer.exe e la finestra si chiude a fine script
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
        $padre = Get-CimInstance Win32_Process -Filter "ProcessId=$($me.ParentProcessId)"
        return ($padre -and $padre.Name -eq 'explorer.exe')
    } catch { return $false }
}

function Esci([int]$Codice) {
    if (Test-DaExplorer) { [void](Read-Host 'Premi Invio per chiudere la finestra') }
    exit $Codice
}

function Read-BasaltHeader([string]$Percorso) {
    # l'intestazione del file .basalt: 64 KiB, magic BASALTMF, poi la tabella delle sezioni (128 byte ciascuna)
    $buf = New-Object byte[] 65536
    $fs = [System.IO.File]::Open($Percorso, 'Open', 'Read', 'ReadWrite')
    try { [void]$fs.Read($buf, 0, $buf.Length) } finally { $fs.Dispose() }
    if ([System.Text.Encoding]::ASCII.GetString($buf, 0, 8) -ne 'BASALTMF') { throw "$Percorso non è un file .basalt" }
    $n = [BitConverter]::ToUInt32($buf, 12)
    $sezioni = @{}
    for ($i = 0; $i -lt $n; $i++) {
        $e = 64 + 128 * $i
        $nome = [System.Text.Encoding]::UTF8.GetString($buf, $e, 64).TrimEnd([char]0)
        $digest = -join ($buf[($e + 80)..($e + 111)] | ForEach-Object { $_.ToString('x2') })
        $sezioni[$nome] = @{ Size = [BitConverter]::ToUInt64($buf, $e + 72); Digest = $digest }
    }
    return $sezioni
}

function Get-Memoria {
    # classe WMI non localizzata (i nomi dei contatori di prestazione cambiano con la lingua di Windows)
    $m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $os = Get-CimInstance Win32_OperatingSystem
    [pscustomobject]@{
        Totale      = [double]$os.TotalVisibleMemorySize * 1KB
        Disponibile = [double]$m.AvailableBytes
        Impegnata   = [double]$m.CommittedBytes
        Limite      = [double]$m.CommitLimit
    }
}

function Test-Http([string]$Url) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Proxy = $null
        $req.Timeout = 2000
        $res = $req.GetResponse()
        $res.Close()
        return $true
    } catch [System.Net.WebException] {
        return ($null -ne $_.Exception.Response)      # risponde, anche se con un errore HTTP
    } catch { return $false }
}

function Get-Ascoltatore([int]$Porta) {
    $c = Get-NetTCPConnection -LocalPort $Porta -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
}

# ------------------------------------------------------------------ configurazione
$cfgPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Radice $Config }
if (-not (Test-Path $cfgPath)) { Scrivi "Manca il file di configurazione $cfgPath" Red; Esci 1 }
$cfg = Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json
$srv = $cfg.server
$porta = 7232
if ($srv.PSObject.Properties['port']) { $porta = [int]$srv.port }
$hostIp = '127.0.0.1'
if ($srv.PSObject.Properties['host'] -and $srv.host -notin @('', '0.0.0.0', '::')) { $hostIp = $srv.host }
$url = "http://${hostIp}:$porta/"
$modello = [string]$srv.model
if (-not [System.IO.Path]::IsPathRooted($modello)) { $modello = Join-Path (Split-Path $cfgPath) $modello }
$exe = Join-Path $Radice 'bin\basalt.exe'
$venv = Join-Path $Radice '.venv'
$logDir = Join-Path $Radice 'logs'
$log = Join-Path $logDir 'basalt-serve.log'
$pidFile = Join-Path $Radice 'basalt.pid'

Scrivi "Basalt - avvio $(if ($Prova) { '(PROVA: non parte nulla)' })" Cyan
Scrivi "  configurazione: $cfgPath"
Scrivi "  modello:        $modello"
Scrivi "  indirizzo:      $url"

# ------------------------------------------------------------------ già in esecuzione?
$asc = Get-Ascoltatore $porta
if ($asc) {
    $nostro = ($asc.CommandLine -match 'serve\.server') -or ($asc.Name -like 'basalt*')
    if ($nostro) {
        Scrivi "Basalt è già in esecuzione sulla porta $porta (PID $($asc.ProcessId)): apro solo il browser." Yellow
        if (-not $Prova) { Start-Process $url }
        Esci 0
    }
    Scrivi "La porta $porta è occupata da $($asc.Name) (PID $($asc.ProcessId)): non avvio Basalt." Red
    Esci 1
}
if (Test-Path $pidFile) {
    if (-not $Prova) { Remove-Item $pidFile -ErrorAction SilentlyContinue }   # di un avvio finito male
    Scrivi '  (basalt.pid di un avvio precedente: rimosso)' DarkGray
}

# ------------------------------------------------------------------ file necessari
$mancano = @()
foreach ($f in @($exe, $modello, (Join-Path $venv 'Scripts\python.exe'), (Join-Path $Radice 'tools\windows\BasaltLauncher.cs'))) {
    if (-not (Test-Path $f)) { $mancano += $f }
}
if ($mancano) { Scrivi 'Mancano questi file:' Red; $mancano | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }

# ------------------------------------------------------------------ VRAM (prima: Windows la conta nel commit)
$gpu = 0
if ($srv.PSObject.Properties['gpu']) { $gpu = [int](([string]$srv.gpu).Split(',')[0]) }
$riservaMiB = 700                                       # il predefinito del motore per --vram-reserve-mib
if ($cfg.PSObject.Properties['engine'] -and $cfg.engine.PSObject.Properties['vram-reserve-mib']) {
    $riservaMiB = [int]$cfg.engine.'vram-reserve-mib'
}
$vramBasalt = $null
try {
    $riga = & nvidia-smi --id=$gpu --query-gpu=name,memory.free,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    $parti = $riga.Split(',') | ForEach-Object { $_.Trim() }
    $vramLibera = [double]$parti[1] * 1MB
    # --expert-cache auto riempie la VRAM libera meno la riserva: è quanto Basalt allocherà sulla GPU
    $vramBasalt = [math]::Max([double]0, $vramLibera - $riservaMiB * 1MB)
    Scrivi ''
    Scrivi "GPU $gpu ($($parti[0])): $(GiB $vramLibera) liberi di $(GiB ([double]$parti[2] * 1MB)); Basalt ne prenderà circa $(GiB $vramBasalt) (riserva $riservaMiB MiB)" Cyan
    if ($vramLibera -lt $VramMinGiB * $GiB) {
        Scrivi "  Attenzione: meno di $VramMinGiB GiB di VRAM liberi; un altro programma usa la GPU e il modello andrà più lento." Yellow
    } else { Scrivi '  OK' Green }
} catch {
    Scrivi "Attenzione: nvidia-smi non risponde, VRAM non controllata ($($_.Exception.Message))" Yellow
}
if ($null -eq $vramBasalt) { $vramBasalt = 32 * $GiB }  # sconosciuta: si conta il caso peggiore (una RTX 5090 piena)

# ------------------------------------------------------------------ memoria
$sez = Read-BasaltHeader $modello
if (-not $sez.ContainsKey('experts')) { Scrivi "$modello non ha la sezione experts" Red; Esci 1 }
$arena = [double]$sez['experts'].Size
$mem = Get-Memoria
$servonoRam = $arena + $BufferGiB * $GiB + $MargineRam * $mem.Totale
$servonoCommit = $arena + $BufferGiB * $GiB + $vramBasalt
$commitLibero = $mem.Limite - $mem.Impegnata
Scrivi ''
Scrivi 'Memoria:' Cyan
Scrivi "  RAM fisica totale:            $(GiB $mem.Totale)"
Scrivi "  RAM disponibile ora:          $(GiB $mem.Disponibile)"
Scrivi "  servono:                      $(GiB $servonoRam)  (esperti $(GiB $arena) + buffer $(GiB ($BufferGiB * $GiB)) + margine 15% $(GiB ($MargineRam * $mem.Totale)))"
Scrivi "  commit libero (senza paging): $(GiB $commitLibero)  (impegnata $(GiB $mem.Impegnata) di $(GiB $mem.Limite), file di paging alla misura attuale)"
Scrivi "  servono:                      $(GiB $servonoCommit)  (esperti + buffer + VRAM di Basalt $(GiB $vramBasalt), che Windows conta nel commit)"
$memOk = $true
if ($mem.Disponibile -lt $servonoRam) {
    Scrivi "  Manca RAM: $(GiBSu ($servonoRam - $mem.Disponibile)) in più da liberare (chiudi altri programmi pesanti)." Red
    $memOk = $false
}
if ($commitLibero -lt $servonoCommit) {
    Scrivi "  Manca memoria impegnabile: $(GiBSu ($servonoCommit - $commitLibero)) in più (il file di paging non deve crescere: chiudi altri programmi)." Red
    $memOk = $false
}
if ($memOk) { Scrivi '  OK' Green }

if (-not $memOk -and -not $Prova) {
    Scrivi ''
    Scrivi 'Basalt NON viene avviato: la memoria non basta senza usare il file di paging.' Red
    Esci 1
}

# ------------------------------------------------------------------ ambiente e comando
$cmdLine = '"' + $exe + '" serve --config "' + $cfgPath + '"'
$cache = if ($env:BASALT_CACHE) { $env:BASALT_CACHE } else { Join-Path $HOME '.cache\basalt' }
$engineLog = Join-Path (Join-Path $cache $sez['meta'].Digest.Substring(0, 16)) 'engine.log'
Scrivi ''
Scrivi 'Avvio:' Cyan
Scrivi "  comando:      $cmdLine"
Scrivi "  cartella:     $Radice"
Scrivi "  modo:         $(if ($Sfondo) { 'in sfondo (fermare con Ferma-Basalt.ps1)' } else { 'legato a questa finestra (Ctrl+C o chiudere la finestra lo ferma)' })"
Scrivi "  log server:   $log"
Scrivi "  log motore:   $engineLog"

Add-Type -Path (Join-Path $Radice 'tools\windows\BasaltLauncher.cs')    # verifica anche in prova che compili

if ($Prova) {
    Scrivi ''
    if ($memOk) {
        Scrivi 'PROVA completata: tutti i controlli sono passati, nessun processo avviato.' Green
        Esci 0
    }
    Scrivi 'PROVA completata, nessun processo avviato: in questo momento un avvio vero sarebbe RIFIUTATO (memoria).' Yellow
    Esci 1
}

foreach ($v in 'CL', '_CL_') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }   # opzioni nascoste del compilatore
$env:VIRTUAL_ENV = $venv                                                              # il venv, senza Activate.ps1
$env:PATH = (Join-Path $venv 'Scripts') + ';' + $env:PATH
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force $logDir | Out-Null
if (Test-Path $log) { Move-Item -Force $log "$log.1" }                                # il log dell'avvio precedente
$legato = -not $Sfondo
$idServer = [BasaltLauncher]::Start($exe, $cmdLine, $Radice, $log, $legato)
$proc = Get-Process -Id $idServer
@{ pid = $idServer; start = $proc.StartTime.ToString('o'); mode = $(if ($legato) { 'finestra' } else { 'sfondo' });
   port = $porta; log = $log } | ConvertTo-Json | Set-Content -Encoding UTF8 $pidFile
Scrivi "  avviato: PID $idServer" Green
Scrivi ''

# ------------------------------------------------------------------ log dal vivo, attesa, browser
$fs = [System.IO.File]::Open($log, 'OpenOrCreate', 'Read', 'ReadWrite, Delete')
$lettore = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
$t0 = Get-Date
$aperto = $false
$fine = $false
function Mostra-Log {
    while ($null -ne ($riga = $lettore.ReadLine())) {
        $c = 'Gray'
        if ($riga -match 'error|errore|Traceback') { $c = 'Red' } elseif ($riga -match 'warning') { $c = 'Yellow' }
        elseif ($riga -match '^\[server\] (ready|listening|web UI)') { $c = 'Green' }
        Write-Host $riga -ForegroundColor $c
        if ($riga -match '^\[server\] ready:') {
            Scrivi "Modello pronto dopo $([int]((Get-Date) - $t0).TotalSeconds) s." Green
        }
    }
}

try {
    while ($true) {
        Mostra-Log
        $codice = [BasaltLauncher]::ExitCode()
        if ($codice -ne -1) {
            Mostra-Log
            Scrivi "Il server si è fermato (codice $codice)." $(if ($codice -eq 0) { 'Yellow' } else { 'Red' })
            if ($codice -ne 0 -and (Test-Path $engineLog)) {
                Scrivi "Ultime righe del log del motore ($engineLog):" Yellow
                Get-Content $engineLog -Tail 15 | ForEach-Object { Scrivi "  $_" DarkYellow }
            }
            $fine = $true
            break
        }
        if (-not $aperto) {
            if (Test-Http ($url + 'health')) {
                $aperto = $true
                Scrivi "Interfaccia web pronta: apro $url (il modello si carica: vedi Monitor)" Green
                Start-Process $url
                if ($Sfondo) {
                    Scrivi "Basalt resta in esecuzione in sfondo (PID $idServer). Per fermarlo: Ferma-Basalt.ps1" Cyan
                    break
                }
            } elseif (((Get-Date) - $t0).TotalSeconds -gt $AttesaMaxS) {
                Scrivi "Il server non risponde dopo $AttesaMaxS s: lo fermo." Red
                if ($legato) { [BasaltLauncher]::KillAll() } else { & taskkill.exe /PID $idServer /T /F | Out-Null }
                $fine = $true
                break
            }
        }
        Start-Sleep -Milliseconds 300
    }
} finally {
    if ($legato -and [BasaltLauncher]::ExitCode() -eq -1) {
        # Ctrl+C: il server l'ha ricevuto anche lui e si ferma da solo (QUIT al motore); poi il job chiude il resto
        Scrivi 'Arresto di Basalt in corso...' Yellow
        $limite = (Get-Date).AddSeconds(45)
        while ([BasaltLauncher]::ExitCode() -eq -1 -and (Get-Date) -lt $limite) {
            Mostra-Log
            [void][BasaltLauncher]::Wait(300)
        }
        [BasaltLauncher]::KillAll()
        [void][BasaltLauncher]::Wait(10000)
        $fine = $true
    }
    $lettore.Dispose()
    if ($fine) {
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        $dopo = Get-Memoria
        Scrivi "Basalt fermato. RAM disponibile ora: $(GiB $dopo.Disponibile)." Cyan
    }
}
if ($fine -and [BasaltLauncher]::ExitCode() -ne 0) { Esci 1 }
Esci 0
