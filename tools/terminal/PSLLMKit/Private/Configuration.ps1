# Private/Configuration.ps1 - Minimal configuration

$script:ConfigDir = Join-Path $env:USERPROFILE '.psllmkit'
$script:ConfigFile = Join-Path $script:ConfigDir 'config.json'

$script:DefaultConfig = @{
    DefaultProvider = 'claude'
    Port            = 8080
}

function Initialize-LLMConfig {
    if (-not (Test-Path $script:ConfigDir)) {
        New-Item -Path $script:ConfigDir -ItemType Directory -Force | Out-Null
    }
    if (-not (Test-Path $script:ConfigFile)) {
        $script:DefaultConfig | ConvertTo-Json -Depth 5 |
            Set-Content -Path $script:ConfigFile -Encoding UTF8
    }
}

function Read-LLMConfigFile {
    Initialize-LLMConfig
    $raw = Get-Content -Path $script:ConfigFile -Raw -ErrorAction SilentlyContinue
    if (-not $raw) { return $script:DefaultConfig.Clone() }
    try {
        $obj = $raw | ConvertFrom-Json
        $hash = @{}
        foreach ($prop in $obj.PSObject.Properties) { $hash[$prop.Name] = $prop.Value }
        return $hash
    }
    catch { return $script:DefaultConfig.Clone() }
}

function Write-LLMConfigFile {
    param([hashtable]$Config)
    Initialize-LLMConfig
    $Config | ConvertTo-Json -Depth 5 |
        Set-Content -Path $script:ConfigFile -Encoding UTF8
}
