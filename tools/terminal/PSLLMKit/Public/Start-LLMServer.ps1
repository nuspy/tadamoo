# Public/Start-LLMServer.ps1 - Start a dedicated server instance for one app

function Start-LLMServer {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$App,

        [Parameter(Mandatory)]
        [int]$Port
    )

    # Verify app is registered
    $tools = Get-RegisteredCLITools
    if (-not $tools.Contains($App)) {
        throw "Unknown app: '$App'. Available: $($tools.Keys -join ', ')"
    }

    # Verify app CLI is installed
    $installed = Get-InstalledCLITools
    if (-not $installed[$App].Available) {
        throw "CLI tool for '$App' ($($tools[$App].Command)) is not installed or not in PATH."
    }

    Start-HttpServerInternal -App $App -Port $Port

    $displayName = $tools[$App].DisplayName
    Write-Host "$displayName server running at http://localhost:${Port}" -ForegroundColor Green
    Write-Host ''
    Write-Host 'Endpoints:' -ForegroundColor Cyan
    Write-Host "  POST /api/prompt         { `"prompt`":`"...`" }"
    Write-Host '  GET  /api/status         instance info + session'
    Write-Host '  POST /api/session/reset  reset conversation'
    Write-Host '  GET  /api/health         health check'
    Write-Host ''
    Write-Host "Use Stop-LLMServer -App $App to shut down." -ForegroundColor DarkGray
}
