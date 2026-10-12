<#
.SYNOPSIS
    Ferma Basalt (server e motore) e controlla che la RAM sia tornata libera.

.DESCRIPTION
    Funziona sia con il server avviato in sfondo (Avvia-Basalt.ps1 -Sfondo) sia con quello legato a una finestra
    (che allora si chiude da sola). Trova il server da basalt.pid, o dalla porta di basalt_config.json; prima chiede
    al server di scaricare il modello (il motore riceve QUIT e libera RAM e VRAM in modo pulito), poi termina
    l'albero dei processi (basalt.exe serve -> Python -> motore) e verifica che non resti nulla.

.PARAMETER Config
    Il file di configurazione da cui leggere la porta (predefinito: basalt_config.json accanto allo script).

.PARAMETER Port
    Ferma solo il server su questa porta (anche --port N): gli altri server Basalt restano accesi.

.EXAMPLE
    .\Ferma-Basalt.ps1
.EXAMPLE
    .\Ferma-Basalt.ps1 --port 7298
#>
[CmdletBinding(PositionalBinding = $false)]   # --port / --model / --config go to $Altri, never to a parameter by position
param(
    [string]$Config = 'basalt_config.json',
    [int]$Port = 0,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Altri = @()
)
for ($i = 0; $i -lt $Altri.Count; $i++) {          # le forme --port N e --port=N
    $a = $Altri[$i]
    if ($a -eq '--port' -and $i + 1 -lt $Altri.Count) { $Port = [int]$Altri[++$i] }
    elseif ($a -like '--port=*') { $Port = [int]$a.Substring(7) }
    else { Write-Host "Argomento sconosciuto: $a (uso: Ferma-Basalt.ps1 [--port N])" -ForegroundColor Red; exit 1 }
}
if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) { Write-Host "Porta non valida: $Port (1..65535)" -ForegroundColor Red; exit 1 }

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
    if (Test-DaExplorer) { [void](Read-Host 'Premi Invio per chiudere la finestra') }
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
            Scrivi "Sulla porta $porta ascolta $($srvProc.Name) (PID $($srvProc.ProcessId)), che non è Basalt: non lo tocco." Yellow
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
    if (-not $capo) { Scrivi "Nessun server Basalt sulla porta $Port." Green; Esci 0 }
    $albero = @(Get-Albero $capo | ForEach-Object { [int]$_.ProcessId })
    function Get-ProcessiBasalt { Get-CimInstance Win32_Process | Where-Object { $albero -contains [int]$_.ProcessId } }
}
$tutti = @(Get-ProcessiBasalt)
if (-not $capo -and $tutti.Count -eq 0) {
    Scrivi 'Basalt non è in esecuzione.' Green
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Esci 0
}

$prima = Disponibile
Scrivi "Fermo Basalt$(if ($capo) { " (PID $capo$(if ($modo) { ", avviato $modo" }))" }). RAM disponibile ora: $(GiB $prima)." Cyan

# ------------------------------------------------------------------ 1. il motore esce in modo pulito
try {
    $req = [System.Net.HttpWebRequest]::Create("http://${hostIp}:$porta/unload")
    $req.Proxy = $null
    $req.Method = 'POST'
    $req.ContentType = 'application/json'
    $req.Timeout = 90000                        # QUIT al motore: fino a 20 s, poi terminate
    $corpo = [System.Text.Encoding]::UTF8.GetBytes('{}')
    $req.ContentLength = $corpo.Length
    $s = $req.GetRequestStream(); $s.Write($corpo, 0, $corpo.Length); $s.Close()
    $res = $req.GetResponse()
    $testo = (New-Object System.IO.StreamReader($res.GetResponseStream())).ReadToEnd()
    $res.Close()
    Scrivi "  modello scaricato: $testo"
} catch {
    Scrivi "  il server non ha scaricato il modello ($($_.Exception.Message)): termino i processi." DarkYellow
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
    Scrivi 'Restano processi di Basalt:' Red
    $resto | ForEach-Object { Scrivi "  PID $($_.ProcessId) $($_.Name)" Red }
    Esci 1
}
if (-not $Port -or $modo) { Remove-Item $pidFile -ErrorAction SilentlyContinue }   # basalt.pid era di questo server

# ------------------------------------------------------------------ 3. la RAM torna libera
$dopo = Disponibile
$limite = (Get-Date).AddSeconds(15)
while ($dopo - $prima -lt 1GB -and (Get-Date) -lt $limite) { Start-Sleep -Seconds 1; $dopo = Disponibile }
Scrivi "Basalt fermato: nessun processo rimasto. RAM disponibile: $(GiB $dopo) (prima $(GiB $prima), liberati $(GiB ([math]::Max([double]0, $dopo - $prima))))." Green
Esci 0
