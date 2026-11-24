# Private/ProcessRunner.ps1 - CLI tool registry and execution
# Each provider maps to an installed agentic coding CLI tool.
# The module never talks to LLM APIs directly; the CLI handles auth (OAuth).

$script:CLITools = @{
    claude = @{
        Command      = 'claude'
        DisplayName  = 'Claude (Anthropic)'
        BuildArgs    = {
            param($Prompt, $Model, $SystemPrompt, $SessionId)
            $a = [System.Collections.Generic.List[string]]::new()
            $a.Add('-p');               $a.Add($Prompt)
            $a.Add('--output-format');  $a.Add('json')
            if ($Model)        { $a.Add('--model');         $a.Add($Model) }
            if ($SystemPrompt) { $a.Add('--system-prompt'); $a.Add($SystemPrompt) }
            if ($SessionId)    { $a.Add('--resume');        $a.Add($SessionId) }
            $a.ToArray()
        }
        ParseOutput  = {
            param($RawOutput)
            try {
                $json = $RawOutput | ConvertFrom-Json
                @{ Content = $json.result; Raw = $json }
            }
            catch {
                @{ Content = $RawOutput; Raw = $null }
            }
        }
        ExtractSessionId = {
            param($Raw)
            if ($Raw -and $Raw.session_id) { $Raw.session_id } else { $null }
        }
        InputViaPipe = $false
    }

    codex = @{
        Command      = 'codex'
        DisplayName  = 'Codex (OpenAI)'
        BuildArgs    = {
            param($Prompt, $Model, $SystemPrompt, $SessionId)
            $a = [System.Collections.Generic.List[string]]::new()
            $a.Add('exec'); $a.Add('-')
            if ($Model) { $a.Add('--model'); $a.Add($Model) }
            $a.ToArray()
        }
        ParseOutput  = {
            param($RawOutput)
            @{ Content = $RawOutput; Raw = $null }
        }
        ExtractSessionId = $null
        InputViaPipe = $true
    }

    gemini = @{
        Command      = 'gemini'
        DisplayName  = 'Gemini (Google)'
        BuildArgs    = {
            param($Prompt, $Model, $SystemPrompt, $SessionId)
            $a = [System.Collections.Generic.List[string]]::new()
            $a.Add('-p'); $a.Add($Prompt)
            if ($Model) { $a.Add('--model'); $a.Add($Model) }
            $a.ToArray()
        }
        ParseOutput  = {
            param($RawOutput)
            @{ Content = $RawOutput; Raw = $null }
        }
        ExtractSessionId = $null
        InputViaPipe = $false
    }

    droid = @{
        Command      = 'droid'
        DisplayName  = 'Droid (Factory)'
        BuildArgs    = {
            param($Prompt, $Model, $SystemPrompt, $SessionId)
            $a = [System.Collections.Generic.List[string]]::new()
            $a.Add('exec')
            $a.Add($Prompt)
            $a.Add('-o'); $a.Add('json')
            if ($Model)     { $a.Add('-m'); $a.Add($Model) }
            if ($SessionId) { $a.Add('-s'); $a.Add($SessionId) }
            $a.ToArray()
        }
        ParseOutput  = {
            param($RawOutput)
            try {
                $json = $RawOutput | ConvertFrom-Json
                @{ Content = $json.result; Raw = $json }
            }
            catch {
                @{ Content = $RawOutput; Raw = $null }
            }
        }
        ExtractSessionId = {
            param($Raw)
            if ($Raw -and $Raw.session_id) { $Raw.session_id } else { $null }
        }
        InputViaPipe = $false
    }
}

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

function Get-RegisteredCLITools {
    return $script:CLITools
}

function Register-CLITool {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][hashtable]$Tool
    )
    foreach ($key in @('Command', 'BuildArgs', 'ParseOutput')) {
        if (-not $Tool.ContainsKey($key)) {
            throw "CLI tool '$Name' missing required key: $key"
        }
    }
    if (-not $Tool.ContainsKey('DisplayName'))       { $Tool.DisplayName       = $Name }
    if (-not $Tool.ContainsKey('InputViaPipe'))       { $Tool.InputViaPipe       = $false }
    if (-not $Tool.ContainsKey('ExtractSessionId'))   { $Tool.ExtractSessionId   = $null }
    $script:CLITools[$Name] = $Tool
}

function Get-InstalledCLITools {
    $result = @{}
    foreach ($name in $script:CLITools.Keys) {
        $tool = $script:CLITools[$name]
        $result[$name] = @{
            Name        = $name
            DisplayName = $tool.DisplayName
            Command     = $tool.Command
            Available   = [bool](Get-Command $tool.Command -ErrorAction SilentlyContinue)
        }
    }
    return $result
}

function Invoke-CLITool {
    param(
        [Parameter(Mandatory)][string]$Provider,
        [Parameter(Mandatory)][string]$Prompt,
        [string]$Model,
        [string]$SystemPrompt,
        [string]$SessionId
    )

    if (-not $script:CLITools.Contains($Provider)) {
        throw "Unknown provider: $Provider. Available: $($script:CLITools.Keys -join ', ')"
    }

    $tool = $script:CLITools[$Provider]
    $cmd  = $tool.Command

    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "CLI tool '$cmd' is not installed or not in PATH."
    }

    $cliArgs   = & $tool.BuildArgs $Prompt $Model $SystemPrompt $SessionId
    $startTime = [DateTime]::UtcNow

    try {
        if ($tool.InputViaPipe) {
            $rawOutput = $Prompt | & $cmd @cliArgs 2>&1 | Out-String
        }
        else {
            $rawOutput = & $cmd @cliArgs 2>&1 | Out-String
        }
    }
    catch {
        throw "CLI tool '$cmd' failed: $($_.Exception.Message)"
    }

    $elapsed = ([DateTime]::UtcNow - $startTime).TotalMilliseconds
    $parsed  = & $tool.ParseOutput $rawOutput.Trim()

    $extractedSessionId = $null
    if ($tool.ExtractSessionId) {
        $extractedSessionId = & $tool.ExtractSessionId $parsed.Raw
    }

    return @{
        content   = $parsed.Content
        provider  = $Provider
        model     = $Model
        sessionId = $extractedSessionId
        duration  = [Math]::Round($elapsed)
        raw       = $parsed.Raw
        timestamp = [DateTime]::UtcNow.ToString('o')
    }
}
