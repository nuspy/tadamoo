<#
.SYNOPSIS
    Installs the TaDaMoo window alone (no engine, no server on this PC) into C:\Program Files\Tadamoo\TaDaMooGUI, with a Start menu shortcut and an
    entry in "Installed apps". At its first start the app asks which server to use. Run it from the unzipped setup folder.
.PARAMETER Target
    Another install folder (default C:\Program Files\Tadamoo\TaDaMooGUI).
.PARAMETER NoShortcut
    Do not make the Start menu shortcut.
#>
[CmdletBinding()]
param([string]$Target = (Join-Path $env:ProgramFiles 'Tadamoo\TaDaMooGUI'), [switch]$NoShortcut)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath), '-Target', ('"{0}"' -f $Target))
    if ($NoShortcut) { $argList += '-NoShortcut' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList -Wait
    return
}

$here = $PSScriptRoot
if (-not (Test-Path (Join-Path $here 'TaDaMoo.exe')) -or -not (Test-Path (Join-Path $here 'gui-only.flag'))) { throw "run this from the setup folder: TaDaMoo.exe and gui-only.flag are not in $here" }

Get-Process -Name TaDaMoo -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($Target, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object {
    $_.CloseMainWindow() | Out-Null; if (-not $_.WaitForExit(5000)) { $_.Kill() }
}
New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item -Path (Join-Path $here '*') -Destination $Target -Recurse -Force
$exe = Join-Path $Target 'TaDaMoo.exe'

if (-not $NoShortcut) {
    $lnk = Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'TaDaMoo (window only).lnk'
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $sh.TargetPath = $exe; $sh.WorkingDirectory = $Target; $sh.IconLocation = "$exe,0"; $sh.Description = 'TaDaMoo: the window for a TaDaMoo server'
    $sh.Save()
}

$ver = (Get-Item $exe).VersionInfo.ProductVersion
$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\TaDaMooGUI'
New-Item -Path $key -Force | Out-Null
$props = @{
    DisplayName = 'TaDaMoo (window only)'; DisplayVersion = $ver; Publisher = 'Tadamoo'; InstallLocation = $Target; DisplayIcon = $exe
    UninstallString = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $Target 'Remove-TaDaMooGUI.ps1'))
}
foreach ($k in $props.Keys) { Set-ItemProperty -Path $key -Name $k -Value $props[$k] }
Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord

$webview = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue) -or
           (Get-ItemProperty 'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction SilentlyContinue)
Write-Host "Installed in $Target. Start it from the Start menu: it asks which server to use."
if (-not $webview) { Write-Host 'The WebView2 runtime was not found: the window needs it (https://developer.microsoft.com/microsoft-edge/webview2/). Without it the page opens in the browser.' -ForegroundColor Yellow }
