@{
    RootModule        = 'PSLLMKit.psm1'
    ModuleVersion     = '0.3.0'
    GUID              = '3f8e7d6c-5b4a-4321-9876-fedcba987654'
    Author            = 'PSLLMKit'
    CompanyName       = 'Community'
    Copyright         = '(c) 2026. All rights reserved.'
    Description       = 'Multi-instance REST API gateway for agentic coding CLI tools (Claude, Codex, Gemini, Droid). Each app runs as an independent server with session continuity.'
    PowerShellVersion = '5.1'
    FunctionsToExport = @(
        'Start-LLMServer'
        'Stop-LLMServer'
        'Get-LLMServerStatus'
        'Invoke-LLMPrompt'
        'Register-CLITool'
        'Get-InstalledCLITools'
        'Invoke-CLITool'
        'Enable-TaDaMooPrefix'
        'Disable-TaDaMooPrefix'
        'Close-TaDaMooTerminal'
    'Close-TaDaMooTerminal'
        'Invoke-TaDaMoo'
        'Invoke-TaDaMooLine'
        'Set-TaDaMooPending'
        'Get-TaDaMooSettings'
    )
    CmdletsToExport   = @()
    VariablesToExport  = @()
    AliasesToExport    = @('tadamoo')
    PrivateData       = @{
        PSData = @{
            Tags = @('LLM', 'AI', 'Claude', 'Codex', 'Gemini', 'Droid', 'Factory', 'REST', 'CLI', 'Gateway')
        }
    }
}
