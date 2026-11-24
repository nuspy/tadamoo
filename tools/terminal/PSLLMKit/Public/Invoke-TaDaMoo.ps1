# Public/Invoke-TaDaMoo.ps1 - `tadamoo`: a TaDaMoo agent working in the current folder, like a coding agent in the terminal

function Invoke-TaDaMoo {
    <#
    .SYNOPSIS
        tadamoo "task"   does one task in the current folder and prints the result.
        tadamoo          opens a conversation with the agent in the current folder; an empty line or `exit` leaves it.
        A command the agent wants in your shell is left on the prompt, waiting for Enter.
    #>
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments)][string[]]$Task)
    $s = Get-TaDaMooSettings
    $cwd = (Get-Location -PSProvider FileSystem).ProviderPath
    New-TaDaMooSession $s $cwd
    $pending = ''
    try {
        if ($Task -and ($Task -join ' ').Trim()) {
            $pending = Send-TaDaMooMessage (($Task -join ' ').Trim()) $s
        } else {
            [Console]::Out.WriteLine("TaDaMoo in $cwd. Write what you want; an empty line leaves.")
            while ($true) {
                $line = Read-Host 'tadamoo'
                if (-not $line -or $line.Trim() -in 'exit', 'quit') { break }
                $got = Send-TaDaMooMessage $line.Trim() $s
                if ($got) { $pending = $got }
            }
        }
    } catch { Write-TaDaMooStep ([string]$_) }
    finally { Stop-TaDaMooSession $s }
    if ($pending) { $script:TdmState.Pending = $pending; Set-TaDaMooPending }
}

Set-Alias -Name tadamoo -Value Invoke-TaDaMoo
