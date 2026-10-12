<#
.SYNOPSIS
    Avvia Basalt (server web + motore) con un profilo di caricamento e apre l'interfaccia nel browser.

.DESCRIPTION
    Se un server Basalt di questa cartella è già acceso (su qualunque porta) non ne avvia un altro: si collega a quello,
    dice indirizzo, impostazioni e modello, ed esce.

    Altrimenti scrive in basalt_config.json i valori del profilo scelto (--config "<nome>", altrimenti il profilo
    predefinito scelto nel Loader; il file precedente resta in basalt_config.json.bak) e controlla con gli stessi
    calcoli del Loader che VRAM, RAM e memoria impegnabile (commit) bastino lasciando liberi il 15% della RAM e il 15%
    del limite di commit, senza far crescere il file di paging. Poi avvia "bin\basalt.exe serve --config
    basalt_config.json", apre http://127.0.0.1:<porta> appena il server risponde (il modello si carica dietro, la
    pagina Monitor mostra "Loading") e mostra il log dal vivo.

    Normalmente il server è legato a questa finestra: Ctrl+C o la X della finestra fermano server e motore e
    liberano la RAM (un Job Object di Windows chiude tutto anche se PowerShell viene terminato di colpo).
    Con -Sfondo il server parte staccato, senza finestra: si ferma con Ferma-Basalt.ps1.
    Con -SoloServer parte staccato e senza aprire il browser: per gli agenti.

.PARAMETER Config
    Il profilo di caricamento (anche --config "<nome>"). Senza: il profilo predefinito.

.PARAMETER Model
    Il file .basalt da servire, con il percorso completo (anche --model "<file>" o -Model). Senza: il modello del
    profilo (--config), altrimenti il modello predefinito per il server scelto nell'app o nel Loader («Usa come
    modello predefinito per il server»), altrimenti quello già nel file di configurazione. Se non ce n'è nessuno lo
    script si ferma e dice come sceglierlo. Un GGUF «Da convertire» si converte con -Converti (dopo una conferma che
    dice quanto scrive e su quale disco); un file «Non compatibile» viene rifiutato con il motivo.

.PARAMETER Converti
    Se il modello è un GGUF da convertire: mostra il piano (dimensione del nuovo file, disco, spazio libero), chiede
    conferma, converte e poi avvia il file .basalt ottenuto (anche --converti).

.PARAMETER RiservaMib
    La VRAM da lasciare libera, in MiB, al posto di quella del profilo (anche --vram-reserve-mib N).

.PARAMETER Port
    La porta (anche --port N, 1..65535); vince su quella del profilo.

.PARAMETER Prova
    Fa tutti i controlli e dice cosa farebbe, senza avviare nulla e senza scrivere basalt_config.json.

.PARAMETER Sfondo
    Avvia il server staccato dalla finestra (PID in basalt.pid); per fermarlo: Ferma-Basalt.ps1.

.PARAMETER SoloServer
    Come -Sfondo, senza aprire il browser (anche --solo-server).

.PARAMETER File
    Il file di configurazione (predefinito: basalt_config.json accanto a questo script).

.EXAMPLE
    .\Avvia-Basalt.ps1 -Prova
.EXAMPLE
    .\Avvia-Basalt.ps1 --config "Valori sicuri" --port 7240
.EXAMPLE
    .\Avvia-Basalt.ps1 -SoloServer
.EXAMPLE
    .\Avvia-Basalt.ps1 --model "D:\Modelli\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --port 7240
