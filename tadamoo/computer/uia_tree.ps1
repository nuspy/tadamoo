# The accessibility tree of one window, as JSON (a list of {r: role, n: name, x, y, w, h, d: depth, e: enabled, v: value}), by Windows UI Automation
# through the .NET assemblies every Windows has. Nothing is changed in the window; a password field's value is never read.
param([long]$Hwnd, [int]$Limit = 150)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$out = New-Object System.Collections.ArrayList
$script:count = 0

function Walk($el, $depth) {
    if ($script:count -ge $Limit -or $depth -gt 14) { return }
    $c = $el.Current
    $r = $c.BoundingRectangle
    if (-not $c.IsOffscreen -and $r.Width -gt 0 -and $r.Height -gt 0) {
        $script:count++
        $value = ''
        if (-not $c.IsPassword) {
            try { $value = [string]($el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).Current.Value } catch { $value = '' }
        }
        [void]$out.Add([ordered]@{
            r = ($c.ControlType.ProgrammaticName -replace '^ControlType\.', ''); n = [string]$c.Name
            x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height; d = $depth; e = [bool]$c.IsEnabled; v = $value
        })
    }
    $child = $walker.GetFirstChild($el)
    while ($null -ne $child -and $script:count -lt $Limit) {
        Walk $child ($depth + 1)
        $child = $walker.GetNextSibling($child)
    }
}

Walk $root 0
ConvertTo-Json -InputObject @($out) -Compress -Depth 4
