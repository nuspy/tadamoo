# tools/windows/vsenv.ps1 - load the MSVC x64 build environment into the current PowerShell session.
#
#   . .\tools\windows\vsenv.ps1                 # dot-source: cl, link, rc, the Windows SDK on PATH/INCLUDE/LIB
#   . .\tools\windows\vsenv.ps1 -Toolset 14.44  # a specific MSVC toolset (the default below)
#
# CUDA 13.1's nvcc accepts MSVC 19.3x/19.4x (the Visual Studio 2022 toolsets, v143) as its host compiler; the
# Visual Studio 2026 toolset (14.5x, cl 19.5x) is newer than nvcc knows and needs -allow-unsupported-compiler.
# Visual Studio 2026 installs the v143 toolset beside its own ("MSVC v143 - VS 2022 C++ x64/x86 build tools"),
# so the engine is built with that one: the C++ and the CUDA host code then come from the same compiler.
param(
    [string]$Toolset = "14.44"
)
$ErrorActionPreference = "Stop"
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found: is Visual Studio installed?" }
$vs = & $vswhere -latest -prerelease -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "no Visual Studio with the C++ x64 tools was found" }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
$arg = if ($Toolset) { "-vcvars_ver=$Toolset" } else { "" }
$lines = cmd.exe /s /c "`"$vcvars`" $arg >nul 2>&1 && set"
if ($LASTEXITCODE -ne 0) { throw "vcvars64.bat $arg failed (is the MSVC $Toolset toolset installed?)" }
foreach ($l in $lines) {
    $i = $l.IndexOf('=')
    if ($i -gt 0) { Set-Item -Path ("env:" + $l.Substring(0, $i)) -Value $l.Substring($i + 1) }
}
# CL / _CL_ add options to EVERY cl.exe call: a user-wide "_CL_=/std:c++20" also reaches the C sources (ggml) and
# fails them ("/std:c11 and /std:c++20 are incompatible").  The build sets its own standards, so they are cleared
# for this session only.
foreach ($v in "CL", "_CL_") {
    if (Test-Path "env:$v") {
        Write-Host ("clearing {0}={1} for this session (the build sets its own options)" -f $v, (Get-Item "env:$v").Value)
        Remove-Item "env:$v"
    }
}
Write-Host ("MSVC {0} from {1}" -f $env:VCToolsVersion, $vs)
