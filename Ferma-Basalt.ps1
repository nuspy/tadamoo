<#
.SYNOPSIS
    Ferma Basalt Win+GUI (server e motore) e controlla che la RAM sia tornata libera.

.DESCRIPTION
    Funziona con ogni modo di Avvia-Basalt.ps1: il solo server (-server), quello legato a una finestra (-browser, che
    allora si chiude da sola) e quello dell'app (l'app se ne accorge e torna al Loader). Trova il server da
    basalt.pid, o dalla porta di basalt_config.json; prima chiede al server di scaricare il modello (il motore riceve
    QUIT e libera RAM e VRAM in modo pulito), poi termina l'albero dei processi (basalt.exe serve -> Python ->
    motore) e verifica che non resti nulla. Le opzioni si scrivono con uno o due trattini, come in Avvia-Basalt.ps1.

.PARAMETER Port
    Ferma solo il server su questa porta (--port N): gli altri server Basalt Win+GUI restano accesi.

.PARAMETER File
    Il file di configurazione da cui leggere la porta (predefinito: basalt_config.json accanto allo script).

.PARAMETER Lang
    La lingua dei messaggi (--lang it|en); altrimenti BASALT_LANG, quella scelta nell'interfaccia, l'inglese.
    The messages' language (--lang it|en); else BASALT_LANG, the one chosen in the interface, English.

.EXAMPLE
    .\Ferma-Basalt.ps1
.EXAMPLE
    .\Ferma-Basalt.ps1 --port 7240
#>
[CmdletBinding(PositionalBinding = $false)]   # --port / --file go to $Altri, never to a parameter by position
param(
    [int]$Port = 0,
    [Alias('Config')][string]$File = 'basalt_config.json',      # -Config: il nome vecchio di -File
    [string]$Lang = '',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Altri = @()
)
for ($i = 0; $i -lt $Altri.Count; $i++) {          # -port N, --port N, --port=N; lo stesso per --file
    $a = $Altri[$i]
    $parti = $a.TrimStart('-').Split([char[]]'=', 2)
    $nome = if ($a.StartsWith('-')) { $parti[0].ToLowerInvariant() } else { '' }
    $v = if ($parti.Count -gt 1) { $parti[1] } elseif ($i + 1 -lt $Altri.Count) { $Altri[++$i] } else { $null }
    if ($nome -eq 'port' -and $v -match '^\d+$') { $Port = [int]$v }
    elseif ($nome -in 'file', 'config' -and $v) { $File = $v }
    elseif ($nome -eq 'lang' -and $v) { $Lang = $v }
    else { Write-Host "Argomento sconosciuto o senza valore / unknown argument or no value: $a (Ferma-Basalt.ps1 [--port N] [--file <config>] [--lang it|en])" -ForegroundColor Red; exit 1 }
}
$Config = $File
# la lingua: --lang > BASALT_LANG > scelta nell'interfaccia (prefs.json) > inglese
$Lingua = 'en'
$dati = if ($env:BASALT_DATA) { $env:BASALT_DATA } else { Join-Path $env:APPDATA 'BasaltPannello' }
$scelta = $null
try { $scelta = (Get-Content -Raw -Encoding UTF8 -ErrorAction Stop (Join-Path $dati 'prefs.json') | ConvertFrom-Json).lang } catch { }
foreach ($l in @($Lang, $env:BASALT_LANG, $scelta)) {
    if ($l -and ([string]$l).ToLowerInvariant() -in 'it', 'en') { $Lingua = ([string]$l).ToLowerInvariant(); break }
}
function T([string]$It, [string]$En) { if ($Lingua -eq 'it') { $It } else { $En } }
if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) { Write-Host (T "Porta non valida: $Port (1..65535)" "Invalid port: $Port (1..65535)") -ForegroundColor Red; exit 1 }

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$Radice = $PSScriptRoot
$GiB = [double](1GB)
$pidFile = Join-Path $Radice 'basalt.pid'
$binDir = (Join-Path $Radice 'bin').ToLowerInvariant()