#>
[CmdletBinding()]
param(
    [string]$Config = '',
    [string]$Model = '',
    [switch]$Converti,
    [int]$Port = 0,
    [int]$RiservaMib = -1,
    [switch]$Prova,
    [switch]$Sfondo,
    [switch]$SoloServer,
    [string]$File = 'basalt_config.json',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Altri = @()
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Radice = $PSScriptRoot
$GiB = [double](1GB)
$AttesaMaxS = 600         # 10 minuti per la prima risposta HTTP

function Scrivi([string]$Testo, [string]$Colore = 'Gray') { Write-Host $Testo -ForegroundColor $Colore }
function GiB([double]$Byte) { '{0:N1} GiB' -f ($Byte / $GiB) }

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

# ------------------------------------------------------------------ argomenti nella forma --nome valore
for ($i = 0; $i -lt $Altri.Count; $i++) {
    $a = $Altri[$i]
    $val = { param($n) if ($a -like "--$n=*") { $a.Substring($n.Length + 3) } elseif ($i + 1 -lt $Altri.Count) { $script:i++; $Altri[$script:i] } else { $null } }
    if ($a -eq '--config' -or $a -like '--config=*') { $Config = & $val 'config' }
    elseif ($a -eq '--model' -or $a -like '--model=*') { $Model = & $val 'model' }
    elseif ($a -eq '--converti') { $Converti = $true }
    elseif ($a -eq '--vram-reserve-mib' -or $a -like '--vram-reserve-mib=*') {
        $v = & $val 'vram-reserve-mib'
        if (-not ($v -match '^\d+$')) { Scrivi "--vram-reserve-mib vuole un numero di MiB, non '$v'" Red; Esci 1 }
        $RiservaMib = [int]$v
    }
    elseif ($a -eq '--port' -or $a -like '--port=*') {
        $v = & $val 'port'
        if (-not ($v -match '^\d+$')) { Scrivi "--port vuole un numero da 1 a 65535, non '$v'" Red; Esci 1 }
        $Port = [int]$v
    }
    elseif ($a -eq '--solo-server') { $SoloServer = $true }
    elseif ($a -eq '--prova') { $Prova = $true }
    elseif ($a -eq '--sfondo') { $Sfondo = $true }
    else { Scrivi "Argomento sconosciuto: $a (uso: Avvia-Basalt.ps1 [--config ""<profilo>""] [--model ""<file .basalt>""] [--port N] [-Converti] [-Sfondo|-SoloServer] [-Prova])" Red; Esci 1 }
}
if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) { Scrivi "Porta non valida: $Port (1..65535)" Red; Esci 1 }
if ($SoloServer) { $Sfondo = $true }

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

function Get-Json([string]$Url) {
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Proxy = $null
        $req.Timeout = 3000
        $res = $req.GetResponse()
        try { $testo = (New-Object System.IO.StreamReader($res.GetResponseStream(), [System.Text.Encoding]::UTF8)).ReadToEnd() }
        finally { $res.Close() }
        return $testo | ConvertFrom-Json
    } catch { return $null }
}

function Get-Ascoltatore([int]$Porta) {
    $c = Get-NetTCPConnection -LocalPort $Porta -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
}

function Get-ServerAccesi {
    # le porte su cui ascolta un server Basalt di questa cartella: il server Python (serve.server) avviato da bin\basalt.exe
    # o con il venv di qui, e la porta di basalt.pid se risponde come Basalt
    $bin = (Join-Path $Radice 'bin').ToLowerInvariant()
    $qui = $Radice.ToLowerInvariant()
    $porte = @()
    foreach ($c in @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue)) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        if (-not ($p -and $p.CommandLine -and $p.CommandLine -match 'serve\.server')) { continue }
        # the venv's python.exe starts the base interpreter as its child: look at the parent too
        $padre = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
        $tracce = @($p.CommandLine, $p.ExecutablePath)
        if ($padre) { $tracce += @($padre.CommandLine, $padre.ExecutablePath) }
        if ($tracce | Where-Object { $_ -and ($_.ToLowerInvariant().Contains($bin) -or $_.ToLowerInvariant().Contains($qui)) }) {
            $porte += [int]$c.LocalPort
        }
    }
    $pidFile = Join-Path $Radice 'basalt.pid'
    if (Test-Path $pidFile) {
        try { $info = Get-Content -Raw $pidFile | ConvertFrom-Json; if ($info.PSObject.Properties['port']) { $porte += [int]$info.port } } catch { }
    }
    $accesi = @()
    foreach ($porta in ($porte | Sort-Object -Unique)) {
        $h = Get-Json "http://127.0.0.1:$porta/health"
        if ($h -and $h.service -eq 'basalt') { $accesi += [pscustomobject]@{ Porta = $porta; Salute = $h } }
    }
    return $accesi
}

$py = Join-Path $Radice '.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
function Invoke-Py([string[]]$Argomenti) {
    # python del venv in questa cartella; l'uscita e il codice
    Push-Location $Radice
    try {
        $out = & $py @Argomenti 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Codice = $LASTEXITCODE; Righe = @($out) }
    } finally { Pop-Location }
}

