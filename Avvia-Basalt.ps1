<#
.SYNOPSIS
    Avvia Basalt: di norma apre l'app BasaltPannello (l'interfaccia principale), oppure il server con il browser, oppure
    il solo server per gli agenti.

.DESCRIPTION
    Tre modi, con gli stessi nomi di Avvia-Strata.ps1 (ogni opzione si scrive con uno o due trattini, maiuscole o
    minuscole come si vuole):

      -gui      (il modo predefinito, senza opzioni) apre l'app BasaltPannello.exe passandole --config, --model,
                --port e --vram-libera. L'app avvia il server senza caricare il modello e mostra il Loader: cartelle
                dei modelli, profili, tutti i valori, la previsione di memoria e velocità, la configurazione guidata,
                Carica. Il browser NON si apre, ma l'indirizzo resta disponibile e viene stampato. Se l'app non è
                ancora stata costruita lo script dice come costruirla e intanto usa -browser.
      -browser  il server legato a questa finestra e l'interfaccia nel browser, senza app: Ctrl+C o la X della
                finestra fermano server e motore e liberano la RAM (un Job Object di Windows chiude tutto anche se
                PowerShell viene terminato di colpo).
      -server   il solo server, staccato dalla finestra, senza app né browser: lo script ritorna quando il modello è
                carico (o se non parte). Serve agli agenti; si ferma con Ferma-Basalt.ps1 --port N.

    Se un server Basalt di questa cartella è già acceso non ne avvia un altro: con -gui apre l'app (che chiede se
    collegarsi o cambiare modello), con -browser apre il browser su quel server, con -server si collega e ritorna.

    Con -browser e -server lo script scrive in basalt_config.json i valori del profilo scelto (--config "<nome>",
    altrimenti il profilo predefinito: «Machine Default» se la configurazione guidata l'ha creato, altrimenti
    «Valori sicuri», a meno che un altro sia stato scelto come predefinito nel Loader; il file precedente resta in
    basalt_config.json.bak) e controlla con gli stessi calcoli del Loader che VRAM, RAM e memoria impegnabile
    (commit) bastino lasciando libera la parte di RAM e di commit scelta per gli altri programmi (15% se non è stata
    cambiata), senza far crescere il file di paging.

.PARAMETER Gui
    Apre l'app (il modo predefinito).

.PARAMETER Browser
    Server legato alla finestra e interfaccia nel browser.

.PARAMETER Server
    Solo server, staccato dalla finestra; ritorna quando il modello è carico.

.PARAMETER Config
    Il profilo di caricamento (--config "<nome>"). Senza: il profilo predefinito.

.PARAMETER Model
    Il file .basalt da servire, con il percorso completo (--model "<file>"). Senza: il modello del profilo, altrimenti
    il modello predefinito per il server scelto nell'app o nel Loader, altrimenti quello già nel file di
    configurazione. Un GGUF «Da convertire» si converte con -converti (dopo una conferma che dice quanto scrive e su
    quale disco); un file «Non compatibile» viene rifiutato con il motivo.

.PARAMETER Port
    La porta (--port N, 1..65535); vince su quella del profilo.

.PARAMETER VramLibera
    La VRAM da lasciare libera per gli altri programmi, in GB (--vram-libera 1.5 oppure "1,5"), al posto di quella
    del profilo.
    --vram-reserve-mib N (in MiB) resta come sinonimo.

.PARAMETER Converti
    Se il modello è un GGUF da convertire: mostra il piano (dimensione del nuovo file, disco, spazio libero), chiede
    conferma, converte e poi avvia il file .basalt ottenuto.

.PARAMETER Prova
    Fa tutti i controlli e dice cosa farebbe, senza avviare nulla e senza scrivere basalt_config.json.

.PARAMETER File
    Il file di configurazione (predefinito: basalt_config.json accanto a questo script).

.EXAMPLE
    .\Avvia-Basalt.ps1
