<#
.SYNOPSIS
    Builds the GUI-only variant of the Windows setup: only the desktop window, no engine, no Python, no local server. At its first start it asks
    which server to use (the list of servers: name, http or https address, key). The servers it can show are the ones that already run elsewhere,
    for example a Linux install behind nginx.

.DESCRIPTION
    Makes, under dist\gui-only\ (ignored by git):

      stage\TaDaMooGUI\                         the app (self-contained .NET 8), the file gui-only.flag that makes it start as the window alone,
                                                and Install-TaDaMooGUI.ps1 / Remove-TaDaMooGUI.ps1
      TaDaMooGUI-<release>-setup.zip            the same folder, zipped, to hand to a PC

    On the PC: unzip, right-click Install-TaDaMooGUI.ps1 > Run with PowerShell (it asks for administrator rights). It copies the folder to
    C:\Program Files\Tadamoo\TaDaMooGUI, adds a Start menu shortcut and an entry in "Installed apps". Nothing is downloaded and nothing is
    uploaded anywhere. The window needs the WebView2 runtime (part of Windows 11; the install script says so when it is missing and the app
    then opens the page in the browser instead).

    The full setup (the engine, Python and the server on this PC) is compile_and_package.ps1 at the top of the repository.
.PARAMETER OutDir
    Where to build (default dist\gui-only).
#>
[CmdletBinding()]
param([string]$OutDir = 'dist\gui-only')

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Out = if ([IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $Root $OutDir }
$Stage = Join-Path $Out 'stage\TaDaMooGUI'
$release = ''; $version = '1.0'
$versionFile = Join-Path $Root 'VERSION'
if (Test-Path $versionFile) {
    $v = Get-Content -Raw -Encoding UTF8 $versionFile | ConvertFrom-Json
    $release = [string]$v.release; $version = '{0}.{1}' -f $v.version, $v.subversion
}
$label = if ($release) { $release } else { "v$version" }

if (Test-Path (Join-Path $Out 'stage')) { Remove-Item -Recurse -Force (Join-Path $Out 'stage') }
New-Item -ItemType Directory -Force $Stage | Out-Null

& dotnet publish (Join-Path $Root 'tools\windows\desktop\TaDaMooDesktop.csproj') -c Release -r win-x64 --self-contained true -o $Stage -nologo -v q `
    -p:DebugType=None -p:DebugSymbols=false -p:NoWarn=MSB3277 "-p:Version=$version.0" "-p:InformationalVersion=$label" -p:IncludeSourceRevisionInInformationalVersion=false
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed (exit $LASTEXITCODE)" }
Get-ChildItem $Stage -Recurse -Include *.pdb, *.xml | Remove-Item -Force
if (-not (Test-Path (Join-Path $Stage 'TaDaMoo.exe'))) { throw 'TaDaMoo.exe was not published' }

Set-Content -Path (Join-Path $Stage 'gui-only.flag') -Value 'The window alone: no local server, no engine. Delete this file to start it as the full app.' -Encoding ASCII
Copy-Item (Join-Path $PSScriptRoot 'Install-TaDaMooGUI.ps1') $Stage
Copy-Item (Join-Path $PSScriptRoot 'Remove-TaDaMooGUI.ps1') $Stage

$zip = Join-Path $Out ("TaDaMooGUI-{0}-setup.zip" -f $label)
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path $Stage -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
Set-Content -Path (Join-Path $Out 'SHA256SUMS.txt') -Value ("{0} *{1}" -f $hash, (Split-Path $zip -Leaf)) -Encoding ASCII
Write-Host ("GUI-only setup: {0} ({1:N0} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