# ------------------------------------------------------------------ già acceso?
$accesi = @(Get-ServerAccesi)
if ($accesi.Count) {
    $s = $accesi[0]
    $url = "http://127.0.0.1:$($s.Porta)/"
    $imp = Get-Json "${url}api/settings"
    Scrivi "Basalt è già acceso: mi collego a quello, non ne avvio un altro." Yellow
    Scrivi "  indirizzo:  $url"
    Scrivi "  modello:    $($s.Salute.model) (contesto $($s.Salute.max_context) token, $(if ($s.Salute.loaded) { 'caricato' } else { 'non ancora caricato' }))"
    if ($imp -and $imp.running) {
        $r = $imp.running
        Scrivi "  impostazioni in uso: kv $($r.'engine.kv'), spec $($r.'engine.spec'), cache esperti $($r.'engine.expert-cache'), slot $($r.'server.parallel')"
        if ($imp.config) { Scrivi "  configurazione: $($imp.config)" }
    }
    $pidFile0 = Join-Path $Radice 'basalt.pid'
    if (Test-Path $pidFile0) {
        try {
            $info0 = Get-Content -Raw $pidFile0 | ConvertFrom-Json
            if ($info0.PSObject.Properties['profile'] -and $info0.profile -and $info0.PSObject.Properties['port'] -and [int]$info0.port -eq $s.Porta) { Scrivi "  profilo:    $($info0.profile)" }
        } catch { }
    }
    if ($imp -and $imp.PSObject.Properties['model'] -and $imp.model) { Scrivi "  file:       $($imp.model)" }
    if ($accesi.Count -gt 1) { Scrivi "  altri server Basalt accesi sulle porte: $(($accesi | Select-Object -Skip 1 | ForEach-Object { $_.Porta }) -join ', ')" }
    if ($Config) { Scrivi "  (il profilo «$Config» non è stato applicato: per cambiarlo usa il Loader o Ferma-Basalt.ps1 e poi riavvia)" DarkYellow }
    if ($Model) {
        $inUso = if ($imp -and $imp.PSObject.Properties['model']) { [string]$imp.model } else { '' }
        if (-not $inUso -or ([System.IO.Path]::GetFullPath($Model) -ne [System.IO.Path]::GetFullPath($inUso))) {
            Scrivi "  (il modello «$Model» NON è stato caricato: questo server serve un altro modello. Per cambiarlo: Ferma-Basalt.ps1 --port $($s.Porta), poi di nuovo Avvia-Basalt.ps1 --model ...)" DarkYellow
        }
    }
    if (-not $Prova -and -not $SoloServer) { Start-Process $url }
    Esci 0
}

