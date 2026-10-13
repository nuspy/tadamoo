<#
.SYNOPSIS
    Avvia Basalt Win+GUI: di norma apre l'app (BasaltWinGUI.exe, l'interfaccia principale), oppure il server con il
    browser, oppure il solo server per gli agenti. / Starts Basalt Win+GUI: the app by default (BasaltWinGUI.exe, the
    main interface), or the server with the browser, or the server alone for agents.

.DESCRIPTION
    Tre modi, con gli stessi nomi di Avvia-Strata.ps1 (ogni opzione si scrive con uno o due trattini, maiuscole o
    minuscole come si vuole):

      -gui      (il modo predefinito, senza opzioni) apre l'app BasaltWinGUI.exe passandole --config, --model,
                --port, --vram-libera e --lang. L'app avvia il server senza caricare il modello e mostra il Loader:
                cartelle dei modelli, profili, tutti i valori, la previsione di memoria e velocità, la configurazione
                guidata, Carica. Il browser NON si apre, ma l'indirizzo resta disponibile e viene stampato. Se l'app
                non è ancora stata costruita lo script dice come costruirla e intanto usa -browser.
      -browser  il server legato a questa finestra e l'interfaccia nel browser, senza app: Ctrl+C o la X della
                finestra fermano server e motore e liberano la RAM (un Job Object di Windows chiude tutto anche se
                PowerShell viene terminato di colpo).
      -server   il solo server, staccato dalla finestra, senza app né browser: lo script ritorna quando il modello è
                carico (o se non parte). Serve agli agenti; si ferma con Ferma-Basalt.ps1 --port N.

    Se un server Basalt Win+GUI di questa cartella è già acceso non ne avvia un altro: con -gui apre l'app (che
    chiede se collegarsi o cambiare modello), con -browser apre il browser su quel server. Con -server confronta i
    parametri di caricamento chiesti (risolti come per un avvio nuovo: senza --config il profilo predefinito) con
    quelli del server acceso: se sono gli stessi si collega e ritorna; se cambiano li elenca e chiede, in una
    finestra (o nella console senza desktop), se riusare il server avviato o riavviarlo con i nuovi parametri.
    -riusa e -riavvia rispondono in anticipo (per gli agenti); senza risposta entro 60 secondi, o senza nessuno a
    cui chiedere, il server avviato viene riusato: nessun server viene fermato senza una risposta.

    Con -browser e -server lo script scrive in basalt_config.json i valori del profilo scelto (il file precedente
    resta in basalt_config.json.bak) e controlla con gli stessi calcoli del Loader che VRAM, RAM e memoria
    impegnabile (commit) bastino lasciando libera la parte di RAM e di commit scelta per gli altri programmi (15% se
    non è stata cambiata), senza far crescere il file di paging.

    La lingua dei messaggi: --lang it|en, altrimenti BASALT_LANG, altrimenti quella scelta nell'interfaccia
    (Impostazioni), altrimenti l'inglese. The messages' language: --lang it|en, else BASALT_LANG, else the one chosen
    in the interface (Settings), else English. English help: README.md.

.EXAMPLE
    .\Avvia-Basalt.ps1
.EXAMPLE
    .\Avvia-Basalt.ps1 -prova
.EXAMPLE
    .\Avvia-Basalt.ps1 -browser --config "Valori sicuri" --port 7240
.EXAMPLE
    .\Avvia-Basalt.ps1 -server --model "D:\Modelli\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --vram-libera 2 -riusa
#>
[CmdletBinding(PositionalBinding = $false)]   # --port / --model / --config go to $Altri, never to a parameter by position
param(
    [switch]$Gui,
    [switch]$Browser,
    [switch]$Server,
    [string]$Config = '',
    [string]$Model = '',
    [int]$Port = 0,
    [Alias('vram-libera')][string]$VramLibera = '',
    [Alias('vram-reserve-mib')][int]$RiservaMib = -1,
    [switch]$Converti,
    [switch]$Prova,
    [switch]$Riusa,
    [switch]$Riavvia,
    [string]$Lang = '',
    [string]$File = 'basalt_config.json',
    # interno: il server legato al chiamante, senza interfaccia (per un programma che lo avvia e lo ferma)
    [switch]$PerApp,
    # nomi vecchi, ancora accettati: -Sfondo e -SoloServer ora sono -server
    [switch]$Sfondo,
    [Alias('solo-server')][switch]$SoloServer,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Altri = @()
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Radice = $PSScriptRoot
$GiB = [double](1GB)
$AttesaMaxS = 600         # 10 minuti per la prima risposta HTTP
$AttesaCaricoS = 1800     # -server: 30 minuti perché il modello sia carico
$AttesaRispostaS = 60     # -server con un server già acceso: il tempo per rispondere, poi lo si riusa
if ($env:BASALT_ASK_TIMEOUT_S -match '^\d+$') { $AttesaRispostaS = [int]$env:BASALT_ASK_TIMEOUT_S }
$RiavvioModello = 75      # codice d'uscita del server quando il Loader ha scelto un altro modello: si riavvia

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
    if (Test-DaExplorer) { [void](Read-Host $(T 'Premi Invio per chiudere la finestra' 'Press Enter to close the window')) }
    exit $Codice
}

