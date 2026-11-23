<#
.SYNOPSIS
    Builds the RTX Video SDK VSR binding (voice/callbridge/enhance/native/rtx_video_vsr.cpp) and stages the files
    `compile_and_package.ps1` bundles with the app: bin\rtx_video_vsr.dll (our own code, built here) and
    bin\nvngx_vsr.dll (NVIDIA's feature runtime, copied from the SDK).
.DESCRIPTION
    This is a maintainer step, run once per machine/release: it needs the RTX Video SDK (headers, import libs, the
    feature DLL) and the CUDA Toolkit installed, neither of which `compile_and_package.ps1` requires or bundles on
    its own - but once bin\rtx_video_vsr.dll exists, that script picks it up and ships it with the app, so end users
    never install the SDK themselves. Why this is allowed: NVIDIA RTX SDKs License (v. February 23, 2024) section
    1(c) licenses distributing SDK software, object code only, as part of an application with material functionality
    beyond the SDK (TaDaMoo plainly has that); section 1(b) licenses modifying and distributing derivative works of
    the SDK's own sample source, which is what voice/callbridge/enhance/native/rtx_video_vsr.cpp is, with the
    required notice at its top. See docs/voice/acceleration.md.
.PARAMETER SdkRoot
    The RTX Video SDK folder (holds include\, lib\, bin\). Default: the NV_RTX_VIDEO_SDK environment variable.
.PARAMETER CudaRoot
    The CUDA toolkit folder to build with. Default: the newest one under
    C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA.
.EXAMPLE
    .\tools\windows\build_rtx_video_native.ps1
#>
param(
    [string]$SdkRoot = $env:NV_RTX_VIDEO_SDK,
    [string]$CudaRoot = ''
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

function Find-Cuda([string]$Root) {
    if ($Root) { return $Root }
    $base = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
    $newest = Get-ChildItem $base -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^v[\d.]+$' } | Sort-Object { [version]($_.Name.TrimStart('v')) } -Descending | Select-Object -First 1
    if (-not $newest) { throw "No CUDA toolkit found under $base - pass -CudaRoot." }
    return $newest.FullName
}

if (-not $SdkRoot) { throw 'Set NV_RTX_VIDEO_SDK to the RTX Video SDK folder, or pass -SdkRoot.' }
if (-not (Test-Path (Join-Path $SdkRoot 'include\nvsdk_ngx.h'))) { throw "$SdkRoot does not look like the RTX Video SDK (no include\nvsdk_ngx.h)." }
$Cuda = Find-Cuda $CudaRoot
Write-Host "RTX Video SDK: $SdkRoot"
Write-Host "CUDA Toolkit:  $Cuda"

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -prerelease -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'No Visual Studio install with the C++ (VC.Tools.x86.x64) workload was found.' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$src = Join-Path $Root 'voice\callbridge\enhance\native\rtx_video_vsr.cpp'
$bin = Join-Path $Root 'bin'
New-Item -ItemType Directory -Force $bin | Out-Null
$obj = Join-Path $env:TEMP 'rtx_video_vsr.obj'
$dll = Join-Path $bin 'rtx_video_vsr.dll'

$cl = @(
    '/nologo', '/EHsc', '/MD', '/O2',
    "/I`"$SdkRoot\include`"", "/I`"$Cuda\include`"",
    "`"$src`"", '/c', "/Fo`"$obj`""
) -join ' '
$link = @(
    '/nologo', '/DLL', "/OUT:`"$dll`"", "`"$obj`"",
    "`"$SdkRoot\lib\Windows\x64\nvsdk_ngx_d.lib`"", "`"$Cuda\lib\x64\cuda.lib`"", "`"$Cuda\lib\x64\cudart.lib`"",
    'advapi32.lib', 'user32.lib',
    '/EXPORT:tadamoo_rtx_vsr_create', '/EXPORT:tadamoo_rtx_vsr_evaluate', '/EXPORT:tadamoo_rtx_vsr_shutdown', '/EXPORT:tadamoo_rtx_vsr_last_error'
) -join ' '

Write-Host 'Compiling rtx_video_vsr.cpp...'
cmd /c "`"$vcvars`" >nul && cl.exe $cl && link.exe $link"
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
if (-not (Test-Path $dll)) { throw "$dll was not produced." }

$feature = Join-Path $SdkRoot 'bin\Windows\x64\rel\nvngx_vsr.dll'
if (-not (Test-Path $feature)) { throw "$feature not found (expected the 'rel', non-watermarked build)." }
Copy-Item $feature $bin -Force

Write-Host "Done: $dll"
Write-Host "Done: $(Join-Path $bin 'nvngx_vsr.dll')"
Write-Host 'Both will be picked up and bundled the next time compile_and_package.ps1 runs.'
