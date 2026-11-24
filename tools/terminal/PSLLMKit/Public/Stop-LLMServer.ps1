# Public/Stop-LLMServer.ps1 - Stop server instances

function Stop-LLMServer {
    [CmdletBinding(DefaultParameterSetName = 'ByApp')]
    param(
        [Parameter(ParameterSetName = 'ByApp', Position = 0)]
        [string]$App,

        [Parameter(ParameterSetName = 'All')]
        [switch]$All
    )

    if ($All) {
        Stop-AllHttpServers
        Write-Host 'All LLM servers stopped.' -ForegroundColor Green
    }
    elseif ($App) {
        Stop-HttpServerInternal -App $App
        Write-Host "$App server stopped." -ForegroundColor Green
    }
    else {
        throw 'Specify -App <name> or -All.'
    }
}