# ------------------------------------------------------------------ profilo e configurazione
$cfgPath = if ([System.IO.Path]::IsPathRooted($File)) { $File } else { Join-Path $Radice $File }
if (-not (Test-Path $cfgPath)) { Scrivi "Manca il file di configurazione $cfgPath" Red; Esci 1 }
if (-not (Test-Path $py)) { Scrivi "Manca il Python del venv: $py" Red; Esci 1 }
if (-not $Config) { $Config = ((Invoke-Py @('-m', 'serve.profiles', 'default')).Righe | Select-Object -Last 1).Trim() }
$nomi = (Invoke-Py @('-m', 'serve.profiles', 'names')).Righe -join '' | ConvertFrom-Json
if ($nomi -notcontains $Config) {
    Scrivi "Profilo sconosciuto: «$Config». Profili disponibili:" Red
    $nomi | ForEach-Object { Scrivi "  $_" }
    Esci 1
}
# in prova si lavora su una copia: basalt_config.json resta com'è
$usato = $cfgPath
if ($Prova) {
    $usato = Join-Path ([System.IO.Path]::GetTempPath()) "basalt_config.prova.$PID.json"
    Copy-Item $cfgPath $usato -Force
}
# ------------------------------------------------------------------ il modello: --model > profilo > predefinito > file
$argR = @('-m', 'serve.models', 'resolve', '--profile', $Config, '--config', $cfgPath)
if ($Model) { $argR += @('--model', $Model) }
$rs = Invoke-Py $argR
try { $scelta = $rs.Righe | Select-Object -Last 1 | ConvertFrom-Json } catch { Scrivi "Non riesco a scegliere il modello:" Red; $rs.Righe | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }
$fonte = if ($scelta.source) { $scelta.source_text.it } else { '' }
if ($rs.Codice -eq 7) { Scrivi $scelta.reason.it Red; Esci 1 }
if ($rs.Codice -eq 6) {
    Scrivi "Il modello ($fonte) non si può usare: $($scelta.model)" Red
    Scrivi "  $($scelta.reason.it)" Red
    Esci 1
}
if ($rs.Codice -eq 5) {
    Scrivi "Il modello ($fonte) è un GGUF da convertire in .basalt: $($scelta.model)" Yellow
    if (-not $Converti) {
        Scrivi "  Per convertirlo: .\Avvia-Basalt.ps1 --model `"$($scelta.model)`" -Converti (chiede conferma e dice quanto scrive)," Yellow
        Scrivi "  oppure dalla pagina Carica modello: Converti." Yellow
        Esci 1
    }
    $pc = Invoke-Py @('-m', 'serve.models', 'convert', '--model', $scelta.model)
    try { $piano = $pc.Righe | Select-Object -Last 1 | ConvertFrom-Json } catch { $pc.Righe | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }
    Scrivi "  $($piano.summary.it)"
    Scrivi "  file nuovo: $($piano.out_path)"
    foreach ($x in @($piano.problems)) { Scrivi "  $($x.text_it)" Red }
    if (-not $piano.ok) { Scrivi 'Conversione non possibile ora.' Red; Esci 1 }
    if ($Prova) { Scrivi '  (PROVA: la conversione non parte)' Yellow; Esci 0 }
    $ok = Read-Host "Converto? Scrive circa $([math]::Round($piano.estimate_bytes / 1GB, 1)) GB sul disco $($piano.disk) [s/N]"
    if ($ok -notmatch '^(s|si|sì|y|yes)$') { Scrivi 'Conversione annullata.' Yellow; Esci 1 }
    Push-Location $Radice
    try { & $py -m serve.models convert --model $scelta.model --out $piano.out_dir --yes --token $piano.token; $codConv = $LASTEXITCODE } finally { Pop-Location }
    if ($codConv -ne 0) { Scrivi 'La conversione non è riuscita (vedi sopra).' Red; Esci 1 }
    $scelta.model = $piano.out_path
    $fonte = 'il file appena convertito'
}
$argA = @('-m', 'serve.profiles', 'apply', $Config, '--config', $usato, '--model', $scelta.model)
if ($RiservaMib -ge 0) { $argA += @('--vram-reserve-mib', "$RiservaMib") }
$ap = Invoke-Py $argA
if ($ap.Codice -ne 0) { Scrivi "Il profilo «$Config» non si può applicare:" Red; $ap.Righe | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }
$valori = ($ap.Righe | Select-Object -Last 1 | ConvertFrom-Json).values
$cfg = Get-Content -Raw -Encoding UTF8 $usato | ConvertFrom-Json
$srv = $cfg.server
$porta = 7232
if ($srv.PSObject.Properties['port']) { $porta = [int]$srv.port }
if ($Port) { $porta = $Port }
$hostIp = '127.0.0.1'
if ($srv.PSObject.Properties['host'] -and $srv.host -notin @('', '0.0.0.0', '::')) { $hostIp = $srv.host }
$url = "http://${hostIp}:$porta/"
$modello = [string]$srv.model
if (-not [System.IO.Path]::IsPathRooted($modello)) { $modello = Join-Path (Split-Path $cfgPath) $modello }
$exe = Join-Path $Radice 'bin\basalt.exe'
$logDir = Join-Path $Radice 'logs'
$log = Join-Path $logDir 'basalt-serve.log'
$pidFile = Join-Path $Radice 'basalt.pid'

Scrivi "Basalt - avvio $(if ($Prova) { '(PROVA: non parte nulla)' })" Cyan
Scrivi "  profilo:        $Config"
Scrivi "  configurazione: $cfgPath$(if ($Prova) { ' (in prova: una copia)' })"
Scrivi "  modello:        $modello ($fonte)"
Scrivi "  indirizzo:      $url"

$asc = Get-Ascoltatore $porta
if ($asc) { Scrivi "La porta $porta è occupata da $($asc.Name) (PID $($asc.ProcessId)): non avvio Basalt (--port N per un'altra)." Red; Esci 1 }
if (Test-Path $pidFile) {
    if (-not $Prova) { Remove-Item $pidFile -ErrorAction SilentlyContinue }   # di un avvio finito male
    Scrivi '  (basalt.pid di un avvio precedente: rimosso)' DarkGray
}

# ------------------------------------------------------------------ file necessari
$mancano = @()
foreach ($f in @($exe, $modello, (Join-Path $Radice 'tools\windows\BasaltLauncher.cs'))) {
    if (-not (Test-Path $f)) { $mancano += $f }
}
if ($mancano) { Scrivi 'Mancano questi file:' Red; $mancano | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }

# ------------------------------------------------------------------ memoria: gli stessi calcoli del Loader
$gpu = 0
if ($srv.PSObject.Properties['gpu']) { $gpu = [int](([string]$srv.gpu).Split(',')[0]) }
Scrivi ''
Scrivi 'Memoria (gli stessi calcoli del Loader):' Cyan
$ck = Invoke-Py @('-m', 'serve.loader', 'check', '--config', $usato, '--gpu', "$gpu")
$ck.Righe | Select-Object -SkipLast 1 | ForEach-Object { Scrivi $_ $(if ($_ -match '^  (Non entra|Manca|La tabella|GPU NON)') { 'Red' } elseif ($_ -match '^  Attenzione') { 'Yellow' } else { 'Gray' }) }
$memOk = $ck.Codice -eq 0
if ($ck.Codice -eq 2) { Scrivi '  configurazione non leggibile' Red; Esci 1 }
if ($ck.Codice -eq 4) { Scrivi 'Basalt NON viene avviato: il motore non gira su questa GPU (servono GPU NVIDIA Blackwell).' Red; Esci 1 }
if ($memOk) { Scrivi '  OK' Green }
if ($Prova) { Remove-Item $usato -ErrorAction SilentlyContinue; Remove-Item "$usato.bak" -ErrorAction SilentlyContinue }

if (-not $memOk -and -not $Prova) {
    Scrivi ''
    Scrivi 'Basalt NON viene avviato: la memoria non basta senza usare il file di paging (scegli un altro profilo nel Loader o chiudi altri programmi).' Red
    Esci 1
}

# ------------------------------------------------------------------ ambiente e comando
$cmdLine = '"' + $exe + '" serve --config "' + $cfgPath + '"' + $(if ($Port) { " --port $Port" } else { '' })
$cache = if ($env:BASALT_CACHE) { $env:BASALT_CACHE } else { Join-Path $HOME '.cache\basalt' }
Scrivi ''
Scrivi 'Avvio:' Cyan
Scrivi "  comando:      $cmdLine"
Scrivi "  cartella:     $Radice"
Scrivi "  modo:         $(if ($SoloServer) { 'solo server, in sfondo e senza browser (fermare con Ferma-Basalt.ps1)' } elseif ($Sfondo) { 'in sfondo (fermare con Ferma-Basalt.ps1)' } else { 'legato a questa finestra (Ctrl+C o chiudere la finestra lo ferma)' })"
Scrivi "  log server:   $log"
Scrivi "  log motore:   $cache\<modello>\engine.log"

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
$venv = Join-Path $Radice '.venv'
$env:VIRTUAL_ENV = $venv                                                              # il venv, senza Activate.ps1
$env:PATH = (Join-Path $venv 'Scripts') + ';' + $env:PATH
New-Item -ItemType Directory -Force $logDir | Out-Null
if (Test-Path $log) { Move-Item -Force $log "$log.1" }                                # il log dell'avvio precedente
$legato = -not $Sfondo
$idServer = [BasaltLauncher]::Start($exe, $cmdLine, $Radice, $log, $legato)
$proc = Get-Process -Id $idServer
@{ pid = $idServer; start = $proc.StartTime.ToString('o'); mode = $(if ($legato) { 'finestra' } elseif ($SoloServer) { 'solo-server' } else { 'sfondo' });
   port = $porta; profile = $Config; log = $log } | ConvertTo-Json | Set-Content -Encoding UTF8 $pidFile
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
            $fine = $true
            break
        }
        if (-not $aperto) {
            if (Test-Http ($url + 'health')) {
                $aperto = $true
                if ($SoloServer) {
                    Scrivi "Server pronto su $url (solo server, PID $idServer; il modello si carica: GET ${url}health). Per fermarlo: Ferma-Basalt.ps1 --port $porta" Green
                    break
                }
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
        $dopo = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
        Scrivi "Basalt fermato. RAM disponibile ora: $(GiB ([double]$dopo.AvailableBytes))." Cyan
    }
}
if ($fine -and [BasaltLauncher]::ExitCode() -ne 0) { Esci 1 }
Esci 0