# ------------------------------------------------------------------ la lingua: --lang > BASALT_LANG > scelta nell'interfaccia > inglese
function Get-Lingua([string]$Data) {
    foreach ($l in @($Data, $env:BASALT_LANG)) { if ($l -and $l.ToLowerInvariant() -in 'it', 'en') { return $l.ToLowerInvariant() } }
    $dati = if ($env:BASALT_DATA) { $env:BASALT_DATA } else { Join-Path $env:APPDATA 'BasaltPannello' }
    try {
        $p = Get-Content -Raw -Encoding UTF8 (Join-Path $dati 'prefs.json') | ConvertFrom-Json
        if ($p.PSObject.Properties['lang'] -and $p.lang -in 'it', 'en') { return [string]$p.lang }
    } catch { }
    return 'en'
}
$langArg = $Lang
for ($i = 0; $i -lt $Altri.Count; $i++) {                  # --lang prima di tutto: i messaggi che seguono la usano
    $x = $Altri[$i].TrimStart('-').ToLowerInvariant()
    if ($x -eq 'lang' -and $i + 1 -lt $Altri.Count) { $langArg = $Altri[$i + 1] }
    elseif ($x -like 'lang=*') { $langArg = $x.Substring(5) }
}
$Lingua = Get-Lingua $langArg
if ($langArg) { $env:BASALT_LANG = $Lingua }               # --lang: i comandi Python e il server la seguono
function T([string]$It, [string]$En) { if ($Lingua -eq 'it') { $It } else { $En } }
$Uso = T 'uso: Avvia-Basalt.ps1 [-gui | -browser | -server] [--config "<profilo>"] [--model "<file .basalt>"] [--port N] [--vram-libera GB] [-riusa | -riavvia] [-converti] [-prova] [--lang it|en] [-file <configurazione>]' `
         'usage: Avvia-Basalt.ps1 [-gui | -browser | -server] [--config "<profile>"] [--model "<.basalt file>"] [--port N] [--vram-libera GB] [-riusa | -riavvia] [-converti] [-prova] [--lang it|en] [-file <config file>]'

# ------------------------------------------------------------------ le opzioni: -nome o --nome, --nome valore o --nome=valore
for ($i = 0; $i -lt $Altri.Count; $i++) {
    $a = $Altri[$i]
    if (-not $a.StartsWith('-')) { Scrivi "$(T 'Argomento inatteso' 'Unexpected argument'): $a ($Uso)" Red; Esci 1 }
    $parti = $a.TrimStart('-').Split([char[]]'=', 2)
    $nome = $parti[0].ToLowerInvariant()
    $inLinea = if ($parti.Count -gt 1) { $parti[1] } else { $null }
    $val = {
        if ($null -ne $inLinea) { return $inLinea }
        if ($script:i + 1 -lt $Altri.Count) { $script:i++; return $Altri[$script:i] }
        Scrivi "--$nome $(T 'vuole un valore' 'needs a value') ($Uso)" Red; Esci 1
    }
    switch -Exact ($nome) {
        'gui' { $Gui = $true }
        'browser' { $Browser = $true }
        'server' { $Server = $true }
        'prova' { $Prova = $true }
        'converti' { $Converti = $true }
        'riusa' { $Riusa = $true }
        'riavvia' { $Riavvia = $true }
        'perapp' { $PerApp = $true }
        'sfondo' { $Sfondo = $true }
        { $_ -in 'solo-server', 'soloserver' } { $SoloServer = $true }
        'lang' { [void](& $val) }
        'config' { $Config = & $val }
        'model' { $Model = & $val }
        'file' { $File = & $val }
        'port' {
            $v = & $val
            if (-not ($v -match '^\d+$')) { Scrivi (T "--port vuole un numero da 1 a 65535, non '$v'" "--port needs a number from 1 to 65535, not '$v'") Red; Esci 1 }
            $Port = [int]$v
        }
        { $_ -in 'vram-libera', 'vramlibera' } { $VramLibera = & $val }
        { $_ -in 'vram-reserve-mib', 'riservamib' } {
            $v = & $val
            if (-not ($v -match '^\d+$')) { Scrivi (T "--vram-reserve-mib vuole un numero di MiB, non '$v'" "--vram-reserve-mib needs a number of MiB, not '$v'") Red; Esci 1 }
            $RiservaMib = [int]$v
        }
        default { if (-not ($nome -like 'lang=*')) { Scrivi "$(T 'Argomento sconosciuto' 'Unknown argument'): $a ($Uso)" Red; Esci 1 } }
    }
}
if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) { Scrivi (T "Porta non valida: $Port (1..65535)" "Invalid port: $Port (1..65535)") Red; Esci 1 }
if ($VramLibera) {
    $gb = 0.0
    if (-not [double]::TryParse($VramLibera.Replace(',', '.'), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$gb) -or $gb -lt 0 -or $gb -gt 1024) {
        Scrivi (T "--vram-libera vuole i GB da lasciare liberi (per esempio 2 o 1,5), non '$VramLibera'" "--vram-libera needs the GB to leave free (for example 2 or 1.5), not '$VramLibera'") Red; Esci 1
    }
    if ($RiservaMib -ge 0) { Scrivi (T '--vram-libera e --vram-reserve-mib dicono la stessa cosa: usane uno solo' '--vram-libera and --vram-reserve-mib say the same thing: use only one') Red; Esci 1 }
    $RiservaMib = [int][math]::Round($gb * 1024)
}
if ($Riusa -and $Riavvia) { Scrivi (T '-riusa e -riavvia si escludono: scegline uno' '-riusa and -riavvia exclude each other: choose one') Red; Esci 1 }
$ApriDopo = $false                         # -Sfondo apriva il browser quando il server rispondeva
if ($Sfondo -or $SoloServer) {
    $vecchio = if ($Sfondo) { '-Sfondo' } else { '-SoloServer' }
    Scrivi (T "Nota: $vecchio è un nome vecchio: ora si scrive -server." "Note: $vecchio is an old name: it is now -server.") DarkYellow
    $Server = $true
    $ApriDopo = [bool]$Sfondo
}
$modi = @(@($Gui, $Browser, $Server, $PerApp) | Where-Object { $_ })
if ($modi.Count -gt 1) { Scrivi (T "Scegli un solo modo tra -gui, -browser e -server ($Uso)" "Choose only one mode among -gui, -browser and -server ($Uso)") Red; Esci 1 }
if ($modi.Count -eq 0) { $Gui = $true }

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
    # BASALT_SCAN_PORTS (per i test: "7280-7289" o "7283,7285"): si guardano solo quelle porte, mai le altre
    if ($env:BASALT_SCAN_PORTS) {
        $ammesse = @()
        foreach ($pezzo in ($env:BASALT_SCAN_PORTS -split ',')) {
            if ($pezzo.Trim() -match '^(\d+)(?:-(\d+))?$') {
                $da = [int]$Matches[1]; $a = if ($Matches[2]) { [int]$Matches[2] } else { $da }
                $ammesse += $da..$a
            }
        }
        $porte = @($porte | Where-Object { $ammesse -contains $_ })
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
function Say($Coppia) { if ($Lingua -eq 'it') { [string]$Coppia.it } else { [string]$Coppia.en } }   # {"it", "en"} dal Python

# ------------------------------------------------------------------ -server con un server già acceso: riusarlo o riavviarlo?
function Test-Desktop {
    # una sessione con il desktop (non la 0 dei servizi) e senza BASALT_NO_DIALOG: si può mostrare una finestra
    if ($env:BASALT_NO_DIALOG -eq '1') { return $false }
    try { return [Environment]::UserInteractive -and (Get-Process -Id $PID).SessionId -ne 0 } catch { return $false }
}

function Chiedi-Riavvio([string]$Elenco) {
    # 'riusa', 'riavvia' o $null (nessuna risposta entro $AttesaRispostaS secondi, o nessuno a cui chiedere)
    $domanda = T "Basalt Win+GUI è già acceso, con parametri di caricamento diversi da quelli chiesti:" `
                 "Basalt Win+GUI is already running, with loading parameters different from the ones asked for:"
    if (Test-Desktop) {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        [System.Windows.Forms.Application]::EnableVisualStyles()
        $f = New-Object System.Windows.Forms.Form
        $f.Text = 'Basalt Win+GUI'; $f.TopMost = $true; $f.StartPosition = 'CenterScreen'; $f.FormBorderStyle = 'FixedDialog'
        $f.MaximizeBox = $false; $f.MinimizeBox = $false; $f.AutoSize = $true; $f.AutoSizeMode = 'GrowAndShrink'
        $f.Font = New-Object System.Drawing.Font('Segoe UI', 11)
        $ico = Join-Path $Radice 'tools\windows\desktop\basalt.ico'
        if (Test-Path $ico) { try { $f.Icon = New-Object System.Drawing.Icon($ico) } catch { } }
        $tab = New-Object System.Windows.Forms.TableLayoutPanel
        $tab.AutoSize = $true; $tab.ColumnCount = 1; $tab.Padding = New-Object System.Windows.Forms.Padding(16)
        $l1 = New-Object System.Windows.Forms.Label; $l1.AutoSize = $true; $l1.MaximumSize = New-Object System.Drawing.Size(640, 0)
        $l1.Text = "$domanda`n`n$Elenco"
        $l2 = New-Object System.Windows.Forms.Label; $l2.AutoSize = $true; $l2.ForeColor = [System.Drawing.Color]::DimGray
        $righe = New-Object System.Windows.Forms.FlowLayoutPanel; $righe.AutoSize = $true; $righe.Margin = New-Object System.Windows.Forms.Padding(0, 12, 0, 0)
        $b1 = New-Object System.Windows.Forms.Button; $b1.Text = T 'Riusa il server avviato' 'Reuse the running server'
        $b2 = New-Object System.Windows.Forms.Button; $b2.Text = T 'Riavvia con i nuovi parametri' 'Restart with the new parameters'
        foreach ($b in $b1, $b2) { $b.AutoSize = $true; $b.MinimumSize = New-Object System.Drawing.Size(160, 44); $b.Margin = New-Object System.Windows.Forms.Padding(0, 0, 8, 0) }
        $b1.DialogResult = [System.Windows.Forms.DialogResult]::Yes
        $b2.DialogResult = [System.Windows.Forms.DialogResult]::No
        $righe.Controls.AddRange(@($b1, $b2))
        $tab.Controls.Add($l1); $tab.Controls.Add($l2); $tab.Controls.Add($righe)
        $f.Controls.Add($tab); $f.AcceptButton = $b1; $f.CancelButton = $b1
        $script:resta = $AttesaRispostaS
        $testoAttesa = { T "Senza risposta tra $($script:resta) s il server avviato viene riusato." "Without an answer in $($script:resta) s the running server is reused." }
        $l2.Text = & $testoAttesa
        $timer = New-Object System.Windows.Forms.Timer; $timer.Interval = 1000
        $timer.Add_Tick({ $script:resta--; $l2.Text = & $testoAttesa; if ($script:resta -le 0) { $timer.Stop(); $f.DialogResult = [System.Windows.Forms.DialogResult]::Cancel; $f.Close() } })
        $timer.Start()
        $r = $f.ShowDialog()
        $timer.Stop(); $f.Dispose()
        if ($r -eq [System.Windows.Forms.DialogResult]::Yes -and $script:resta -gt 0) { return 'riusa' }
        if ($r -eq [System.Windows.Forms.DialogResult]::No) { return 'riavvia' }
        return $null
    }
    if ([Console]::IsInputRedirected) { return $null }       # un agente: nessuno a cui chiedere
    Scrivi $domanda Yellow
    Scrivi $Elenco
    Scrivi (T "[R]iusa il server avviato / ri[A]vvia con i nuovi parametri (senza risposta tra $AttesaRispostaS s: riuso)" "[R]euse the running server / re[S]tart with the new parameters (no answer in $AttesaRispostaS s: reuse)") Yellow
    $fine = (Get-Date).AddSeconds($AttesaRispostaS)
    while ((Get-Date) -lt $fine) {
        if ([Console]::KeyAvailable) {
            $k = [Console]::ReadKey($true).KeyChar.ToString().ToLowerInvariant()
            if ($k -eq 'r') { return 'riusa' }
            if ($k -in 'a', 's') { return 'riavvia' }
        }
        Start-Sleep -Milliseconds 100
    }
    return $null
}

