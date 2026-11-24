# Private/TaDaMoo.ps1 - the terminal as a face of the TaDaMoo harness: a line that starts with the prefix goes to an agent, its answer is printed under the prompt.
# Written to run in Windows PowerShell 5.1 and in PowerShell 7. The file is plain ASCII: the prefix and the bar are built from their code points.

$script:TdmDefaults = [ordered]@{
    prefix = [string][char]0x00A7           # the section sign
    color  = '#2E7BFF'                      # the colour of the agent's result, electric blue
    server = 'http://127.0.0.1:7232'
    key    = ''
    mode   = ''                             # empty: the level the server gives the chat; a level above the one set in Settings is refused by the server
}
$script:TdmState = @{ Session = $null; Cwd = ''; Mark = 0; Transcript = ''; Pending = ''; Seq = 0 }

function Get-TaDaMooSettingsPath {
    $base = if ($env:APPDATA) { Join-Path $env:APPDATA 'TaDaMoo' } else { Join-Path $HOME '.tadamoo' }
    Join-Path $base 'terminal.json'
}

function Get-TaDaMooSettings {
    $out = @{}
    foreach ($k in $script:TdmDefaults.Keys) { $out[$k] = $script:TdmDefaults[$k] }
    $path = Get-TaDaMooSettingsPath
    if (Test-Path -LiteralPath $path) {
        try {
            $raw = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
            foreach ($k in @($out.Keys)) { if ($raw.PSObject.Properties.Name -contains $k -and "$($raw.$k)".Length -gt 0) { $out[$k] = "$($raw.$k)" } }
        } catch { }
    }
    $out
}

# -- colour ---------------------------------------------------------------------------------------------------------

function ConvertTo-TaDaMooAnsi([string]$Hex) {
    $h = $Hex.TrimStart('#')
    if ($h -notmatch '^[0-9a-fA-F]{6}$') { $h = '2E7BFF' }
    $r = [Convert]::ToInt32($h.Substring(0, 2), 16); $g = [Convert]::ToInt32($h.Substring(2, 2), 16); $b = [Convert]::ToInt32($h.Substring(4, 2), 16)
    "$([char]27)[38;2;$r;$g;${b}m"
}

function Write-TaDaMooResult([string]$Text, [hashtable]$Settings) {
    # the bar on the left keeps the agent's words recognisable where there is no colour
    $on = ConvertTo-TaDaMooAnsi $Settings.color; $off = "$([char]27)[0m"
    foreach ($line in ($Text -split "\r?\n")) { [Console]::Out.WriteLine("$on$([char]0x2502) $line$off") }
}

function Write-TaDaMooStep([string]$Text) {
    [Console]::Out.WriteLine("$([char]27)[90m  $Text$([char]27)[0m")
}

# -- the server -----------------------------------------------------------------------------------------------------

function Invoke-TaDaMooApi {
    param([string]$Method, [string]$Path, $Body, [hashtable]$Settings)
    $uri = $Settings.server.TrimEnd('/') + $Path
    $headers = @{}
    if ($Settings.key) { $headers['Authorization'] = "Bearer $($Settings.key)" }
    $args = @{ Uri = $uri; Method = $Method; Headers = $headers; UseBasicParsing = $true; ErrorAction = 'Stop'; TimeoutSec = 60 }
    if ($null -ne $Body) {
        $args['Body'] = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8 -Compress))
        $args['ContentType'] = 'application/json; charset=utf-8'
    }
    try { $resp = Invoke-WebRequest @args } catch {
        $msg = $_.Exception.Message
        $body = if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { '' }
        if (-not $body -and $_.Exception.Response -and ($_.Exception.Response | Get-Member -Name GetResponseStream -ErrorAction SilentlyContinue)) {
            try { $body = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd() } catch { }
        }
        if ($body) { try { $j = $body | ConvertFrom-Json; if ($j.error.message) { $msg = [string]$j.error.message } } catch { } }
        throw "TaDaMoo ($($Settings.server)): $msg"
    }
    [Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()) | ConvertFrom-Json
}

