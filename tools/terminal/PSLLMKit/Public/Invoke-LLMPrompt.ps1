# Public/Invoke-LLMPrompt.ps1 - Direct CLI invocation (no HTTP server needed)

function Invoke-LLMPrompt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)]
        [string]$Prompt,

        [Parameter(Mandatory)]
        [string]$App,

        [string]$Model,

        [string]$SystemPrompt,

        [string]$SessionId
    )

    $result = Invoke-CLITool -Provider $App -Prompt $Prompt `
        -Model $Model -SystemPrompt $SystemPrompt -SessionId $SessionId

    return [PSCustomObject]@{
        PSTypeName = 'PSLLMKit.Response'
        Content    = $result.content
        App        = $result.provider
        Model      = $result.model
        SessionId  = $result.sessionId
        Duration   = $result.duration
        Timestamp  = $result.timestamp
    }
}