$cfgPath = if ([System.IO.Path]::IsPathRooted($File)) { $File } else { Join-Path $Radice $File }
$accesi = @(Get-ServerAccesi)

# ------------------------------------------------------------------ -gui: l'app
$exeApp = Join-Path $Radice 'tools\windows\desktop\publish\BasaltWinGUI.exe'
$exeVecchio = Join-Path $Radice 'tools\windows\desktop\publish\BasaltPannello.exe'   # il nome di prima
if (-not (Test-Path $exeApp) -and (Test-Path $exeVecchio)) { $exeApp = $exeVecchio }
if ($Gui -and -not (Test-Path $exeApp)) {
    Scrivi (T "L'app Basalt Win+GUI non è ancora stata costruita ($exeApp)." "The Basalt Win+GUI app is not built yet ($exeApp).") Yellow
    Scrivi "  $(T 'Per costruirla (serve il .NET 8 SDK):' 'To build it (the .NET 8 SDK is needed):')  dotnet publish tools\windows\desktop -c Release -o tools\windows\desktop\publish" Yellow
    Scrivi "  $(T 'Intanto apro Basalt Win+GUI nel browser (-browser).' 'Meanwhile Basalt Win+GUI opens in the browser (-browser).')" Yellow
    $Gui = $false
    $Browser = $true
}
if ($Gui) {
    $argApp = @()
    if ($Config) { $argApp += @('--config', $Config) }
    if ($Model) { $argApp += @('--model', $Model) }
    if ($Port) { $argApp += @('--port', "$Port") }
    if ($RiservaMib -ge 0) { $argApp += @('--vram-libera', ([double]$RiservaMib / 1024).ToString([Globalization.CultureInfo]::InvariantCulture)) }
    if ($langArg) { $argApp += @('--lang', $Lingua) }
    if ($File -ne 'basalt_config.json') { $argApp += @('--file', $cfgPath) }
    if ($Converti) { Scrivi "  $(T '(-converti vale con -browser e -server: nell''app la conversione è nel Loader, «Converti…»)' '(-converti applies to -browser and -server: in the app the conversion is in the Loader, “Converti…”)')" DarkYellow }
    $porta = 7232
    if ($accesi.Count) { $porta = $accesi[0].Porta }
    elseif ($Port) { $porta = $Port }
    elseif (Test-Path $cfgPath) {
        try { $srv0 = (Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json).server; if ($srv0 -and $srv0.PSObject.Properties['port']) { $porta = [int]$srv0.port } } catch { }
    }
    $mostra = ($argApp | ForEach-Object { if ($_ -match '\s') { """$_""" } else { $_ } }) -join ' '
    Scrivi "Basalt Win+GUI - app $(if ($Prova) { T '(PROVA: non parte nulla)' '(DRY RUN: nothing starts)' })" Cyan
    Scrivi "  app:        $exeApp $mostra"
    if ($accesi.Count) { Scrivi "  $(T "Basalt Win+GUI è già acceso sulla porta ${porta}: l'app chiede se collegarsi o cambiare modello." "Basalt Win+GUI is already running on port ${porta}: the app asks whether to connect or change the model.")" Yellow }
    Scrivi "  $(T 'indirizzo: ' 'address:   ') http://127.0.0.1:$porta/  $(T '(il browser non si apre: l''indirizzo resta disponibile)' '(the browser does not open: the address stays available)')"
    Scrivi "  $(T 'L''app avvia il server senza caricare il modello e mostra il Loader; si chiude dall''icona vicino all''orologio (Spegni).' 'The app starts the server without loading the model and shows the Loader; it closes from the icon near the clock (Spegni).')"
    if ($Prova) { Scrivi (T 'PROVA completata: nessun processo avviato.' 'DRY RUN done: no process started.') Green; Esci 0 }
    if ($argApp.Count) { Start-Process -FilePath $exeApp -ArgumentList $mostra -WorkingDirectory $Radice }
    else { Start-Process -FilePath $exeApp -WorkingDirectory $Radice }
    Esci 0
}

# ------------------------------------------------------------------ già acceso?
if ($accesi.Count) {
    $s = $accesi[0]
    $url = "http://127.0.0.1:$($s.Porta)/"
    $imp = Get-Json "${url}api/settings"
    $decisione = 'riusa'
    if ($Server) {
        # i parametri chiesti, risolti come per un avvio nuovo, contro quelli del server acceso
        $argC = @('-m', 'serve.profiles', 'compare', '--url', $url, '--config', $cfgPath)
        if ($Config) { $argC += @('--profile', $Config) }
        if ($Model) { $argC += @('--model', $Model) }
        if ($RiservaMib -ge 0) { $argC += @('--vram-reserve-mib', "$RiservaMib") }
        if ($Port) { $argC += @('--port', "$Port") }
        $cmp = Invoke-Py $argC
        $confronto = $null
        try { $confronto = $cmp.Righe | Select-Object -Last 1 | ConvertFrom-Json } catch { }
        if ($cmp.Codice -eq 3 -and $confronto) {
            $elenco = ($confronto.differences | ForEach-Object { "  $($_.field):  $($_.running)  ->  $($_.requested)" }) -join "`n"
            $elenco = "$(T 'profilo chiesto' 'profile asked for'): $($confronto.profile)`n$(T 'in uso -> chiesto' 'running -> asked for'):`n$elenco"
            if ($Riusa) { $decisione = 'riusa'; $perche = '-riusa' }
            elseif ($Riavvia) { $decisione = 'riavvia'; $perche = '-riavvia' }
            elseif ($Prova) { $decisione = 'riusa'; $perche = T 'prova: la domanda non viene fatta' 'dry run: the question is not asked' }
            else {
                $risposta = Chiedi-Riavvio $elenco
                if ($risposta) { $decisione = $risposta; $perche = T 'risposta' 'answer' }
                else { $decisione = 'riusa'; $perche = T "nessuna risposta (entro $AttesaRispostaS s o nessuno a cui chiedere): non si ferma un server senza risposta" "no answer (within $AttesaRispostaS s, or nobody to ask): a server is never stopped without an answer" }
            }
            Scrivi (T 'Parametri di caricamento diversi da quelli del server acceso:' 'Loading parameters differ from the running server''s:') Yellow
            Scrivi $elenco
            Scrivi "$(T 'Deciso' 'Decided'): $(if ($decisione -eq 'riavvia') { T 'riavvio con i nuovi parametri' 'restart with the new parameters' } else { T 'riuso il server avviato' 'reuse the running server' }) ($perche)" Cyan
        } elseif ($cmp.Codice -eq 0) {
            Scrivi (T 'Il server acceso ha già i parametri di caricamento chiesti.' 'The running server already has the loading parameters asked for.') Green
        } else {
            Scrivi "$(T 'Non riesco a confrontare i parametri con il server acceso; lo riuso' 'Cannot compare the parameters with the running server; it is reused'): $($cmp.Righe -join ' ')" DarkYellow
        }
        if ($decisione -eq 'riavvia' -and $Prova) {
            Scrivi (T "PROVA: fermerei solo il server sulla porta $($s.Porta) (Ferma-Basalt.ps1 --port $($s.Porta)) e lo riavvierei con i nuovi parametri." "DRY RUN: only the server on port $($s.Porta) would be stopped (Ferma-Basalt.ps1 --port $($s.Porta)) and started again with the new parameters.") Yellow
            Esci 0
        }
        if ($decisione -eq 'riavvia') {
            Scrivi (T "Fermo solo il server sulla porta $($s.Porta)..." "Stopping only the server on port $($s.Porta)...") Cyan
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Radice 'Ferma-Basalt.ps1') --port "$($s.Porta)" --lang $Lingua
            if ($LASTEXITCODE -ne 0) { Scrivi (T 'Il server non si è fermato: non ne avvio un altro.' 'The server did not stop: no other is started.') Red; Esci 1 }
            if (-not $Port) { $Port = [int]$s.Porta }        # di nuovo sulla stessa porta, se non è stata chiesta un'altra
            $accesi = @()
        }
    }
}
if ($accesi.Count) {
    Scrivi (T 'Basalt Win+GUI è già acceso: mi collego a quello, non ne avvio un altro.' 'Basalt Win+GUI is already running: connecting to it, no other is started.') Yellow
    Scrivi "  $(T 'indirizzo: ' 'address:   ') $url"
    $carico = if ($s.Salute.loaded) { T 'caricato' 'loaded' } else { T 'non ancora caricato' 'not loaded yet' }
    Scrivi "  $(T 'modello:   ' 'model:     ') $($s.Salute.model) ($(T 'contesto' 'context') $($s.Salute.max_context) token, $carico)"
    if ($imp -and $imp.running) {
        $r = $imp.running
        Scrivi "  $(T 'impostazioni in uso' 'settings in use'): kv $($r.'engine.kv'), spec $($r.'engine.spec'), $(T 'cache esperti' 'expert cache') $($r.'engine.expert-cache'), slot $($r.'server.parallel')"
        if ($imp.config) { Scrivi "  $(T 'configurazione' 'config file'): $($imp.config)" }
    }
    $pidFile0 = Join-Path $Radice 'basalt.pid'
    if (Test-Path $pidFile0) {
        try {
            $info0 = Get-Content -Raw $pidFile0 | ConvertFrom-Json
            if ($info0.PSObject.Properties['profile'] -and $info0.profile -and $info0.PSObject.Properties['port'] -and [int]$info0.port -eq $s.Porta) { Scrivi "  $(T 'profilo:   ' 'profile:   ') $($info0.profile)" }
        } catch { }
    }
    if ($imp -and $imp.PSObject.Properties['model'] -and $imp.model) { Scrivi "  file:       $($imp.model)" }
    if ($accesi.Count -gt 1) { Scrivi "  $(T 'altri server Basalt Win+GUI accesi sulle porte' 'other Basalt Win+GUI servers running on ports'): $(($accesi | Select-Object -Skip 1 | ForEach-Object { $_.Porta }) -join ', ')" }
    if (-not $Server) {
        if ($Config) { Scrivi "  $(T "(il profilo «$Config» non è stato applicato: per cambiarlo usa il Loader o Ferma-Basalt.ps1 e poi riavvia)" "(the profile “$Config” was not applied: to change it use the Loader, or Ferma-Basalt.ps1 and then start again)")" DarkYellow }
        if ($Model) {
            $inUso = if ($imp -and $imp.PSObject.Properties['model']) { [string]$imp.model } else { '' }
            if (-not $inUso -or ([System.IO.Path]::GetFullPath($Model) -ne [System.IO.Path]::GetFullPath($inUso))) {
                Scrivi "  $(T "(il modello «$Model» NON è stato caricato: questo server serve un altro modello. Per cambiarlo: Ferma-Basalt.ps1 --port $($s.Porta), poi di nuovo Avvia-Basalt.ps1 --model ...)" "(the model “$Model” was NOT loaded: this server serves another model. To change it: Ferma-Basalt.ps1 --port $($s.Porta), then Avvia-Basalt.ps1 --model ... again)")" DarkYellow
            }
        }
    }
    if ($PerApp) { Esci 3 }                                # chi voleva avviarlo deve collegarsi a questo
    if ($Browser -and -not $Prova) { Start-Process $url }
    Esci 0
}

