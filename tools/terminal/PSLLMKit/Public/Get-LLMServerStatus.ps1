# Public/Get-LLMServerStatus.ps1 - Show status of server instances

function Get-LLMServerStatus {
    [CmdletBinding()]
    param(
        [string]$App
    )

    if ($App) {
        $status = Get-HttpServerStatus -App $App
        if (-not $status.Running) {
            Write-Host "No instance running for '$App'." -ForegroundColor Yellow
            return [PSCustomObject]@{ App = $App; Running = $false }
        }
        return [PSCustomObject]@{
            App       = $status.App
            Running   = $true
            Url       = $status.Url
            Port      = $status.Port
            StartTime = $status.StartTime
            Uptime    = '{0:hh\:mm\:ss}' -f $status.Uptime
            SessionId = $status.SessionId
        }
    }

    # List all instances
    $statuses = Get-HttpServerStatus
    if (-not $statuses -or $statuses.Count -eq 0) {
        Write-Host 'No servers running.' -ForegroundColor Yellow
        return @()
    }

    $statuses | ForEach-Object {
        [PSCustomObject]@{
            App       = $_.App
            Running   = $true
            Url       = $_.Url
            Port      = $_.Port
            StartTime = $_.StartTime
            Uptime    = '{0:hh\:mm\:ss}' -f $_.Uptime
            SessionId = $_.SessionId
        }
    }
}
