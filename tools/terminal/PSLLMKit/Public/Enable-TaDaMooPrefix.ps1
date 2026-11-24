# Public/Enable-TaDaMooPrefix.ps1 - a line that starts with the prefix goes to the TaDaMoo harness instead of being run by this shell

function Enable-TaDaMooPrefix {
    <#
    .SYNOPSIS
        Makes Enter send a line that starts with the prefix (default: the section sign) to the TaDaMoo harness. One prefix starts a new conversation, two go on
        with the last one and bring along the terminal's text since the last answer. A command the agent wants in this shell is left on the prompt for Enter.
    #>
    [CmdletBinding()]
    param()
    # only in a window a person types in: not in a script, a pipe or a host without PSReadLine
    if ($Host.Name -ne 'ConsoleHost' -or [Console]::IsInputRedirected -or -not (Get-Command Set-PSReadLineKeyHandler -ErrorAction SilentlyContinue)) { return }
    $s = Get-TaDaMooSettings
    $script:TdmState.Transcript = Join-Path ([IO.Path]::GetTempPath()) ("tadamoo-terminal-{0}.txt" -f $PID)
    try { $null = Start-Transcript -Path $script:TdmState.Transcript -Force -ErrorAction Stop; Set-TaDaMooMark } catch { $script:TdmState.Transcript = '' }

    Set-PSReadLineKeyHandler -Key Enter -BriefDescription 'TaDaMooPrefix' -LongDescription 'A line that starts with the TaDaMoo prefix goes to the harness' -ScriptBlock {
        param($key, $arg)
        $line = $null; $cursor = $null
        [Microsoft.PowerShell.PSConsoleReadLine]::GetBufferState([ref]$line, [ref]$cursor)
        $prefix = (Get-TaDaMooSettings).prefix
        if ($line -and $line.TrimStart().StartsWith($prefix)) {
            $quoted = "'" + $line.Trim().Replace("'", "''") + "'"
            [Microsoft.PowerShell.PSConsoleReadLine]::RevertLine()
            [Microsoft.PowerShell.PSConsoleReadLine]::Insert("Invoke-TaDaMooLine -Line $quoted; Set-TaDaMooPending")
        }
        [Microsoft.PowerShell.PSConsoleReadLine]::AcceptLine()
    }
    # a command written by the agent is put on the next prompt as soon as the shell waits for input
    $null = Register-EngineEvent -SourceIdentifier 'PowerShell.OnIdle' -SupportEvent -Action { Set-TaDaMooPending }          # does nothing when nothing is waiting
    # the window's conversation and its transcript file go away with it
    $null = Register-EngineEvent -SourceIdentifier 'PowerShell.Exiting' -SupportEvent -Action { Close-TaDaMooTerminal }
}

function Close-TaDaMooTerminal {
    # the window's conversation is dismissed and its transcript file removed (a scriptblock registered as an event action cannot see this module's variables, so it calls this)
    [CmdletBinding()]
    param()
    try { Stop-TaDaMooSession (Get-TaDaMooSettings) } catch { }
    $file = $script:TdmState.Transcript
    if ($file) {
        try { $null = Stop-Transcript -ErrorAction Stop } catch { }
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        $script:TdmState.Transcript = ''
    }
}

function Disable-TaDaMooPrefix {
    [CmdletBinding()]
    param()
    Set-PSReadLineKeyHandler -Key Enter -Function AcceptLine
    Unregister-Event -SourceIdentifier 'PowerShell.OnIdle' -ErrorAction SilentlyContinue
    Unregister-Event -SourceIdentifier 'PowerShell.Exiting' -ErrorAction SilentlyContinue
    Close-TaDaMooTerminal
}