.EXAMPLE
    .\Avvia-Basalt.ps1 -prova
.EXAMPLE
    .\Avvia-Basalt.ps1 -browser --config "Valori sicuri" --port 7240
.EXAMPLE
    .\Avvia-Basalt.ps1 -server --model "D:\Modelli\Qwen3.8-Flash-Next-IQ3_XXS.basalt" --vram-libera 2
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
$RiavvioModello = 75      # codice d'uscita del server quando il Loader ha scelto un altro modello: si riavvia
$Uso = 'uso: Avvia-Basalt.ps1 [-gui | -browser | -server] [--config "<profilo>"] [--model "<file .basalt>"] [--port N] [--vram-libera GB] [-converti] [-prova] [-file <configurazione>]'

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

# ------------------------------------------------------------------ le opzioni: -nome o --nome, --nome valore o --nome=valore
for ($i = 0; $i -lt $Altri.Count; $i++) {
    $a = $Altri[$i]
    if (-not $a.StartsWith('-')) { Scrivi "Argomento inatteso: $a ($Uso)" Red; Esci 1 }
    $parti = $a.TrimStart('-').Split([char[]]'=', 2)
    $nome = $parti[0].ToLowerInvariant()
    $inLinea = if ($parti.Count -gt 1) { $parti[1] } else { $null }
    $val = {
        if ($null -ne $inLinea) { return $inLinea }
        if ($script:i + 1 -lt $Altri.Count) { $script:i++; return $Altri[$script:i] }
        Scrivi "--$nome vuole un valore ($Uso)" Red; Esci 1
    }
    switch -Exact ($nome) {
        'gui' { $Gui = $true }
        'browser' { $Browser = $true }
        'server' { $Server = $true }
        'prova' { $Prova = $true }
        'converti' { $Converti = $true }
        'perapp' { $PerApp = $true }
        'sfondo' { $Sfondo = $true }
        { $_ -in 'solo-server', 'soloserver' } { $SoloServer = $true }
        'config' { $Config = & $val }
        'model' { $Model = & $val }
        'file' { $File = & $val }
        'port' {
            $v = & $val
            if (-not ($v -match '^\d+$')) { Scrivi "--port vuole un numero da 1 a 65535, non '$v'" Red; Esci 1 }
            $Port = [int]$v
        }
        { $_ -in 'vram-libera', 'vramlibera' } { $VramLibera = & $val }
        { $_ -in 'vram-reserve-mib', 'riservamib' } {
            $v = & $val
            if (-not ($v -match '^\d+$')) { Scrivi "--vram-reserve-mib vuole un numero di MiB, non '$v'" Red; Esci 1 }
            $RiservaMib = [int]$v
        }
        default { Scrivi "Argomento sconosciuto: $a ($Uso)" Red; Esci 1 }
    }
}
if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) { Scrivi "Porta non valida: $Port (1..65535)" Red; Esci 1 }
if ($VramLibera) {
    $gb = 0.0
    if (-not [double]::TryParse($VramLibera.Replace(',', '.'), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$gb) -or $gb -lt 0 -or $gb -gt 1024) {
        Scrivi "--vram-libera vuole i GB da lasciare liberi (per esempio 2 o 1,5), non '$VramLibera'" Red; Esci 1
    }
    if ($RiservaMib -ge 0) { Scrivi '--vram-libera e --vram-reserve-mib dicono la stessa cosa: usane uno solo' Red; Esci 1 }
    $RiservaMib = [int][math]::Round($gb * 1024)
}
$ApriDopo = $false                         # -Sfondo apriva il browser quando il server rispondeva
if ($Sfondo -or $SoloServer) {
    Scrivi "Nota: $(if ($Sfondo) { '-Sfondo' } else { '-SoloServer' }) è un nome vecchio: ora si scrive -server." DarkYellow
    $Server = $true
    $ApriDopo = [bool]$Sfondo
}
$modi = @(@($Gui, $Browser, $Server, $PerApp) | Where-Object { $_ })
if ($modi.Count -gt 1) { Scrivi "Scegli un solo modo tra -gui, -browser e -server ($Uso)" Red; Esci 1 }
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

