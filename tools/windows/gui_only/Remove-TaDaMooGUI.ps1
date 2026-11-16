<#
.SYNOPSIS
    Removes the window-only TaDaMoo: the program folder, the Start menu shortcut and the entry in "Installed apps". The list of servers
    (servers.json, in the user's app data) is left, so a new install finds it.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath)) -Wait
    return
}

$target = $PSScriptRoot
if (-not (Test-Path (Join-Path $target 'gui-only.flag'))) { throw "this is not the window-only install: $target" }
Get-Process -Name TaDaMoo -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($target, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object {
    $_.CloseMainWindow() | Out-Null; if (-not $_.WaitForExit(5000)) { $_.Kill() }
}
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'TaDaMoo (window only).lnk')
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\TaDaMooGUI'
# this script lives in the folder it removes: a separate process deletes the folder after this one ends
Start-Process cmd.exe -WindowStyle Hidden -ArgumentList @('/c', ('ping -n 3 127.0.0.1 >nul & rmdir /s /q "{0}"' -f $target))
Write-Host 'Removed.'