function Scrivi([string]$Testo, [string]$Colore = 'Gray') { Write-Host $Testo -ForegroundColor $Colore }
function GiB([double]$Byte) { '{0:N1} GiB' -f ($Byte / $GiB) }
function Disponibile { [double](Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory).AvailableBytes }

function Test-DaExplorer {
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
        $padre = Get-CimInstance Win32_Process -Filter "ProcessId=$($me.ParentProcessId)"
        return ($padre -and $padre.Name -eq 'explorer.exe')
    } catch { return $false }
}
function Esci([int]$Codice) {
    if (Test-DaExplorer) { [void](Read-Host (T 'Premi Invio per chiudere la finestra' 'Press Enter to close the window')) }
    exit $Codice
}

function Get-ProcessiBasalt {
    # i processi di questa installazione: basalt.exe (serve e motore), basalt-vision.exe, il server Python
    Get-CimInstance Win32_Process | Where-Object {
        (($_.Name -in @('basalt.exe', 'basalt-vision.exe')) -and $_.ExecutablePath -and
            $_.ExecutablePath.ToLowerInvariant().StartsWith($binDir)) -or
        ($_.Name -like 'python*.exe' -and $_.CommandLine -and $_.CommandLine -match 'serve\.server' -and
            $_.CommandLine.ToLowerInvariant().Contains($binDir))
    }
}

# ------------------------------------------------------------------ dove gira
$cfgPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $Radice $Config }
$porta = 7232
$hostIp = '127.0.0.1'
if (Test-Path $cfgPath) {
    $srv = (Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json).server
    if ($srv.PSObject.Properties['port']) { $porta = [int]$srv.port }
    if ($srv.PSObject.Properties['host'] -and $srv.host -notin @('', '0.0.0.0', '::')) { $hostIp = $srv.host }
}
if ($Port) { $porta = $Port }
$capo = $null                                   # il processo "basalt.exe serve" avviato dallo script
$modo = $null
if (Test-Path $pidFile) {
    try {
        $info = Get-Content -Raw $pidFile | ConvertFrom-Json
        $p = Get-Process -Id ([int]$info.pid) -ErrorAction Stop
        # stesso PID e stesso istante di avvio: non un altro processo che ha riusato il numero
        $stessaPorta = -not $Port -or -not $info.PSObject.Properties['port'] -or [int]$info.port -eq $Port
        if ($stessaPorta -and [math]::Abs(($p.StartTime - [datetime]$info.start).TotalSeconds) -lt 2) { $capo = $p.Id; $modo = $info.mode }
    } catch { }
}
if (-not $capo) {
    $c = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) {
        $srvProc = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
        if ($srvProc -and $srvProc.CommandLine -match 'serve\.server') {
            $padre = Get-CimInstance Win32_Process -Filter "ProcessId=$($srvProc.ParentProcessId)"
            $capo = if ($padre -and $padre.Name -eq 'basalt.exe') { $padre.ProcessId } else { $srvProc.ProcessId }
        } elseif ($srvProc) {
            Scrivi (T "Sulla porta $porta ascolta $($srvProc.Name) (PID $($srvProc.ProcessId)), che non è Basalt Win+GUI: non lo tocco." "Port $porta is held by $($srvProc.Name) (PID $($srvProc.ProcessId)), which is not Basalt Win+GUI: left alone.") Yellow
        }
    }
}
function Get-Albero([int]$Capo) {
    # il processo e tutti i suoi discendenti
    $elenco = @(Get-CimInstance Win32_Process)
    $ids = @($Capo); $nuovi = @($Capo)
    while ($nuovi.Count) {
        $figli = @($elenco | Where-Object { $nuovi -contains [int]$_.ParentProcessId } | ForEach-Object { [int]$_.ProcessId })
        $nuovi = @($figli | Where-Object { $ids -notcontains $_ }); $ids += $nuovi
    }
    $elenco | Where-Object { $ids -contains [int]$_.ProcessId }
}
if ($Port) {                                    # solo il server di questa porta e i suoi processi
    if (-not $capo) { Scrivi (T "Nessun server Basalt Win+GUI sulla porta $Port." "No Basalt Win+GUI server on port $Port.") Green; Esci 0 }
    $albero = @(Get-Albero $capo | ForEach-Object { [int]$_.ProcessId })
    function Get-ProcessiBasalt { Get-CimInstance Win32_Process | Where-Object { $albero -contains [int]$_.ProcessId } }
}
$tutti = @(Get-ProcessiBasalt)
if (-not $capo -and $tutti.Count -eq 0) {
    Scrivi (T 'Basalt Win+GUI non è in esecuzione.' 'Basalt Win+GUI is not running.') Green
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Esci 0
}