$cfgPath = if ([System.IO.Path]::IsPathRooted($File)) { $File } else { Join-Path $Radice $File }
$accesi = @(Get-ServerAccesi)

# ------------------------------------------------------------------ -gui: l'app
$exeApp = Join-Path $Radice 'tools\windows\desktop\publish\BasaltPannello.exe'
if ($Gui -and -not (Test-Path $exeApp)) {
    Scrivi "L'app BasaltPannello non è ancora stata costruita ($exeApp)." Yellow
    Scrivi '  Per costruirla (serve il .NET 8 SDK):  dotnet publish tools\windows\desktop -c Release -o tools\windows\desktop\publish' Yellow
    Scrivi '  Intanto apro Basalt nel browser (-browser).' Yellow
    $Gui = $false
    $Browser = $true
}
if ($Gui) {
    $argApp = @()
    if ($Config) { $argApp += @('--config', $Config) }
    if ($Model) { $argApp += @('--model', $Model) }
    if ($Port) { $argApp += @('--port', "$Port") }
    if ($RiservaMib -ge 0) { $argApp += @('--vram-libera', ([double]$RiservaMib / 1024).ToString([Globalization.CultureInfo]::InvariantCulture)) }
    if ($File -ne 'basalt_config.json') { $argApp += @('--file', $cfgPath) }
    if ($Converti) { Scrivi '  (-converti vale con -browser e -server: nell''app la conversione è nel Loader, «Converti…»)' DarkYellow }
    $porta = 7232
    if ($accesi.Count) { $porta = $accesi[0].Porta }
    elseif ($Port) { $porta = $Port }
    elseif (Test-Path $cfgPath) {
        try { $srv0 = (Get-Content -Raw -Encoding UTF8 $cfgPath | ConvertFrom-Json).server; if ($srv0 -and $srv0.PSObject.Properties['port']) { $porta = [int]$srv0.port } } catch { }
    }
    $mostra = ($argApp | ForEach-Object { if ($_ -match '\s') { """$_""" } else { $_ } }) -join ' '
    Scrivi "Basalt - app $(if ($Prova) { '(PROVA: non parte nulla)' })" Cyan
    Scrivi "  app:        $exeApp $mostra"
    if ($accesi.Count) { Scrivi "  Basalt è già acceso sulla porta ${porta}: l'app chiede se collegarsi o cambiare modello." Yellow }
    Scrivi "  indirizzo:  http://127.0.0.1:$porta/  (il browser non si apre: l'indirizzo resta disponibile)"
    Scrivi '  L''app avvia il server senza caricare il modello e mostra il Loader; si chiude dall''icona vicino all''orologio (Spegni).'
    if ($Prova) { Scrivi 'PROVA completata: nessun processo avviato.' Green; Esci 0 }
    if ($argApp.Count) { Start-Process -FilePath $exeApp -ArgumentList $mostra -WorkingDirectory $Radice }
    else { Start-Process -FilePath $exeApp -WorkingDirectory $Radice }
    Esci 0
}

# ------------------------------------------------------------------ già acceso?
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
    if ($PerApp) { Esci 3 }                                # chi voleva avviarlo deve collegarsi a questo
    if ($Browser -and -not $Prova) { Start-Process $url }
    Esci 0
}