function Get-TaDaMooTerminalInfo {
    $v = $PSVersionTable.PSVersion
    $os = if ($PSVersionTable.PSObject.Properties.Name -contains 'OS' -and $PSVersionTable.OS) { [string]$PSVersionTable.OS } else { 'Windows' }
    $venv = if ($env:VIRTUAL_ENV) { $env:VIRTUAL_ENV } elseif ($env:CONDA_PREFIX) { $env:CONDA_PREFIX } else { '' }
    $cwd = (Get-Location -PSProvider FileSystem).ProviderPath
    @{ shell = 'PowerShell'; version = $v.ToString(); os = $os; cwd = $cwd; venv = $venv }
}

# -- the conversation of this terminal ------------------------------------------------------------------------------

function Stop-TaDaMooSession([hashtable]$Settings) {
    $id = $script:TdmState.Session
    if ($id) {
        try { $null = Invoke-TaDaMooApi -Method POST -Path "/api/agent/sessions/$id/close" -Body @{} -Settings $Settings } catch { }
        try { $null = Invoke-TaDaMooApi -Method POST -Path "/api/agent/sessions/$id/delete" -Body @{} -Settings $Settings } catch { }
    }
    $script:TdmState.Session = $null; $script:TdmState.Seq = 0
}

function New-TaDaMooSession([hashtable]$Settings, [string]$Cwd) {
    Stop-TaDaMooSession $Settings                            # the one before is dismissed and deleted
    $make = @{ workspace = $Cwd; worktree = 'off'; title = 'terminal'; channel = 'terminal' }
    if ($Settings.mode) { $make['mode'] = $Settings.mode }
    $made = Invoke-TaDaMooApi -Method POST -Path '/api/agent/sessions' -Settings $Settings -Body $make
    $id = if ($made.session -is [string]) { $made.session } elseif ($made.session.id) { $made.session.id } else { $made.id }
    if (-not $id) { throw 'TaDaMoo did not say which conversation it made.' }
    $script:TdmState.Session = [string]$id; $script:TdmState.Seq = 0
}

function Get-TaDaMooTranscriptSince {
    # what appeared in this window since the mark: the commands the person ran and what they printed (kept in the transcript file)
    $path = $script:TdmState.Transcript
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { return '' }
    try {
        $fs = New-Object IO.FileStream($path, 'Open', 'Read', 'ReadWrite')
        try {
            $from = [Math]::Min([int64]$script:TdmState.Mark, $fs.Length); $fs.Position = $from
            $text = (New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)).ReadToEnd()
        } finally { $fs.Dispose() }
    } catch { return '' }
    $text = ($text -split "\r?\n" | Where-Object { $_ -notmatch '^\*{10,}|^(Windows PowerShell|PowerShell) transcript (start|end)|^Start time:|^End time:|^Username:|^RunAs User:|^Configuration Name:|^Machine:|^Host Application:|^Process ID:|^PSVersion:|^PSEdition:|^GitCommitId:|^OS:|^Platform:|^PSCompatibleVersions:|^PSRemotingProtocolVersion:|^SerializationVersion:|^WSManStackVersion:' }) -join "`n"
    $text = $text.Trim()
    if ($text.Length -gt 20000) { $text = '[... earlier output left out]' + "`n" + $text.Substring($text.Length - 20000) }
    $text
}

function Set-TaDaMooMark {
    $path = $script:TdmState.Transcript
    $script:TdmState.Mark = if ($path -and (Test-Path -LiteralPath $path)) { (Get-Item -LiteralPath $path).Length } else { 0 }
}

function Send-TaDaMooAnswer($Ask, [hashtable]$Settings) {
    # a question the agent waits on (permission to run something): asked here, in the terminal; Enter alone is the last choice, which is a refusal
    $options = @($Ask.options | Where-Object { $_.value })
    if (-not $options) { Write-TaDaMooStep "the agent asks: $($Ask.question) (answer it in the TaDaMoo page)"; return }
    [Console]::Out.WriteLine("$([char]27)[33m? $($Ask.question)$([char]27)[0m")
    for ($i = 0; $i -lt $options.Count; $i++) { [Console]::Out.WriteLine(("  {0}. {1}" -f ($i + 1), $options[$i].label)) }
    $pick = Read-Host ("choose 1-{0} (Enter: {1})" -f $options.Count, $options[-1].label)
    $n = 0
    $chosen = if ([int]::TryParse($pick, [ref]$n) -and $n -ge 1 -and $n -le $options.Count) { $options[$n - 1] } else { $options[-1] }
    $null = Invoke-TaDaMooApi -Method POST -Path "/api/agent/sessions/$($script:TdmState.Session)/answer" -Body @{ ask = $Ask.id; value = $chosen.value } -Settings $Settings
}

