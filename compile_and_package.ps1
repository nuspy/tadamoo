<#
.SYNOPSIS
    One command that does everything: builds TaDaMoo Win+GUI, stages the program folder, builds the setup, updates the
    app on this PC and raises the version. Run it as it is: .\compile_and_package.ps1

.DESCRIPTION
    In order, and stopping at the first failure (the version is raised only when everything worked):

      1. the version: it grows by itself, as <name>_(v_<version>_<subversion>), for example chunky-pasta_(v_1_3)
      2. the engine (CMake with the newest CUDA toolkit installed: when a newer one than the build was made with appears, the build folder
         is thrown away and the engine is built again from scratch, so there is only ever one build. Incremental otherwise: when nothing
         changed it is not touched and the server may keep running; when it has to be rebuilt the server must be stopped, and the script
         says so before it starts)
      3. the program folder, the desktop app and the setup helper, the Python installer and wheels for PCs without Python
      4. the InstallForge project (.ifp), updated and not rewritten: what you edited in InstallForge is kept
      5. the setup .exe (InstallForge's command-line builder) and its SHA256SUMS.txt
      6. the app on this PC (tools\windows\desktop\publish), so the app you start is the one just built
      7. the file VERSION in the repository, with the new numbers
      8. the setups of earlier versions in dist\ are deleted, so only the one just built is left (only after everything worked)

    Everything it makes is under dist\ (ignored by git) except VERSION, which you commit when you like. Nothing is
    uploaded or published anywhere.

      dist\stage\TaDaMoo\   the program as it will be installed
      dist\TaDaMoo-WinGUI.ifp     the InstallForge project
      dist\TaDaMoo-WinGUI-<name>_(v_<version>_<subversion>)-setup.exe   the setup
      dist\cache\                downloads kept between runs (Python installer, wheels, the name generator)

    The version. The subversion goes up by one at every successful run. The name belongs to the version: it is made
    once, with unique-namer (MIT, https://github.com/aziele/unique-namer, used only here and not shipped), never
    repeats one used before and stays the same while the subversion goes up. To start a new version (the version goes
    up by one, the subversion starts again and a new name is made) run it once with -NewVersion. VERSION holds the
    numbers and the names used; the setup, the app (window title, tray, file properties) and the file names show them.

    The installer puts the folder in C:\Program Files\Tadamoo\TaDaMoo and runs
    tools\windows\desktop\publish\TaDaMooSetup.exe once the files are copied: it finds Python (or installs the bundled
    one), creates .venv, installs requirements.txt from the bundled wheels (else the internet), checks the server
    imports and sets the write permissions. The user starts TaDaMoo.exe from the shortcut.

.PARAMETER NewVersion
    Start a new version instead of a new subversion.
.PARAMETER Name
    The name of the release, used instead of a generated one (with -NewVersion; a name already used is refused).
.PARAMETER SkipEngine
    Rare. Do not run the CMake build even when something changed; use the binaries already in bin\. Not needed to keep the
    server running: when the engine is up to date the build is skipped by itself.
.PARAMETER SkipDownloads
    Rare. Do not touch the network: the Python installer, the wheels and the name generator must already be in dist\cache.
.PARAMETER SkipVendored
    Rare. Do not put agent-desktop and agent-browser (the Vercel Labs programs in third_party\) into tools\vendored\: computer control then uses the system's own backend and the
    browser tools their own driver.
.PARAMETER VendoredArchive
    The tadamoo-vendored-tools-x86_64-pc-windows-msvc.tar.gz that the vendored-tools GitHub workflow attached, to take the two programs from instead of building them here (default:
    dist\cache\vendored\ if it has one, else cargo builds them from third_party\).
.PARAMETER RustToolchain
    Rare. A Rust toolchain to build the two programs with instead of the one agent-desktop pins (rust-toolchain.toml).
.PARAMETER NoSetup
    Rare. Stop after the .ifp: the setup is then built by hand in InstallForge.
.PARAMETER CudaRoot
    Rare. The CUDA toolkit folder to build with. By default the newest one under C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA.
.PARAMETER PythonMinor
    The Python minor version bundled for machines that have none (default 3.13); its newest patch release is used.
.PARAMETER InstallForge
    The path of ifbuildx86.exe (default: found on PATH or under Program Files\solicus\InstallForge\bin).
.EXAMPLE
    .\compile_and_package.ps1
.EXAMPLE
    .\compile_and_package.ps1 -NewVersion
#>
[CmdletBinding()]
param(
    [switch]$NewVersion,
    [string]$Name = '',
    [string]$OutDir = 'dist',
    [switch]$SkipEngine,
    [switch]$SkipDownloads,
    [switch]$NoSetup,
    [switch]$SkipVendored,
    [string]$VendoredArchive = '',
    [string]$RustToolchain = '',
    [string]$CudaRoot = '',
    [string]$PythonMinor = '3.13',
    [string]$InstallForge = ''
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

function Find-Cuda([string]$Wanted) {
    if ($Wanted) { if (-not (Test-Path (Join-Path $Wanted 'bin\nvcc.exe'))) { throw "$Wanted has no bin\nvcc.exe" }; return (Resolve-Path $Wanted).Path }
    $base = Join-Path $env:ProgramFiles 'NVIDIA GPU Computing Toolkit\CUDA'
    $found = Get-ChildItem $base -Directory -Filter 'v*' -ErrorAction SilentlyContinue |
        Where-Object { (Test-Path (Join-Path $_.FullName 'bin\nvcc.exe')) -and (Test-Path (Join-Path $_.FullName 'bin\x64\cublas64_13.dll')) } |
        Sort-Object { [version]($_.Name.TrimStart('v')) } -Descending | Select-Object -First 1
    if (-not $found) { throw "no CUDA toolkit with nvcc and cuBLAS 13 under ${base}: install the CUDA toolkit" }
    return $found.FullName
}

$Root = $PSScriptRoot
$Dist = if ([IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $Root $OutDir }
$Cache = Join-Path $Dist 'cache'
$AppFolder = 'TaDaMoo'                       # the folder InstallForge creates under the install path
$Stage = Join-Path (Join-Path $Dist 'stage') $AppFolder
$IfpPath = Join-Path $Dist 'TaDaMoo-WinGUI.ifp'
$Company = 'Tadamoo'
$InstallRoot = '<ProgramFilesX64>\' + $Company + '\'          # C:\Program Files\Tadamoo\ (not <ProgramFiles>: that is x86)
$AppRel = '<InstallPath>\' + $AppFolder                        # C:\Program Files\Tadamoo\TaDaMoo
$ExcludeFromPackage = @('tests', 'serve/tests', '.github', '.agents', 'third_party')   # developer-only, tracked (third_party is built into tools\vendored below)

function Step([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Info([string]$Text) { Write-Host "   $Text" }
function Warn([string]$Text) { Write-Host "   WARNING: $Text" -ForegroundColor Yellow }
function Run([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed (exit $LASTEXITCODE)" }
}

Push-Location $Root
try {
    New-Item -ItemType Directory -Force $Dist, $Cache | Out-Null

    # ------------------------------------------------------------------ version: it grows by itself and has a name
    $ifpXml = $null
    if (Test-Path $IfpPath) { $ifpXml = New-Object System.Xml.XmlDocument; $ifpXml.PreserveWhitespace = $false; $ifpXml.Load($IfpPath) }
    $VersionFile = Join-Path $Root 'VERSION'
    $NamerDir = Join-Path $Cache 'namer'

    function New-ReleaseName([string[]]$Used) {
        $py = Join-Path $Root '.venv\Scripts\python.exe'
        if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction Stop).Source }
        if (-not (Test-Path (Join-Path $NamerDir 'namer'))) {
            if ($SkipDownloads) { throw "the name generator is not in $NamerDir (run without -SkipDownloads once)" }
            Info 'installing unique-namer (MIT) into the cache, for the release names'
            Run $py @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--target', $NamerDir, 'unique-namer')
        }
        for ($i = 0; $i -lt 100; $i++) {
            $env:PYTHONPATH = $NamerDir
            try { $n = ((& $py -c 'import namer; print(namer.generate())') | Select-Object -Last 1).Trim() } finally { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
            if ($LASTEXITCODE -ne 0) { throw 'the name generator failed' }
            if ($n -match '^[a-z]+(-[a-z]+)+$' -and ($Used -notcontains $n)) { return $n }
        }
        throw 'could not make a name that was not used before'
    }

    $curVersion = 1; $curSub = 0; $relName = ''; $usedNames = @()
    if (Test-Path $VersionFile) {
        try { $st = Get-Content -Raw -Encoding UTF8 $VersionFile | ConvertFrom-Json } catch { throw "VERSION is not valid JSON: $VersionFile" }
        $curVersion = [int]$st.version; $curSub = [int]$st.subversion; $relName = [string]$st.name; $usedNames = @($st.names_used)
    } elseif ($ifpXml) {
        # the first run with this scheme continues the version number the project already had (1.0 -> version 1)
        $n = $ifpXml.SelectSingleNode('/InstallForgeProject/ProductInformation/Version')
        if ($n -and $n.InnerText -match '^(\d+)') { $curVersion = [int]$Matches[1] }
    }
    if ($NewVersion -and $relName) { $curVersion++; $curSub = 0; $relName = '' }
    if ($Name -and $usedNames -contains $Name) { throw "the release name '$Name' was used before" }
    if (-not $relName) { $relName = if ($Name) { $Name } else { New-ReleaseName $usedNames }; $usedNames += $relName; $curSub = 0 }
    $newSub = $curSub + 1
    $ReleaseLabel = '{0}_(v_{1}_{2})' -f $relName, $curVersion, $newSub       # the name shown everywhere
    $Version = '{0}.{1}' -f $curVersion, $newSub                              # the number InstallForge takes
    $SetupExe = Join-Path $Dist ("TaDaMoo-WinGUI-{0}-setup.exe" -f $ReleaseLabel)

    # ------------------------------------------------------------------ what is being packaged
    Step "Source tree (release $ReleaseLabel)"
    $commit = (& git rev-parse --short HEAD).Trim()
    $changed = @(& git diff --name-only HEAD -- . ':!VERSION')
    Info "HEAD $commit; tracked files are taken from the working tree"
    if ($changed.Count) { Warn ("{0} tracked file(s) differ from HEAD and go into the package as they are now: {1}" -f $changed.Count, (($changed | Select-Object -First 8) -join ', ')) }

    # ------------------------------------------------------------------ engine
    $Cuda = Find-Cuda $CudaRoot
    $env:CUDA_PATH = $Cuda
    Info "CUDA toolkit: $Cuda"
    if ($SkipEngine) {
        Step 'Engine: skipped (-SkipEngine)'
    } else {
        Step "Engine (CMake, MSVC + CUDA $(Split-Path $Cuda -Leaf))"
        . (Join-Path $Root 'tools\windows\vsenv.ps1')
        $cmakeCache = Join-Path $Root 'build-win\CMakeCache.txt'
        $builtWith = if (Test-Path $cmakeCache) { (Select-String -Path $cmakeCache -Pattern '^CMAKE_CUDA_COMPILER:\w+=(.*)$' | Select-Object -First 1).Matches.Groups[1].Value } else { '' }
        $wanted = (Join-Path $Cuda 'bin\nvcc.exe') -replace '\\', '/'
        if ($builtWith -and ($builtWith -replace '\\', '/') -ne $wanted) {
            $inUse = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith((Join-Path $Root 'bin'), [StringComparison]::OrdinalIgnoreCase) })
            if ($inUse.Count) { throw ('the build was made with {0} and this run uses {1}, so the engine is built again, but it is running ({2}): stop the server (Ferma-TaDaMoo.ps1) and run this again' -f $builtWith, $wanted, (($inUse | ForEach-Object { "$($_.ProcessName) $($_.Id)" }) -join ', ')) }
            Info "the build folder was made with ${builtWith}: removed, the engine is built again with ${wanted}"
            Remove-Item -Recurse -Force (Join-Path $Root 'build-win')
        }
        if (-not (Test-Path (Join-Path $Root 'build-win\build.ninja'))) {
            Info 'configuring build-win'
            Run cmake @('-S', '.', '-B', 'build-win', '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CUDA_ARCHITECTURES=120', '-DCMAKE_C_COMPILER=cl', '-DCMAKE_CXX_COMPILER=cl',
                           "-DCMAKE_CUDA_COMPILER=$wanted", ('-DCUDAToolkit_ROOT=' + ($Cuda -replace '\\', '/')))
        }
        # a dry run lists what would be rebuilt: when nothing, the binaries are left alone; when something, the programs it would link
        # must not be running (a running program keeps its file locked), and the script says which before it starts
        $plan = (& cmake --build build-win -- -n 2>&1 | Out-String)
        if ($LASTEXITCODE -eq 0 -and $plan -match 'no work to do') {
            Info 'the engine is up to date: not rebuilt (the server may keep running)'
        } else {
            $targets = @([regex]::Matches($plan, 'Linking \S+ executable (\S+)') | ForEach-Object { $_.Groups[1].Value })
            $inUse = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Where-Object { $own = $_.Path; $targets | Where-Object { [string]::Equals($_, $own, [StringComparison]::OrdinalIgnoreCase) } })
            if ($inUse.Count) { throw ('the engine has to be rebuilt ({0}) but it is running ({1}): stop the server (Ferma-TaDaMoo.ps1) and run this again' -f (($targets | ForEach-Object { Split-Path $_ -Leaf }) -join ', '), (($inUse | ForEach-Object { "$($_.ProcessName) $($_.Id)" }) -join ', ')) }
            (Get-Process -Id $PID).PriorityClass = 'BelowNormal'
            Run cmake @('--build', 'build-win', '--parallel', '4')
        }
    }
    foreach ($exe in 'basalt.exe', 'basalt-vision.exe', 'basalt-bench.exe') {
        if (-not (Test-Path (Join-Path $Root "bin\$exe"))) { throw "bin\$exe is missing: build the engine (run without -SkipEngine)" }
    }

    # ------------------------------------------------------------------ stage: tracked files
    Step 'Staging the program folder'
    if (Test-Path (Join-Path $Dist 'stage')) { Remove-Item -Recurse -Force (Join-Path $Dist 'stage') }
    New-Item -ItemType Directory -Force $Stage | Out-Null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $files = & git -c core.quotepath=off ls-files
    $copied = 0
    foreach ($f in $files) {
        if (-not $f) { continue }
        $skip = $false
        foreach ($x in $ExcludeFromPackage) { if ($f -eq $x -or $f.StartsWith($x + '/')) { $skip = $true; break } }
        if ($skip) { continue }
        $src = Join-Path $Root $f
        if (-not (Test-Path $src -PathType Leaf)) { continue }          # deleted in the working tree
        $dst = Join-Path $Stage $f
        New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
        Copy-Item -LiteralPath $src -Destination $dst
        $copied++
    }
    Info "$copied tracked files"

    # ------------------------------------------------------------------ stage: binaries and runtime libraries
    $bin = Join-Path $Stage 'bin'
    New-Item -ItemType Directory -Force $bin | Out-Null
    Copy-Item (Join-Path $Root 'bin\*.exe') $bin

    foreach ($dll in 'cublas64_13.dll', 'cublasLt64_13.dll') {
        $p = Join-Path $Cuda "bin\x64\$dll"
        if (-not (Test-Path $p)) { throw "$p not found (the CUDA toolkit the engine was built with)" }
        Copy-Item $p $bin
    }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = (& $vswhere -latest -prerelease -products * -property installationPath)
    $crt = Get-ChildItem (Join-Path $vs 'VC\Redist\MSVC\*\x64\Microsoft.VC*.CRT') -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch 'Debug' } | Sort-Object { [version]($_.FullName -replace '^.*\\MSVC\\([\d.]+)\\.*$', '$1') } -Descending | Select-Object -First 1
    if (-not $crt) { throw 'the Microsoft C++ runtime (VC\Redist\MSVC\...\x64\Microsoft.VC*.CRT) was not found in Visual Studio' }
    foreach ($dll in 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') { Copy-Item (Join-Path $crt.FullName $dll) $bin }
    Info "bin: engine, cuBLAS from $Cuda (CUDA EULA, Attachment A), C++ runtime from $($crt.FullName)"
    $lic = Join-Path $Stage 'licenses'
    New-Item -ItemType Directory -Force $lic | Out-Null
    Copy-Item (Join-Path $cuda 'EULA.txt') (Join-Path $lic 'NVIDIA-CUDA-EULA.txt')

    # ------------------------------------------------------------------ desktop app and setup helper
    Step 'Desktop app and setup helper (.NET 8, self-contained)'
    $pub = Join-Path $Stage 'tools\windows\desktop\publish'
    foreach ($proj in 'tools\windows\desktop\TaDaMooDesktop.csproj', 'tools\windows\setup\TaDaMooSetup.csproj') {
        Run dotnet @('publish', (Join-Path $Root $proj), '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '-o', $pub, '-nologo', '-v', 'q', '-p:DebugType=None', '-p:DebugSymbols=false', '-p:NoWarn=MSB3277',
            "-p:Version=$Version.0", "-p:InformationalVersion=$ReleaseLabel", '-p:IncludeSourceRevisionInInformationalVersion=false')
    }
    Get-ChildItem $pub -Recurse -Include *.pdb, *.xml | Remove-Item -Force
    foreach ($exe in 'TaDaMoo.exe', 'TaDaMooSetup.exe') { if (-not (Test-Path (Join-Path $pub $exe))) { throw "$exe was not published" } }
    Info ("{0} files, {1:N0} MB" -f (Get-ChildItem $pub -Recurse -File).Count, ((Get-ChildItem $pub -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

    # ------------------------------------------------------------------ vendored tools: agent-desktop and agent-browser (third_party/, Apache-2.0)
    Step 'Vendored tools (agent-desktop, agent-browser)'
    $vend = Join-Path $Stage 'tools\vendored'
    $archive = $VendoredArchive
    if (-not $archive) { $archive = (Get-ChildItem (Join-Path $Cache 'vendored') -Filter 'tadamoo-vendored-tools-x86_64-pc-windows-msvc.tar.gz' -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object { $_.FullName }) }
    if ($SkipVendored) {
        Info 'skipped (-SkipVendored)'
    } elseif ($archive) {
        Info "from $archive"
        Run (Join-Path $env:SystemRoot 'System32\tar.exe') @('-xzf', $archive, '-C', $Stage)     # Windows' own tar (a Git tar reads C: as a host); tools\vendored\<name>\..., and the licences at the top
        foreach ($f in Get-ChildItem $Stage -Filter 'licenses-*.txt' -File) { Move-Item -Force $f.FullName (Join-Path $Stage 'licenses') }
        foreach ($f in Get-ChildItem $Stage -Filter 'deny-*.txt' -File) { Move-Item -Force $f.FullName (Join-Path $Stage 'licenses') }
    } elseif (Get-Command cargo -ErrorAction SilentlyContinue) {
        Info 'building them from third_party\ with cargo (the vendored-tools workflow gives the same programs: -VendoredArchive)'
        if ($RustToolchain) { $env:RUSTUP_TOOLCHAIN = $RustToolchain }
        try {
            if (-not (Get-Command cl -ErrorAction SilentlyContinue)) { . (Join-Path $Root 'tools\windows\vsenv.ps1') }
            Push-Location (Join-Path $Root 'third_party\agent-desktop'); try { Run cargo @('build', '--locked', '--release', '-p', 'agent-desktop') } finally { Pop-Location }
            Push-Location (Join-Path $Root 'third_party\agent-browser\cli'); try { Run cargo @('build', '--locked', '--release', '--jobs', '2') } finally { Pop-Location }
        } finally { if ($RustToolchain) { Remove-Item Env:RUSTUP_TOOLCHAIN -ErrorAction SilentlyContinue } }
        New-Item -ItemType Directory -Force (Join-Path $vend 'agent-desktop'), (Join-Path $vend 'agent-browser') | Out-Null
        Copy-Item (Join-Path $Root 'third_party\agent-desktop\target\release\agent-desktop.exe') (Join-Path $vend 'agent-desktop')
        Copy-Item (Join-Path $Root 'third_party\agent-browser\cli\target\release\agent-browser.exe') (Join-Path $vend 'agent-browser')
        foreach ($n in 'agent-desktop', 'agent-browser') { Copy-Item (Join-Path $Root "third_party\$n\LICENSE"), (Join-Path $Root "third_party\$n\UPSTREAM.md") (Join-Path $vend $n) }
    } else {
        Warn 'no cargo and no archive: the setup has no agent-desktop or agent-browser (computer control uses the system''s own backend, the browser its own driver)'
    }
    foreach ($n in 'agent-desktop', 'agent-browser') {
        $dir = Join-Path $vend $n
        if (Test-Path $dir) {
            if (-not (Test-Path (Join-Path $dir "$n.exe"))) { throw "$n.exe is missing from $dir" }
            Copy-Item (Join-Path $dir 'LICENSE') (Join-Path (Join-Path $Stage 'licenses') "$n-LICENSE.txt") -ErrorAction SilentlyContinue
            & (Join-Path $dir "$n.exe") $(if ($n -eq 'agent-desktop') { 'version' } else { '--version' }) | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "$n.exe does not run" }
            Info "$n.exe: $([math]::Round((Get-Item (Join-Path $dir "$n.exe")).Length / 1MB, 1)) MB"
        }
    }

    # ------------------------------------------------------------------ Python assets (for PCs without Python)
    Step "Python $PythonMinor assets (bundled installer and wheels)"
    $setupDir = Join-Path $Stage 'setup'
    $pyDir = Join-Path $setupDir 'python'
    $wheelDir = Join-Path $setupDir 'wheels'
    New-Item -ItemType Directory -Force $pyDir, $wheelDir | Out-Null

    $cachedPy = @(Get-ChildItem $Cache -Filter "python-$PythonMinor.*-amd64.exe" -ErrorAction SilentlyContinue | Sort-Object { [version]($_.Name -replace '^python-([\d.]+)-amd64\.exe$', '$1') } -Descending)
    if (-not $SkipDownloads) {
        $index = (Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/').Content
        $patches = [regex]::Matches($index, 'href="(' + [regex]::Escape($PythonMinor) + '\.\d+)/"') | ForEach-Object { $_.Groups[1].Value } |
            Sort-Object { [version]$_ } -Descending
        $chosen = $null
        foreach ($v in $patches) {
            $url = "https://www.python.org/ftp/python/$v/python-$v-amd64.exe"
            try { $r = Invoke-WebRequest -UseBasicParsing -Method Head $url; if ($r.StatusCode -eq 200) { $chosen = $v; break } } catch { }
        }
        if (-not $chosen) { throw "no Python $PythonMinor.x amd64 installer found on python.org" }
        $target = Join-Path $Cache "python-$chosen-amd64.exe"
        if (-not (Test-Path $target)) { Info "downloading Python $chosen"; Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$chosen/python-$chosen-amd64.exe" -OutFile $target }
        $cachedPy = @(Get-Item $target)
    }
    if (-not $cachedPy.Count) { throw "no Python $PythonMinor installer in $Cache (run without -SkipDownloads once)" }
    $pyInstaller = $cachedPy[0]
    $sig = Get-AuthenticodeSignature $pyInstaller.FullName
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') { throw "the signature of $($pyInstaller.Name) is not the Python Software Foundation's ($($sig.Status))" }
    $pyVersion = $pyInstaller.Name -replace '^python-([\d.]+)-amd64\.exe$', '$1'
    $pyHash = (Get-FileHash $pyInstaller.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    Copy-Item $pyInstaller.FullName $pyDir
    @{ version = $pyVersion; installer = $pyInstaller.Name; sha256 = $pyHash } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $pyDir 'bundled.json')
    Info "Python $pyVersion, signature valid, sha256 $($pyHash.Substring(0, 16))..."

    $wheelCache = Join-Path $Cache "wheels-cp$($PythonMinor.Replace('.', ''))"
    New-Item -ItemType Directory -Force $wheelCache | Out-Null
    if (-not $SkipDownloads) {
        $hostPy = Join-Path $Root '.venv\Scripts\python.exe'
        if (-not (Test-Path $hostPy)) { $hostPy = (Get-Command python -ErrorAction Stop).Source }
        Info "downloading the wheels of requirements.txt for CPython $PythonMinor (win_amd64)"
        Run $hostPy @('-m', 'pip', 'download', '--disable-pip-version-check', '--quiet', '-r', (Join-Path $Root 'requirements.txt'), '--dest', $wheelCache,
            '--only-binary=:all:', '--python-version', $PythonMinor, '--implementation', 'cp', '--abi', ('cp' + $PythonMinor.Replace('.', '')), '--platform', 'win_amd64')
    }
    $wheels = @(Get-ChildItem $wheelCache -Filter *.whl)
    if (-not $wheels.Count) { throw "no wheels in $wheelCache (run without -SkipDownloads once)" }
    Copy-Item (Join-Path $wheelCache '*.whl') $wheelDir
    Info ("{0} wheels, {1:N0} MB" -f $wheels.Count, (($wheels | Measure-Object Length -Sum).Sum / 1MB))

    # ------------------------------------------------------------------ add-ons (dependencies shipped with the app)
    Step 'Add-ons: Node.js, Billion Context, REA, Openweight Canary, Tor, MinIO and PostgreSQL (with pgvector)'
    $addDir = Join-Path $Stage 'addons'
    New-Item -ItemType Directory -Force $addDir | Out-Null
    $addCache = Join-Path $Cache 'addons'
    New-Item -ItemType Directory -Force $addCache | Out-Null
    $licAdd = Join-Path $lic 'addons'
    New-Item -ItemType Directory -Force $licAdd | Out-Null
    $NodeVersion = '24.22.1'
    $NpmAddons = @(
        @{ name = 'billion-context'; version = '0.1.208'; integrity = 'sha512-UxsXmDoXRFZJ5D/hyhq33QGd0dOWqA5uo/ssJLd79NH54yU7wFlb1Bf9h+b9+nzKcrfCxEo9iIibKMUzeedwDA==' },
        @{ name = 'rea-agents'; version = '6.8.0'; integrity = 'sha512-9QDeRnGqEDzABZyr04eLnBXBv/c6I9/TWu/6M0MD4pEfkySI/BIgwTODIbAReX3C1x6hjw9+Jlx4A2yUSPZ9ww==' }
    )
    function Get-Cached([string]$Url, [string]$File) {
        if (-not (Test-Path $File)) {
            if ($SkipDownloads) { throw "$File is not in the cache (run without -SkipDownloads once)" }
            Info "downloading $Url"
            Invoke-WebRequest -UseBasicParsing $Url -OutFile $File
        }
    }
    function Get-Sha512Sri([string]$File) {
        $sha = [Security.Cryptography.SHA512]::Create()
        try { $fs = [IO.File]::OpenRead($File); try { 'sha512-' + [Convert]::ToBase64String($sha.ComputeHash($fs)) } finally { $fs.Dispose() } } finally { $sha.Dispose() }
    }
    $addInfo = [ordered]@{}

    # Node.js, portable (node.exe only), checked against the SHASUMS256.txt of nodejs.org
    $nodeZip = Join-Path $addCache "node-v$NodeVersion-win-x64.zip"
    Get-Cached "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip" $nodeZip
    $sums = Join-Path $addCache "node-v$NodeVersion-SHASUMS256.txt"
    Get-Cached "https://nodejs.org/dist/v$NodeVersion/SHASUMS256.txt" $sums
    $want = ((Get-Content $sums | Where-Object { $_ -match "node-v$([regex]::Escape($NodeVersion))-win-x64\.zip$" }) -split '\s+')[0]
    $have = (Get-FileHash $nodeZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not $want -or $want -ne $have) { throw "Node.js ${NodeVersion}: the sha256 of the zip does not match nodejs.org's SHASUMS256.txt" }
    $nodeTmp = Join-Path $addCache "node-extract-$NodeVersion"
    if (Test-Path $nodeTmp) { Remove-Item -Recurse -Force $nodeTmp }
    Expand-Archive -LiteralPath $nodeZip -DestinationPath $nodeTmp
    $nodeSrc = Join-Path $nodeTmp "node-v$NodeVersion-win-x64"
    $nodeDst = Join-Path $addDir 'node'
    New-Item -ItemType Directory -Force $nodeDst | Out-Null
    Copy-Item (Join-Path $nodeSrc 'node.exe') $nodeDst
    Copy-Item (Join-Path $nodeSrc 'LICENSE') (Join-Path $licAdd 'Node.js-LICENSE.txt')
    $addInfo['node'] = [ordered]@{ version = $NodeVersion; license = 'MIT and others (see licenses\addons\Node.js-LICENSE.txt)'; sha256 = $have; source = 'https://nodejs.org' }
    Info "Node.js $NodeVersion, sha256 matches nodejs.org"

    # npm packages: the tarball is checked against the pinned integrity, then installed with its dependencies and no install scripts
    $npm = (Get-Command npm -ErrorAction Stop).Source
    foreach ($a in $NpmAddons) {
        $tgz = Join-Path $addCache ("{0}-{1}.tgz" -f $a.name, $a.version)
        Get-Cached ("https://registry.npmjs.org/{0}/-/{0}-{1}.tgz" -f $a.name, $a.version) $tgz
        if ((Get-Sha512Sri $tgz) -ne $a.integrity) { throw "$($a.name)@$($a.version): the tarball does not match the pinned integrity" }
        $dst = Join-Path $addDir $a.name
        $work = Join-Path $addCache ("build-{0}-{1}" -f $a.name, $a.version)
        if (Test-Path $work) { Remove-Item -Recurse -Force $work }
        New-Item -ItemType Directory -Force $work | Out-Null
        '{"private":true}' | Set-Content -Encoding ASCII (Join-Path $work 'package.json')
        Run $npm @('install', '--prefix', $work, '--ignore-scripts', '--no-audit', '--no-fund', '--loglevel', 'error', $tgz)
        New-Item -ItemType Directory -Force $dst | Out-Null
        Copy-Item (Join-Path $work 'node_modules') (Join-Path $dst 'node_modules') -Recurse
        if (-not (Test-Path (Join-Path $dst "node_modules\$($a.name)\package.json"))) { throw "$($a.name) was not installed" }
        Copy-Item (Join-Path $work 'package-lock.json') $dst -ErrorAction SilentlyContinue
        $pkgJson = Get-Content -Raw (Join-Path $dst "node_modules\$($a.name)\package.json") | ConvertFrom-Json
        $licFile = Get-ChildItem (Join-Path $dst "node_modules\$($a.name)") -Filter 'LICENSE*' | Select-Object -First 1
        if ($licFile) { Copy-Item $licFile.FullName (Join-Path $licAdd ("{0}-{1}.txt" -f $a.name, $licFile.BaseName)) }
        $addInfo[$a.name] = [ordered]@{ version = $a.version; license = "$($pkgJson.license)"; integrity = $a.integrity; engines = $pkgJson.engines; entry = "node_modules\$($a.name)" }
        Info ("{0}@{1}: {2:N0} MB, license {3}" -f $a.name, $a.version, ((Get-ChildItem $dst -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), $pkgJson.license)
    }

    # Tor (Expert Bundle, for the dark web option): hash and signature checked by darkweb.torbundle, the same module Settings > Dark web uses to install
    # it on a PC that builds nothing. Bundling it here means a person who gets the finished package never needs GnuPG or a download of their own for it.
    # darkweb.torbundle calls plain "gpg" (resolved on PATH): a native PowerShell session has no gpg of its own, only Git for Windows ships one, so it is
    # added to PATH for this step (torbundle swallows "gpg not found" and a bad signature the same way, so a missing gpg otherwise fails with a confusing
    # signature error instead of a clear one).
    $addPy = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path $addPy)) { $addPy = (Get-Command python -ErrorAction Stop).Source }
    if (-not (Get-Command gpg -ErrorAction SilentlyContinue)) {
        $gitExe = (Get-Command git -ErrorAction SilentlyContinue).Source
        $gpgBin = if ($gitExe) { Join-Path (Split-Path (Split-Path $gitExe)) 'usr\bin' } else { $null }
        if (-not $gpgBin -or -not (Test-Path (Join-Path $gpgBin 'gpg.exe'))) { throw 'GnuPG (gpg.exe) was not found on PATH or under Git for Windows (usr\bin): required to check the Tor Expert Bundle signature' }
        $env:PATH = "$gpgBin;$env:PATH"
        Info "gpg.exe found under Git for Windows: $gpgBin"
    }
    $torCache = Join-Path $addCache 'tor'
    New-Item -ItemType Directory -Force $torCache | Out-Null
    $torPyFile = Join-Path $torCache 'fetch_tor.py'
    @"
import sys, urllib.request
from pathlib import Path
sys.path.insert(0, r'$Root')
from darkweb import torbundle

cache = Path(r'$torCache')

def fetch(url, dest):
    if dest.is_file() and dest.stat().st_size > 0:
        return
    if $(if ($SkipDownloads) { 'True' } else { 'False' }):
        raise RuntimeError(f'{dest} is not in the cache (run without -SkipDownloads once)')
    urllib.request.urlretrieve(url, dest)

exe = torbundle.install(cache, fetch, torbundle.gpg_with_key(cache / 'gnupg'))
print(str(exe))
print(torbundle.BUNDLE['version'])
"@ | Set-Content -Encoding UTF8 $torPyFile
    $torOut = & $addPy $torPyFile
    if ($LASTEXITCODE -ne 0) { throw "Tor Expert Bundle: install() failed (hash, signature or GnuPG); see output above:`n$torOut" }
    $torExe, $torVersion = $torOut[-2].Trim(), $torOut[-1].Trim()
    $torDst = Join-Path $addDir 'tor'
    if (Test-Path $torDst) { Remove-Item -Recurse -Force $torDst }
    New-Item -ItemType Directory -Force $torDst | Out-Null
    Copy-Item (Join-Path (Split-Path $torExe) '*') $torDst -Recurse -Force
    $addInfo['tor'] = [ordered]@{ version = $torVersion; license = 'BSD-3-Clause and others (Tor Project); see THIRD_PARTY_SEPARATE_PACKAGES.md'; source = 'https://dist.torproject.org' }
    Info "Tor Expert Bundle $torVersion, hash and signature checked"

    # MinIO (community fork "Silo"), for the image store of the knowledge archives: same pinned release and hash as tadamoo/minio_setup.py, read from
    # there so the two never drift apart. Bundling it means Settings > Knowledge > Images finds it at once, with nothing to download on that PC.
    $minioInfo = (& $addPy -c "import sys; sys.path.insert(0, r'$Root'); from tadamoo.minio_setup import RELEASE as R; print(R['url']); print(R['sha256']); print(R['tag'])")
    $minioUrl, $minioSha, $minioTag = $minioInfo[0].Trim(), $minioInfo[1].Trim(), $minioInfo[2].Trim()
    $minioZip = Join-Path $addCache 'minio-release.zip'
    Get-Cached $minioUrl $minioZip
    $haveMinio = (Get-FileHash $minioZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($haveMinio -ne $minioSha) { throw "MinIO ${minioTag}: the sha256 does not match the one pinned in tadamoo/minio_setup.py" }
    $minioTmp = Join-Path $addCache 'minio-extract'
    if (Test-Path $minioTmp) { Remove-Item -Recurse -Force $minioTmp }
    Expand-Archive -LiteralPath $minioZip -DestinationPath $minioTmp
    $minioExeSrc = Get-ChildItem $minioTmp -Recurse -File | Where-Object { $_.Name -in @('minio.exe', 'silo.exe', 'minio', 'silo') } | Select-Object -First 1
    if (-not $minioExeSrc) { throw 'MinIO: the archive holds no program' }
    $minioDst = Join-Path $addDir 'minio'
    New-Item -ItemType Directory -Force $minioDst | Out-Null
    Copy-Item $minioExeSrc.FullName (Join-Path $minioDst $minioExeSrc.Name)
    $addInfo['minio'] = [ordered]@{ tag = $minioTag; license = 'AGPL-3.0 (Silo, community fork of MinIO); see THIRD_PARTY_SEPARATE_PACKAGES.md'; sha256 = $haveMinio; source = $minioUrl }
    Info "MinIO (Silo) $minioTag, sha256 matches tadamoo/minio_setup.py"

    # PostgreSQL (for a knowledge archive's database, Conoscenza > Database): same pinned release and hash as tadamoo/postgres_setup.py, read from there so
    # the two never drift apart. Only the server, its libraries and the headers are kept (never pgAdmin or StackBuilder); install_release() does the keeping.
    $pgInfo = (& $addPy -c "import sys; sys.path.insert(0, r'$Root'); from tadamoo.postgres_setup import RELEASE as R; print(R['url']); print(R['sha256']); print(R['version'])")
    $pgUrl, $pgSha, $pgVersion = $pgInfo[0].Trim(), $pgInfo[1].Trim(), $pgInfo[2].Trim()
    $pgCache = Join-Path $addCache 'postgres'
    New-Item -ItemType Directory -Force $pgCache | Out-Null
    $pgZip = Join-Path $pgCache 'postgresql-release.zip'
    Get-Cached $pgUrl $pgZip
    $pgPyFile = Join-Path $pgCache 'install_postgres.py'
@"
import shutil, sys
from pathlib import Path
sys.path.insert(0, r'$Root')
from tadamoo import postgres_setup

exe = postgres_setup.install_release(Path(r'$pgCache') / 'pgsql', fetch=lambda url, dest: shutil.copyfile(r'$pgZip', dest))
print(str(exe))
"@ | Set-Content -Encoding UTF8 $pgPyFile
    $pgOut = & $addPy $pgPyFile
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL ${pgVersion}: install_release() failed (its own sha256 check); see output above:`n$pgOut" }
    $pgExe = (@($pgOut))[-1].Trim()   # install_postgres.py prints one line only: PowerShell then hands back a scalar string, not a one-element array, so [-1] must be forced to array-index
    $pgRoot = Split-Path (Split-Path $pgExe)        # .../pgsql, the parent of bin, lib, share and include
    $pgDst = Join-Path $addDir 'postgres'
    if (Test-Path $pgDst) { Remove-Item -Recurse -Force $pgDst }
    Copy-Item $pgRoot $pgDst -Recurse
    Copy-Item (Join-Path $pgDst 'server_license.txt') (Join-Path $licAdd 'PostgreSQL-LICENSE.txt') -ErrorAction SilentlyContinue
    $addInfo['postgres'] = [ordered]@{ version = $pgVersion; license = 'PostgreSQL License (see licenses\addons\PostgreSQL-LICENSE.txt)'; sha256 = $pgSha; source = $pgUrl }
    Info "PostgreSQL $pgVersion, sha256 matches tadamoo/postgres_setup.py"

    # pgvector: no official Windows binary exists, and an unofficial one is not trusted for a program with full database access (decided with the owner), so
    # it is compiled from its own source at build time, tag v0.8.7 (PostgreSQL License), with the same Visual Studio C++ tools that build the engine.
    $PgvectorTag = 'v0.8.7'
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (-not (Test-Path $vswhere)) { throw 'pgvector: vswhere.exe was not found (Visual Studio with the "Desktop development with C++" workload is required to build it)' }
    $vsPath = (& $vswhere -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath).Trim()
    if (-not $vsPath) { throw 'pgvector: no Visual Studio installation with the C++ tools was found' }
    $vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
    if (-not (Test-Path $vcvars)) { throw "pgvector: $vcvars is missing" }
    $pgvSrc = Join-Path $addCache 'pgvector'
    if (Test-Path $pgvSrc) { Remove-Item -Recurse -Force $pgvSrc }
    if ($SkipDownloads) { throw 'pgvector: its source is cloned fresh every build (git clone), which -SkipDownloads has no cache for; build once with network access' }
    Run git @('clone', '--branch', $PgvectorTag, '--depth', '1', 'https://github.com/pgvector/pgvector.git', $pgvSrc)
    Copy-Item (Join-Path $pgvSrc 'LICENSE') (Join-Path $licAdd 'pgvector-LICENSE.txt') -ErrorAction SilentlyContinue
    $pgvLog = Join-Path $addCache 'pgvector-build.log'
    $pgvCmd = "call `"$vcvars`" >nul && set `"PGROOT=$pgDst`" && cd /d `"$pgvSrc`" && nmake /F Makefile.win 2>&1 && nmake /F Makefile.win install 2>&1"
    cmd.exe /c $pgvCmd | Set-Content -Encoding UTF8 $pgvLog
    if ($LASTEXITCODE -ne 0) { throw "pgvector ${PgvectorTag}: the build failed (nmake); see $pgvLog" }
    $vectorDll = Get-ChildItem (Join-Path $pgDst 'lib') -Filter 'vector.dll' -ErrorAction SilentlyContinue
    if (-not $vectorDll) { throw "pgvector ${PgvectorTag}: the build did not install vector.dll into $pgDst\lib; see $pgvLog" }
    $addInfo['pgvector'] = [ordered]@{ tag = $PgvectorTag; license = 'PostgreSQL License (see licenses\addons\pgvector-LICENSE.txt)'; source = 'https://github.com/pgvector/pgvector'; compiled_for = $pgVersion }
    Info "pgvector $PgvectorTag compiled against PostgreSQL $pgVersion"

    # Openweight Canary (the owner's own repository): the committed tree, without tests
    $CanaryRepo = Join-Path (Split-Path $Root -Parent) 'openweight-canary'
    $canaryDst = Join-Path $addDir 'openweight-canary'
    if (Test-Path (Join-Path $CanaryRepo '.git')) {
        $cc = (& git -C $CanaryRepo rev-parse HEAD).Trim()
        $cz = Join-Path $addCache "openweight-canary-$cc.zip"
        if (-not (Test-Path $cz)) { Run git @('-C', $CanaryRepo, 'archive', '--format=zip', '-o', $cz, 'HEAD') }
    } else {
        $cc = 'main'
        $cz = Join-Path $addCache 'openweight-canary-main.zip'
        if (Test-Path $cz) { Remove-Item -Force $cz }
        Get-Cached 'https://github.com/nuspy/Openweight-Canary/archive/refs/heads/main.zip' $cz
    }
    $canTmp = Join-Path $addCache 'canary-extract'
    if (Test-Path $canTmp) { Remove-Item -Recurse -Force $canTmp }
    Expand-Archive -LiteralPath $cz -DestinationPath $canTmp
    $canRoot = if (Test-Path (Join-Path $canTmp 'canary')) { $canTmp } else { (Get-ChildItem $canTmp -Directory | Select-Object -First 1).FullName }
    New-Item -ItemType Directory -Force $canaryDst | Out-Null
    Copy-Item (Join-Path $canRoot '*') $canaryDst -Recurse -Force
    if (Test-Path (Join-Path $canaryDst 'tests')) { Remove-Item -Recurse -Force (Join-Path $canaryDst 'tests') }
    if (-not (Test-Path (Join-Path $canaryDst 'canary\__init__.py'))) { throw 'Openweight Canary: canary\__init__.py is missing from the archive' }
    $addInfo['openweight-canary'] = [ordered]@{ commit = $cc; license = 'to be chosen by the owner'; source = 'https://github.com/nuspy/Openweight-Canary'; run = 'python -m canary (PYTHONPATH = addons\openweight-canary)' }
    Info "Openweight Canary $cc"

    [ordered]@{ node = 'addons\node\node.exe'; addons = $addInfo } | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $addDir 'addons.json')

    # ------------------------------------------------------------------ notes in the package
    @"
TaDaMoo Win+GUI $ReleaseLabel
built $(Get-Date -Format 'yyyy-MM-dd HH:mm') from $commit$(if ($changed.Count) { " (+$($changed.Count) uncommitted tracked file(s))" })

Bundled third-party files that are not part of the repository:
  bin\cublas64_13.dll, bin\cublasLt64_13.dll   NVIDIA cuBLAS, redistributed under the CUDA EULA, Attachment A (licenses\NVIDIA-CUDA-EULA.txt)
  bin\msvcp140.dll, vcruntime140.dll, vcruntime140_1.dll   Microsoft Visual C++ runtime
  setup\python\python-$pyVersion-amd64.exe     Python $pyVersion (PSF license), installed only on a PC that has no suitable Python
  setup\wheels\                                the Python packages of requirements.txt (their own licenses)
  addons\node\node.exe                        Node.js $NodeVersion (MIT), runs Billion Context and REA
  addons\billion-context, addons\rea-agents   npm packages with their dependencies (MIT; licenses\addons), installed without install scripts
  addons\openweight-canary                      the exfiltration test (python -m canary), from github.com/nuspy/Openweight-Canary
  addons\tor                                    the Tor Expert Bundle (BSD-3-Clause and others), for the dark web option; hash and signature checked at build time
  addons\minio                                  MinIO (Silo fork, AGPL-3.0; see THIRD_PARTY_SEPARATE_PACKAGES.md), for the image store of the knowledge archives
  addons\postgres                               PostgreSQL (PostgreSQL License), with pgvector compiled from its own source at build time, for the Database section of the knowledge archives
  addons\addons.json                            versions, hashes and licenses of the add-ons
The setup\ folder is removed by the installer once everything is in place. The Qwen model has its own license.
"@ | Set-Content -Encoding UTF8 (Join-Path $lic 'BUNDLED-FILES.txt')

    [ordered]@{ version = $curVersion; subversion = $newSub; name = $relName; release = $ReleaseLabel; commit = $commit } |
        ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Stage 'VERSION')

    # check the staged folder with the setup helper itself (no change made: --dry-run)
    Step 'Checking the staged folder (TaDaMooSetup --dry-run)'
    & (Join-Path $pub 'TaDaMooSetup.exe') --root $Stage --dry-run --lang en | ForEach-Object { Info $_ }
    if ($LASTEXITCODE -ne 0) { throw "TaDaMooSetup --dry-run failed ($LASTEXITCODE)" }

    # ------------------------------------------------------------------ the InstallForge project
    Step 'InstallForge project (.ifp)'
    $created = $false
    if (-not $ifpXml) {
        $created = $true
        $ifpXml = New-Object System.Xml.XmlDocument
        $ifpXml.PreserveWhitespace = $false
        $ifpXml.LoadXml(@'
<?xml version="1.0" encoding="UTF-8"?>
<InstallForgeProject>
  <Metadata productVersion="1.6.1" projectFileVersion="2.0"/>
  <ProductInformation>
    <Name>TaDaMoo Windows+GUI</Name>
    <Version>1.0</Version>
    <CompanyName>Tadamoo</CompanyName>
    <WebURI>https://github.com/nuspy/tadamoo</WebURI>
  </ProductInformation>
  <InstallerConfiguration>
    <Prerequisites>
      <OperatingSystem check="true">
        <WindowsXP>false</WindowsXP>
        <WindowsServer2003>false</WindowsServer2003>
        <WindowsVista>false</WindowsVista>
        <WindowsServer2008>false</WindowsServer2008>
        <Windows7>false</Windows7>
        <Windows8>false</Windows8>
        <Windows10>true</Windows10>
        <WindowsServer2016>true</WindowsServer2016>
        <WindowsServer2019>true</WindowsServer2019>
        <WindowsServer2022>true</WindowsServer2022>
        <Windows11>true</Windows11>
        <WindowsServer2025>true</WindowsServer2025>
      </OperatingSystem>
    </Prerequisites>
    <UserInterface>
      <WizardImage filePath="C:\Program Files\solicus\InstallForge\images\Wizard_2.bmp"/>
      <HeaderImage filePath="C:\Program Files\solicus\InstallForge\images\Header_3.png"/>
      <Label isEnabled="true"/>
      <VisualStyle isEnabled="true"/>
    </UserInterface>
    <InstallationFiles defaultInstallationPath="" isDefaultInstallationPathAlterable ="true"/>
    <Languages>
      <Language>English</Language>
      <Language>Italiano</Language>
    </Languages>
    <Variables/>
    <Commands/>
    <Registry/>
    <Shortcuts defaultPath="&lt;Company&gt;\&lt;AppName&gt;\" isDefaultPathAlterable="true" isCreateStartmenuShortcutsForAllUsers="true" isCreateDesktopShortcutsForAllUsers="true"/>
    <Serials isDialogEnabled="false" count="1000" mask ="#####-#####-#####-#####"/>
    <SplashScreen isDialogEnabled="false" delayTime="2">
      <Image filePath=""/>
      <Sound isEnabled="false" filePath=""/>
    </SplashScreen>
    <License isDialogEnabled="true">
      <Data/>
    </License>
    <Finish isRebootMachineEnabled="false">
      <Program executableFilePath="" isLaunchEnabled="false" arguments=""/>
    </Finish>
  </InstallerConfiguration>
  <UninstallerConfiguration isPackaged="true">
    <UninstallerExecutableFileName>Uninstall</UninstallerExecutableFileName>
    <OpenWebURIAfterUninstallation isEnabled="false">https://</OpenWebURIAfterUninstallation>
    <CustomDisplayIcon isEnabled="false" iconFilePath="&lt;InstallPath&gt;\"/>
  </UninstallerConfiguration>
  <VisualUpdateConfiguration isPackaged="false">
    <ProductName>TaDaMoo Win+GUI</ProductName>
    <ProductVersion>&lt;AppVersion&gt;</ProductVersion>
    <Language>0</Language>
    <UpdateScriptURIs>
      <MainURI>https://</MainURI>
      <FirstMirrorURI>https://</FirstMirrorURI>
      <SecondMirrorURI>https://</SecondMirrorURI>
    </UpdateScriptURIs>
    <updateExecutableFileName>Update</updateExecutableFileName>
    <proEditionLicenseKey/>
    <Program executableFilePath="" isLaunchEnabled="true" isTerminationEnabled="true"/>
  </VisualUpdateConfiguration>
  <BuildConfiguration>
    <SetupFilePath/>
    <SetupIconPath/>
    <UninstallerIconPath/>
    <Compression method="0" level="2"/>
    <CodeSigning isEnabled="false" signToolCommandLine="/n &quot;Subject&quot; /tr http://timestamp.digicert.com /td sha256 /fd sha256 &quot;&lt;ExecutableFilePath&gt;&quot;"/>
  </BuildConfiguration>
</InstallForgeProject>
'@)
    }
    function Node([string]$Path) {
        $n = $ifpXml.SelectSingleNode('/InstallForgeProject/' + $Path)
        if (-not $n) { throw "the .ifp has no <$Path>: delete $IfpPath to have it created again" }
        return $n
    }

    function New-El([string]$Name, [hashtable]$Attr, [string]$Text) {
        $e = $ifpXml.CreateElement($Name)
        foreach ($k in $Attr.Keys) { $e.SetAttribute($k, [string]$Attr[$k]) }
        if ($Text) { $e.InnerText = $Text }
        return $e
    }
    function Clear-El($El) { while ($El.HasChildNodes) { [void]$El.RemoveChild($El.FirstChild) } }

    (Node 'ProductInformation/Version').InnerText = $Version

    # the program folder: ONE directory, kept as a subfolder of the install path
    $inst = Node 'InstallerConfiguration/InstallationFiles'
    $inst.SetAttribute('defaultInstallationPath', $InstallRoot)
    Clear-El $inst
    [void]$inst.AppendChild((New-El 'InstallationFile' @{ size = 'N/A'; type = '[Folder]' } ($Stage.TrimEnd('\') + '\')))

    # after the files are copied: the setup helper (it shows its own progress window)
    $cmds = Node 'InstallerConfiguration/Commands'
    Clear-El $cmds
    [void]$cmds.AppendChild((New-El 'Command' @{ type = 'Execute Application'; command = "$AppRel\tools\windows\desktop\publish\TaDaMooSetup.exe"; commandLineArguments = ''; options = '-wait' } ''))

    # the shortcuts: the app (desktop + start menu) and the uninstaller (start menu)
    $sc = Node 'InstallerConfiguration/Shortcuts'
    Clear-El $sc
    $appExe = "$AppRel\tools\windows\desktop\publish\TaDaMoo.exe"
    $icon = Join-Path $Root 'tools\windows\desktop\tadamoo.ico'             # a path on this PC, as InstallForge stores it
    foreach ($def in @(
            @{ destination = 'Desktop'; name = 'TaDaMoo'; targetFile = $appExe },
            @{ destination = 'Startmenu'; name = 'TaDaMoo'; targetFile = $appExe },
            @{ destination = 'Startmenu'; name = 'Uninstall TaDaMoo'; targetFile = '<InstallPath>\Uninstall.exe' })) {
        $def.commandLineArguments = ''; $def.iconFilePath = $icon; $def.iconIndex = '0'
        [void]$sc.AppendChild((New-El 'Shortcut' $def ''))
    }

    # not launched at the end: the installer runs elevated and would start the app elevated too
    $fin = Node 'InstallerConfiguration/Finish/Program'
    $fin.SetAttribute('executableFilePath', $appExe)
    $fin.SetAttribute('isLaunchEnabled', 'false')

    # the dummy variable and registry entry of the example are not used
    Clear-El (Node 'InstallerConfiguration/Variables')
    Clear-El (Node 'InstallerConfiguration/Registry')

    # the license page: the repository's LICENSE (plus a pointer to the third-party notices), unless it was edited in InstallForge
    $licNode = Node 'InstallerConfiguration/License/Data'
    $current = ''
    if ($licNode.InnerText) { try { $current = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($licNode.InnerText)) } catch { } }
    if ((-not $current) -or ($current -match 'Spazio per la licenza')) {
        $text = (Get-Content -Raw -Encoding UTF8 (Join-Path $Root 'LICENSE')).TrimEnd() + "`n`n" +
            "Third-party components are covered by their own licenses: see THIRD_PARTY_LICENSES and the licenses folder in the installation folder. " +
            "The Qwen model has its own license."
        $rtf = New-Object System.Text.StringBuilder
        [void]$rtf.Append('{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fnil Arial;}}\viewkind4\uc1\pard\f0\fs20 ')
        foreach ($ch in $text.ToCharArray()) {
            switch -Regex ($ch) {
                '\\' { [void]$rtf.Append('\\'); break }
                '\{' { [void]$rtf.Append('\{'); break }
                '\}' { [void]$rtf.Append('\}'); break }
                "`r" { break }
                "`n" { [void]$rtf.Append('\par '); break }
                default { if ([int]$ch -gt 127) { [void]$rtf.Append(('\u{0}?' -f [int]$ch)) } else { [void]$rtf.Append($ch) } }
            }
        }
        [void]$rtf.Append("\par`r`n}`r`n")
        $licNode.InnerText = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($rtf.ToString()) + [byte[]](0))
        Info 'license page: LICENSE of the repository'
    }

    # build paths
    (Node 'BuildConfiguration/SetupFilePath').InnerText = $SetupExe
    (Node 'BuildConfiguration/SetupIconPath').InnerText = $icon
    (Node 'BuildConfiguration/UninstallerIconPath').InnerText = $icon

    $ws = New-Object System.Xml.XmlWriterSettings
    $ws.Indent = $true; $ws.IndentChars = '  '; $ws.Encoding = New-Object System.Text.UTF8Encoding($false)
    $w = [System.Xml.XmlWriter]::Create($IfpPath, $ws)
    try { $ifpXml.Save($w) } finally { $w.Close() }
    Info ("{0}: {1}" -f $(if ($created) { 'created' } else { 'updated' }), $IfpPath)

    # ------------------------------------------------------------------ the setup
    if ($NoSetup) {
        Step 'Setup build: left to you (-NoSetup); load the .ifp in InstallForge and build'
    } else {
        Step 'Setup (InstallForge command-line builder)'
        $ifb = $InstallForge
        if (-not $ifb) {
            $cmd = Get-Command ifbuildx86.exe -ErrorAction SilentlyContinue
            if ($cmd) { $ifb = $cmd.Source }
            foreach ($d in $env:ProgramFiles, ${env:ProgramFiles(x86)}) {
                if (-not $ifb -and $d -and (Test-Path (Join-Path $d 'solicus\InstallForge\bin\ifbuildx86.exe'))) { $ifb = Join-Path $d 'solicus\InstallForge\bin\ifbuildx86.exe' }
            }
        }
        if (-not $ifb -or -not (Test-Path $ifb)) { throw 'ifbuildx86.exe not found: install InstallForge, or pass -InstallForge <path>, or build from the InstallForge window instead (use -NoSetup)' }
        if (Test-Path $SetupExe) { Remove-Item -Force $SetupExe }
        & $ifb -i $IfpPath -o $SetupExe --no-color
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $SetupExe)) { throw "InstallForge did not build the setup (exit $LASTEXITCODE)" }
        $h = (Get-FileHash $SetupExe -Algorithm SHA256).Hash.ToLowerInvariant()
        "$h  $(Split-Path $SetupExe -Leaf)" | Set-Content -Encoding ASCII (Join-Path $Dist 'SHA256SUMS.txt')
        Info ("{0}  {1:N0} MB  sha256 {2}" -f (Split-Path $SetupExe -Leaf), ((Get-Item $SetupExe).Length / 1MB), $h)
    }

    # ------------------------------------------------------------------ the app on this PC
    Step 'The app on this PC (tools\windows\desktop\publish)'
    $local = Join-Path $Root 'tools\windows\desktop\publish'
    $appUpdated = $false
    if (Get-Process TaDaMoo -ErrorAction SilentlyContinue) {
        Warn 'TaDaMoo is open, so the app on this PC was not updated: close it and the next run will update it (the setup is built anyway)'
    } else {
        New-Item -ItemType Directory -Force $local | Out-Null
        Copy-Item (Join-Path $pub '*') $local -Recurse -Force
        $appUpdated = $true
        Info "updated: $local"
    }

    # ------------------------------------------------------------------ the new numbers are kept only now that everything worked
    [ordered]@{ version = $curVersion; subversion = $newSub; name = $relName; names_used = @($usedNames); release = $ReleaseLabel;
                built = (Get-Date -Format 's'); commit = $commit } | ConvertTo-Json | Set-Content -Encoding UTF8 $VersionFile

    if (-not $NoSetup) {                                           # only the setup just built stays: earlier versions are deleted
        $older = @(Get-ChildItem -LiteralPath $Dist -Filter 'TaDaMoo-WinGUI-*-setup.exe' -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -ne (Resolve-Path -LiteralPath $SetupExe).Path })
        foreach ($f in $older) { Remove-Item -LiteralPath $f.FullName -Force; Info ('deleted the setup of an earlier version: {0}' -f $f.Name) }
        foreach ($f in @(Get-ChildItem -LiteralPath $Cache -Filter 'python-*-amd64.exe' -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne $pyInstaller.Name })) {
            Remove-Item -LiteralPath $f.FullName -Force; Info ('deleted an older Python installer from the cache: {0}' -f $f.Name)
        }
    }

    Step "Done: $ReleaseLabel"
    Info "program folder : $Stage"
    Info "InstallForge   : $IfpPath"
    if (Test-Path $SetupExe) { Info "setup          : $SetupExe" }
    Info ("app on this PC : {0}" -f $(if ($appUpdated) { 'updated' } else { 'NOT updated (it was open)' }))
    Info "VERSION        : $VersionFile (commit it when you like; nothing was uploaded)"
}
finally {
    Pop-Location
}