# ------------------------------------------------------------------ profilo e configurazione
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
        Scrivi "  Per convertirlo: .\Avvia-Basalt.ps1 --model `"$($scelta.model)`" -converti (chiede conferma e dice quanto scrive)," Yellow
        Scrivi "  oppure dall'app o dalla pagina Carica modello: Converti." Yellow
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
$modo = if ($Server) { 'server' } elseif ($PerApp) { 'per-app' } else { 'browser' }

Scrivi "Basalt - avvio -$modo $(if ($Prova) { '(PROVA: non parte nulla)' })" Cyan
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
$legato = -not $Server
# legato a questa finestra: se dal Loader si sceglie un altro modello il server esce con 75 e qui riparte con quello
$cmdLine = '"' + $exe + '" serve --config "' + $cfgPath + '"' + $(if ($Port) { " --port $Port" } else { '' }) +
    $(if ($legato) { ' --relaunch-on-model-change' } else { '' })
$cache = if ($env:BASALT_CACHE) { $env:BASALT_CACHE } else { Join-Path $HOME '.cache\basalt' }
Scrivi ''
Scrivi 'Avvio:' Cyan
Scrivi "  comando:      $cmdLine"
Scrivi "  cartella:     $Radice"
Scrivi "  modo:         $(if ($Server) { 'solo server, staccato e senza browser: lo script ritorna quando il modello è carico (fermare con Ferma-Basalt.ps1)' } elseif ($PerApp) { 'legato al programma che l''ha avviato, senza interfaccia' } else { 'legato a questa finestra, interfaccia nel browser (Ctrl+C o chiudere la finestra lo ferma)' })"
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
function Avvia-Server {
    if (Test-Path $log) { Move-Item -Force $log "$log.1" }                            # il log dell'avvio precedente
    $id = [BasaltLauncher]::Start($exe, $cmdLine, $Radice, $log, $legato)
    $proc = Get-Process -Id $id
    @{ pid = $id; start = $proc.StartTime.ToString('o'); mode = $modo; port = $porta; profile = $Config; log = $log } |
        ConvertTo-Json | Set-Content -Encoding UTF8 $pidFile
    Scrivi "  avviato: PID $id" Green
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
            Scrivi "Modello pronto dopo $([int]((Get-Date) - $t0).TotalSeconds) s." Green
        }
    }
}

try {
    while ($true) {
        Mostra-Log
        $codice = [BasaltLauncher]::ExitCode()
        if ($codice -eq $RiavvioModello -and $legato) {
            Mostra-Log
            Scrivi 'Il Loader ha scelto un altro modello: riavvio Basalt con quello (la pagina si ricollega da sola).' Cyan
            $lettore.Dispose()
            $idServer = Avvia-Server
            $lettore = Apri-Log
            $t0 = Get-Date
            continue
        }
        if ($codice -ne -1) {
            Mostra-Log
            Scrivi "Il server si è fermato (codice $codice)." $(if ($codice -eq 0) { 'Yellow' } else { 'Red' })
            $fine = $true
            break
        }
        if ($Server) {
            $h = Get-Json ($url + 'health')
            if ($h -and $h.loaded) {
                Mostra-Log
                Scrivi "Server pronto con il modello caricato: $url (solo server, PID $idServer). Per fermarlo: Ferma-Basalt.ps1 --port $porta" Green
                if ($ApriDopo) { Start-Process $url }
                break
            }
            if (((Get-Date) - $t0).TotalSeconds -gt $AttesaCaricoS) {
                Scrivi "Il modello non è carico dopo $AttesaCaricoS s: lo fermo." Red
                & taskkill.exe /PID $idServer /T /F | Out-Null
                $fine = $true
                break
            }
        } elseif (-not $aperto) {
            if (Test-Http ($url + 'health')) {
                $aperto = $true
                if ($Browser) {
                    Scrivi "Interfaccia web pronta: apro $url (il modello si carica: vedi Monitor)" Green
                    Start-Process $url
                } else {
                    Scrivi "Server pronto su $url (il modello si carica: vedi Monitor)" Green
                }
            } elseif (((Get-Date) - $t0).TotalSeconds -gt $AttesaMaxS) {
                Scrivi "Il server non risponde dopo $AttesaMaxS s: lo fermo." Red
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