# ------------------------------------------------------------------ profilo e configurazione
if (-not (Test-Path $cfgPath)) { Scrivi (T "Manca il file di configurazione $cfgPath" "The config file is missing: $cfgPath") Red; Esci 1 }
if (-not (Test-Path $py)) { Scrivi (T "Manca il Python del venv: $py" "The venv's Python is missing: $py") Red; Esci 1 }
if (-not $Config) { $Config = ((Invoke-Py @('-m', 'serve.profiles', 'default')).Righe | Select-Object -Last 1).Trim() }
$nomi = (Invoke-Py @('-m', 'serve.profiles', 'names')).Righe -join '' | ConvertFrom-Json
if ($nomi -notcontains $Config) {
    Scrivi (T "Profilo sconosciuto: «$Config». Profili disponibili:" "Unknown profile: “$Config”. Profiles:") Red
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
try { $scelta = $rs.Righe | Select-Object -Last 1 | ConvertFrom-Json } catch { Scrivi (T 'Non riesco a scegliere il modello:' 'Cannot choose the model:') Red; $rs.Righe | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }
$fonte = if ($scelta.source) { Say $scelta.source_text } else { '' }
if ($rs.Codice -eq 7) { Scrivi (Say $scelta.reason) Red; Esci 1 }
if ($rs.Codice -eq 6) {
    Scrivi (T "Il modello ($fonte) non si può usare: $($scelta.model)" "The model ($fonte) cannot be used: $($scelta.model)") Red
    Scrivi "  $(Say $scelta.reason)" Red
    Esci 1
}
if ($rs.Codice -eq 5) {
    Scrivi (T "Il modello ($fonte) è un GGUF da convertire in .basalt: $($scelta.model)" "The model ($fonte) is a GGUF to convert to .basalt: $($scelta.model)") Yellow
    if (-not $Converti) {
        Scrivi "  $(T 'Per convertirlo:' 'To convert it:') .\Avvia-Basalt.ps1 --model `"$($scelta.model)`" -converti $(T '(chiede conferma e dice quanto scrive),' '(asks first and says how much it writes),')" Yellow
        Scrivi "  $(T 'oppure dall''app o dalla pagina Carica modello: Converti.' 'or from the app or the Loader page: Converti.')" Yellow
        Esci 1
    }
    $pc = Invoke-Py @('-m', 'serve.models', 'convert', '--model', $scelta.model)
    try { $piano = $pc.Righe | Select-Object -Last 1 | ConvertFrom-Json } catch { $pc.Righe | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }
    Scrivi "  $(Say $piano.summary)"
    Scrivi "  $(T 'file nuovo' 'new file'): $($piano.out_path)"
    foreach ($x in @($piano.problems)) { Scrivi "  $(if ($Lingua -eq 'it') { $x.text_it } else { $x.text })" Red }
    if (-not $piano.ok) { Scrivi (T 'Conversione non possibile ora.' 'The conversion is not possible now.') Red; Esci 1 }
    if ($Prova) { Scrivi "  $(T '(PROVA: la conversione non parte)' '(DRY RUN: the conversion does not start)')" Yellow; Esci 0 }
    $gbConv = [math]::Round($piano.estimate_bytes / 1GB, 1)
    $ok = Read-Host (T "Converto? Scrive circa $gbConv GB sul disco $($piano.disk) [s/N]" "Convert? It writes about $gbConv GB to disk $($piano.disk) [y/N]")
    if ($ok -notmatch '^(s|si|sì|y|yes)$') { Scrivi (T 'Conversione annullata.' 'Conversion cancelled.') Yellow; Esci 1 }
    Push-Location $Radice
    try { & $py -m serve.models convert --model $scelta.model --out $piano.out_dir --yes --token $piano.token; $codConv = $LASTEXITCODE } finally { Pop-Location }
    if ($codConv -ne 0) { Scrivi (T 'La conversione non è riuscita (vedi sopra).' 'The conversion failed (see above).') Red; Esci 1 }
    $scelta.model = $piano.out_path
    $fonte = T 'il file appena convertito' 'the file just converted'
}
$argA = @('-m', 'serve.profiles', 'apply', $Config, '--config', $usato, '--model', $scelta.model)
if ($RiservaMib -ge 0) { $argA += @('--vram-reserve-mib', "$RiservaMib") }
$ap = Invoke-Py $argA
if ($ap.Codice -ne 0) { Scrivi (T "Il profilo «$Config» non si può applicare:" "The profile “$Config” cannot be applied:") Red; $ap.Righe | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }
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
$modo = if ($Server) { 'server' } elseif ($PerApp) { 'per-app' } else { 'browser' }

Scrivi "Basalt Win+GUI - $(T 'avvio' 'start') -$modo $(if ($Prova) { T '(PROVA: non parte nulla)' '(DRY RUN: nothing starts)' })" Cyan
Scrivi "  $(T 'profilo:        ' 'profile:        ') $Config"
Scrivi "  $(T 'configurazione: ' 'config file:    ') $cfgPath$(if ($Prova) { T ' (in prova: una copia)' ' (dry run: a copy)' })"
Scrivi "  $(T 'modello:        ' 'model:          ') $modello ($fonte)"
Scrivi "  $(T 'indirizzo:      ' 'address:        ') $url"

$asc = Get-Ascoltatore $porta
if ($asc) { Scrivi (T "La porta $porta è occupata da $($asc.Name) (PID $($asc.ProcessId)): non avvio Basalt Win+GUI (--port N per un'altra)." "Port $porta is taken by $($asc.Name) (PID $($asc.ProcessId)): Basalt Win+GUI is not started (--port N for another).") Red; Esci 1 }
if ((Test-Path $pidFile) -and -not $env:BASALT_SCAN_PORTS) {               # con BASALT_SCAN_PORTS può essere di un altro server
    if (-not $Prova) { Remove-Item $pidFile -ErrorAction SilentlyContinue }   # di un avvio finito male
    Scrivi "  $(T '(basalt.pid di un avvio precedente: rimosso)' '(basalt.pid of an earlier start: removed)')" DarkGray
}

# ------------------------------------------------------------------ file necessari
$mancano = @()
foreach ($f in @($exe, $modello, (Join-Path $Radice 'tools\windows\BasaltLauncher.cs'))) {
    if (-not (Test-Path $f)) { $mancano += $f }
}
if ($mancano) { Scrivi (T 'Mancano questi file:' 'These files are missing:') Red; $mancano | ForEach-Object { Scrivi "  $_" Red }; Esci 1 }

# ------------------------------------------------------------------ memoria: gli stessi calcoli del Loader
$gpu = 0
if ($srv.PSObject.Properties['gpu']) { $gpu = [int](([string]$srv.gpu).Split(',')[0]) }
Scrivi ''
Scrivi (T 'Memoria (gli stessi calcoli del Loader):' 'Memory (the Loader''s own computation):') Cyan
$ck = Invoke-Py @('-m', 'serve.loader', 'check', '--config', $usato, '--gpu', "$gpu")
$ck.Righe | Select-Object -SkipLast 1 | ForEach-Object { Scrivi $_ $(if ($_ -match '^  (Non entra|Manca|La tabella|GPU NON|Does not fit|Not enough|The PLE table|UNSUPPORTED GPU)') { 'Red' } elseif ($_ -match '^  (Attenzione|Warning)') { 'Yellow' } else { 'Gray' }) }
$memOk = $ck.Codice -eq 0
if ($ck.Codice -eq 2) { Scrivi "  $(T 'configurazione non leggibile' 'the config file cannot be read')" Red; Esci 1 }
if ($ck.Codice -eq 4) { Scrivi (T 'Basalt Win+GUI NON viene avviato: il motore non gira su questa GPU (servono GPU NVIDIA Blackwell).' 'Basalt Win+GUI is NOT started: the engine does not run on this GPU (NVIDIA Blackwell GPUs are needed).') Red; Esci 1 }
if ($memOk) { Scrivi '  OK' Green }
if ($Prova) { Remove-Item $usato -ErrorAction SilentlyContinue; Remove-Item "$usato.bak" -ErrorAction SilentlyContinue }

if (-not $memOk -and -not $Prova) {
    Scrivi ''
    Scrivi (T 'Basalt Win+GUI NON viene avviato: la memoria non basta senza usare il file di paging (scegli un altro profilo nel Loader o chiudi altri programmi).' 'Basalt Win+GUI is NOT started: the memory is not enough without the page file (choose another profile in the Loader or close other programs).') Red
    Esci 1
}

# ------------------------------------------------------------------ ambiente e comando
$legato = -not $Server
# legato a questa finestra: se dal Loader si sceglie un altro modello il server esce con 75 e qui riparte con quello
$cmdLine = '"' + $exe + '" serve --config "' + $cfgPath + '"' + $(if ($Port) { " --port $Port" } else { '' }) +
    $(if ($legato) { ' --relaunch-on-model-change' } else { '' })
$cache = if ($env:BASALT_CACHE) { $env:BASALT_CACHE } else { Join-Path $HOME '.cache\basalt' }
$modoTesto = if ($Server) { T 'solo server, staccato e senza browser: lo script ritorna quando il modello è carico (fermare con Ferma-Basalt.ps1)' 'server alone, detached, no browser: the script returns once the model is loaded (stop it with Ferma-Basalt.ps1)' }
             elseif ($PerApp) { T 'legato al programma che l''ha avviato, senza interfaccia' 'tied to the program that started it, no interface' }
             else { T 'legato a questa finestra, interfaccia nel browser (Ctrl+C o chiudere la finestra lo ferma)' 'tied to this window, the interface in the browser (Ctrl+C or closing the window stops it)' }
Scrivi ''
Scrivi (T 'Avvio:' 'Start:') Cyan
Scrivi "  $(T 'comando:     ' 'command:     ') $cmdLine"
Scrivi "  $(T 'cartella:    ' 'folder:      ') $Radice"
Scrivi "  $(T 'modo:        ' 'mode:        ') $modoTesto"
Scrivi "  $(T 'log server:  ' 'server log:  ') $log"
Scrivi "  $(T 'log motore:  ' 'engine log:  ') $cache\<$(T 'modello' 'model')>\engine.log"

Add-Type -Path (Join-Path $Radice 'tools\windows\BasaltLauncher.cs')    # verifica anche in prova che compili

if ($Prova) {
    Scrivi ''
    if ($memOk) {
        Scrivi (T 'PROVA completata: tutti i controlli sono passati, nessun processo avviato.' 'DRY RUN done: every check passed, no process started.') Green
        Esci 0
    }
    Scrivi (T 'PROVA completata, nessun processo avviato: in questo momento un avvio vero sarebbe RIFIUTATO (memoria).' 'DRY RUN done, no process started: right now a real start would be REFUSED (memory).') Yellow
    Esci 1
}

foreach ($v in 'CL', '_CL_') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }   # opzioni nascoste del compilatore
$venv = Join-Path $Radice '.venv'
$env:VIRTUAL_ENV = $venv                                                              # il venv, senza Activate.ps1
$env:PATH = (Join-Path $venv 'Scripts') + ';' + $env:PATH
New-Item -ItemType Directory -Force $logDir | Out-Null
function Avvia-Server {
    if (Test-Path $log) { Move-Item -Force $log "$log.1" }                            # il log dell'avvio precedente
    $id = [BasaltLauncher]::Start($exe, $cmdLine, $Radice, $log, $legato)
    $proc = Get-Process -Id $id
    @{ pid = $id; start = $proc.StartTime.ToString('o'); mode = $modo; port = $porta; profile = $Config; log = $log } |
        ConvertTo-Json | Set-Content -Encoding UTF8 $pidFile
    Scrivi "  $(T 'avviato' 'started'): PID $id" Green
    Scrivi ''
    return $id
}
$idServer = Avvia-Server

# ------------------------------------------------------------------ log dal vivo, attesa, browser
function Apri-Log {
    $fs = [System.IO.File]::Open($log, 'OpenOrCreate', 'Read', 'ReadWrite, Delete')
    return New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
}
$lettore = Apri-Log
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
            $secondi = [int]((Get-Date) - $t0).TotalSeconds
            Scrivi (T "Modello pronto dopo $secondi s." "Model ready after $secondi s.") Green
        }
    }
}

try {
    while ($true) {
        Mostra-Log
        $codice = [BasaltLauncher]::ExitCode()
        if ($codice -eq $RiavvioModello -and $legato) {
            Mostra-Log
            Scrivi (T 'Il Loader ha scelto un altro modello: riavvio Basalt Win+GUI con quello (la pagina si ricollega da sola).' 'The Loader chose another model: Basalt Win+GUI starts again with it (the page reconnects by itself).') Cyan
            $lettore.Dispose()
            $idServer = Avvia-Server
            $lettore = Apri-Log
            $t0 = Get-Date
            continue
        }
        if ($codice -ne -1) {
            Mostra-Log
            Scrivi (T "Il server si è fermato (codice $codice)." "The server stopped (code $codice).") $(if ($codice -eq 0) { 'Yellow' } else { 'Red' })
            $fine = $true
            break
        }
        if ($Server) {
            $h = Get-Json ($url + 'health')
            if ($h -and $h.loaded) {
                Mostra-Log
                Scrivi (T "Server pronto con il modello caricato: $url (solo server, PID $idServer). Per fermarlo: Ferma-Basalt.ps1 --port $porta" "Server ready with the model loaded: $url (server alone, PID $idServer). To stop it: Ferma-Basalt.ps1 --port $porta") Green
                if ($ApriDopo) { Start-Process $url }
                break
            }
            if (((Get-Date) - $t0).TotalSeconds -gt $AttesaCaricoS) {
                Scrivi (T "Il modello non è carico dopo $AttesaCaricoS s: lo fermo." "The model is not loaded after $AttesaCaricoS s: stopping it.") Red
                & taskkill.exe /PID $idServer /T /F | Out-Null
                $fine = $true
                break
            }
        } elseif (-not $aperto) {
            if (Test-Http ($url + 'health')) {
                $aperto = $true
                if ($Browser) {
                    Scrivi (T "Interfaccia web pronta: apro $url (il modello si carica: vedi Monitor)" "Web interface ready: opening $url (the model loads: see Monitor)") Green
                    Start-Process $url
                } else {
                    Scrivi (T "Server pronto su $url (il modello si carica: vedi Monitor)" "Server ready on $url (the model loads: see Monitor)") Green
                }
            } elseif (((Get-Date) - $t0).TotalSeconds -gt $AttesaMaxS) {
                Scrivi (T "Il server non risponde dopo $AttesaMaxS s: lo fermo." "The server does not answer after $AttesaMaxS s: stopping it.") Red
                [BasaltLauncher]::KillAll()
                $fine = $true
                break
            }
        }
        Start-Sleep -Milliseconds 300
    }
} finally {
    if ($legato -and [BasaltLauncher]::ExitCode() -eq -1) {
        # Ctrl+C: il server l'ha ricevuto anche lui e si ferma da solo (QUIT al motore); poi il job chiude il resto
        Scrivi (T 'Arresto di Basalt Win+GUI in corso...' 'Stopping Basalt Win+GUI...') Yellow
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
        Scrivi (T "Basalt Win+GUI fermato. RAM disponibile ora: $(GiB ([double]$dopo.AvailableBytes))." "Basalt Win+GUI stopped. RAM available now: $(GiB ([double]$dopo.AvailableBytes)).") Cyan
    }
}
if ($fine -and [BasaltLauncher]::ExitCode() -ne 0) { Esci 1 }
Esci 0