function Send-TaDaMooMessage {
    # sends one message and prints what the agent does until it is done; returns the command it wants on the prompt, or ''
    param([string]$Text, [hashtable]$Settings)
    $id = $script:TdmState.Session
    $null = Invoke-TaDaMooApi -Method POST -Path "/api/agent/sessions/$id/send" -Body @{ text = $Text; terminal = (Get-TaDaMooTerminalInfo) } -Settings $Settings
    $command = ''; $last = ''; $finished = $false
    try {
        while ($true) {
            Start-Sleep -Milliseconds 400
            $got = Invoke-TaDaMooApi -Method GET -Path "/api/agent/sessions/$id/events?after=$($script:TdmState.Seq)" -Settings $Settings
            foreach ($e in @($got.events)) {
                if ($e.seq -gt $script:TdmState.Seq) { $script:TdmState.Seq = [int]$e.seq }
                switch ($e.event) {
                    'terminal_command' { $command = [string]$e.command }
                    'agent' {
                        $d = $e.data
                        if ($d.type -eq 'tool_start') {
                            $argText = (($d.arguments | ConvertTo-Json -Compress -Depth 3) -replace '\s+', ' ')
                            if ($argText.Length -gt 80) { $argText = $argText.Substring(0, 80) + '...' }
                            Write-TaDaMooStep ("{0} {1}" -f $d.name, $argText)
                        }
                        elseif ($d.type -eq 'assistant' -and $d.content) { $last = [string]$d.content; Write-TaDaMooResult $last $Settings }
                    }
                    'ask' { Send-TaDaMooAnswer $e $Settings }
                    'retrying' { Write-TaDaMooStep ("waiting for the model (attempt {0}): {1}" -f $e.attempt, ([string]$e.error).Substring(0, [Math]::Min(120, ([string]$e.error).Length))) }
                    'error' { Write-TaDaMooStep "error: $($e.message)" }
                }
            }
            if ($got.state -in 'idle', 'closed', 'dormant', 'blocked') { break }
        }
        $finished = $true
    } finally {
        if (-not $finished) { try { $null = Invoke-TaDaMooApi -Method POST -Path "/api/agent/sessions/$id/cancel" -Body @{} -Settings $Settings } catch { } }       # Ctrl+C stops the turn
    }
    if ($got.state -eq 'blocked') { Write-TaDaMooStep "the conversation is blocked: $($got.blocked)" }
    $command
}

function Invoke-TaDaMooLine {
    # one line that began with the prefix: one prefix starts a new conversation, two go on with the last one and bring the terminal's text since
    param([Parameter(Mandatory)][string]$Line)
    $s = Get-TaDaMooSettings; $p = $s.prefix
    $continue = $Line.StartsWith($p + $p)
    $body = $Line.Substring(($(if ($continue) { 2 * $p.Length } else { $p.Length }))).Trim()
    if (-not $body) { Write-TaDaMooStep "write what you want after $p ($p$p goes on with the last conversation)"; return }
    try {
        $info = Get-TaDaMooTerminalInfo
        if ($continue -and $script:TdmState.Session) {
            $seen = Get-TaDaMooTranscriptSince
            if ($seen) { $body += "`n`n[the terminal since your last answer: what the person ran and what it printed]`n" + $seen }
        } else {
            New-TaDaMooSession $s $info.cwd
        }
        $command = Send-TaDaMooMessage $body $s
        Set-TaDaMooMark
        if ($command) { $script:TdmState.Pending = $command }
    } catch { Write-TaDaMooStep ([string]$_) }
}

function Set-TaDaMooPending {
    # a command the agent wrote stays on the next prompt, waiting for Enter
    $cmd = $script:TdmState.Pending
    if (-not $cmd) { return }
    $script:TdmState.Pending = ''
    try { [Microsoft.PowerShell.PSConsoleReadLine]::Insert($cmd) } catch { Write-TaDaMooStep "for your prompt: $cmd" }
}