$prima = Disponibile
$chi = if ($capo) { " (PID $capo$(if ($modo) { ", $(T 'avviato' 'started') $modo" }))" } else { '' }
Scrivi (T "Fermo Basalt Win+GUI$chi. RAM disponibile ora: $(GiB $prima)." "Stopping Basalt Win+GUI$chi. RAM available now: $(GiB $prima).") Cyan

# ------------------------------------------------------------------ 1. il motore esce in modo pulito
try {
    # il server scrive il suo segreto di controllo nella cartella dei dati: senza, nessun client può scaricare il modello
    $datiDir = if ($env:BASALT_DATA) { $env:BASALT_DATA } else { Join-Path $env:APPDATA 'BasaltPannello' }
    $segreto = (Get-Content -Raw -ErrorAction Stop (Join-Path $datiDir "control-$porta.token")).Trim()
    $req = [System.Net.HttpWebRequest]::Create("http://${hostIp}:$porta/api/control/unload")
    $req.Proxy = $null
    $req.Method = 'POST'
    $req.ContentType = 'application/json'
    $req.Headers.Add('X-Basalt-Control', $segreto)
    $req.Timeout = 90000                        # QUIT al motore: fino a 20 s, poi terminate
    $corpo = [System.Text.Encoding]::UTF8.GetBytes('{}')
    $req.ContentLength = $corpo.Length
    $s = $req.GetRequestStream(); $s.Write($corpo, 0, $corpo.Length); $s.Close()
    $res = $req.GetResponse()
    $testo = (New-Object System.IO.StreamReader($res.GetResponseStream())).ReadToEnd()
    $res.Close()
    Scrivi "  $(T 'modello scaricato' 'model unloaded'): $testo"
} catch {
    Scrivi "  $(T 'il server non ha scaricato il modello' 'the server did not unload the model') ($($_.Exception.Message)): $(T 'termino i processi.' 'ending the processes.')" DarkYellow
}

# ------------------------------------------------------------------ 2. l'albero dei processi
if ($capo) { & taskkill.exe /PID $capo /T /F 2>&1 | ForEach-Object { Scrivi "  $_" DarkGray } }
$limite = (Get-Date).AddSeconds(30)
do {
    $resto = @(Get-ProcessiBasalt)
    if ($resto.Count -eq 0) { break }
    foreach ($r in $resto) { try { Stop-Process -Id $r.ProcessId -Force -ErrorAction Stop } catch { } }
    Start-Sleep -Milliseconds 500
} while ((Get-Date) -lt $limite)

$resto = @(Get-ProcessiBasalt)
if ($resto.Count -gt 0) {
    Scrivi (T 'Restano processi di Basalt Win+GUI:' 'Basalt Win+GUI processes are left:') Red
    $resto | ForEach-Object { Scrivi "  PID $($_.ProcessId) $($_.Name)" Red }
    Esci 1
}
if (-not $Port -or $modo) { Remove-Item $pidFile -ErrorAction SilentlyContinue }   # basalt.pid era di questo server

# ------------------------------------------------------------------ 3. la RAM torna libera
$dopo = Disponibile
$limite = (Get-Date).AddSeconds(15)
while ($dopo - $prima -lt 1GB -and (Get-Date) -lt $limite) { Start-Sleep -Seconds 1; $dopo = Disponibile }
$liberati = GiB ([math]::Max([double]0, $dopo - $prima))
Scrivi (T "Basalt Win+GUI fermato: nessun processo rimasto. RAM disponibile: $(GiB $dopo) (prima $(GiB $prima), liberati $liberati)." "Basalt Win+GUI stopped: no process left. RAM available: $(GiB $dopo) (before $(GiB $prima), freed $liberati).") Green
Esci 0
