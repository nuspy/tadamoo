<#
.SYNOPSIS
    One command that does everything: builds Basalt Win+GUI, stages the program folder, builds the setup, updates the
    app on this PC and raises the version. Run it as it is: .\compile_and_package.ps1

.DESCRIPTION
    In order, and stopping at the first failure (the version is raised only when everything worked):

      1. the version: it grows by itself, as <name>_(v_<version>_<subversion>), for example chunky-pasta_(v_1_3)
      2. the engine (CMake, incremental: nothing is rebuilt when nothing changed)
      3. the program folder, the desktop app and the setup helper, the Python installer and wheels for PCs without Python
      4. the InstallForge project (.ifp), updated and not rewritten: what you edited in InstallForge is kept
      5. the setup .exe (InstallForge's command-line builder) and its SHA256SUMS.txt
      6. the app on this PC (tools\windows\desktop\publish), so the app you start is the one just built
      7. the file VERSION in the repository, with the new numbers

    Everything it makes is under dist\ (ignored by git) except VERSION, which you commit when you like. Nothing is
    uploaded or published anywhere.

      dist\stage\BasaltWinGUI\   the program as it will be installed
      dist\Basalt-WinGUI.ifp     the InstallForge project
      dist\Basalt-WinGUI-<name>_(v_<version>_<subversion>)-setup.exe   the setup
      dist\cache\                downloads kept between runs (Python installer, wheels, the name generator)

    The version. The subversion goes up by one at every successful run. The name belongs to the version: it is made
    once, with unique-namer (MIT, https://github.com/aziele/unique-namer, used only here and not shipped), never
    repeats one used before and stays the same while the subversion goes up. To start a new version (the version goes
    up by one, the subversion starts again and a new name is made) run it once with -NewVersion. VERSION holds the
    numbers and the names used; the setup, the app (window title, tray, file properties) and the file names show them.

    The installer puts the folder in C:\Program Files\Tadamoo\BasaltWinGUI and runs
    tools\windows\desktop\publish\BasaltSetup.exe once the files are copied: it finds Python (or installs the bundled
    one), creates .venv, installs requirements.txt from the bundled wheels (else the internet), checks the server
    imports and sets the write permissions. The user starts BasaltWinGUI.exe from the shortcut.

.PARAMETER NewVersion
    Start a new version instead of a new subversion.
.PARAMETER SkipEngine
    Rare. Do not run the CMake build; use the binaries already in bin\ (needed while the engine is running, because a
    running program keeps its file locked).
.PARAMETER SkipDownloads
    Rare. Do not touch the network: the Python installer, the wheels and the name generator must already be in dist\cache.
.PARAMETER NoSetup
    Rare. Stop after the .ifp: the setup is then built by hand in InstallForge.
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
    [string]$OutDir = 'dist',
    [switch]$SkipEngine,
    [switch]$SkipDownloads,
    [switch]$NoSetup,
    [string]$PythonMinor = '3.13',
    [string]$InstallForge = ''
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$Root = $PSScriptRoot
$Dist = if ([IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $Root $OutDir }
$Cache = Join-Path $Dist 'cache'
$AppFolder = 'BasaltWinGUI'                       # the folder InstallForge creates under the install path
$Stage = Join-Path (Join-Path $Dist 'stage') $AppFolder
$IfpPath = Join-Path $Dist 'Basalt-WinGUI.ifp'
$Company = 'Tadamoo'
$InstallRoot = '<ProgramFilesX64>\' + $Company + '\'          # C:\Program Files\Tadamoo\ (not <ProgramFiles>: that is x86)
$AppRel = '<InstallPath>\' + $AppFolder                        # C:\Program Files\Tadamoo\BasaltWinGUI
$ExcludeFromPackage = @('tests', 'serve/tests', '.github', '.agents')   # developer-only, tracked

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
    if (-not $relName) { $relName = New-ReleaseName $usedNames; $usedNames += $relName; $curSub = 0 }
    $newSub = $curSub + 1
    $ReleaseLabel = '{0}_(v_{1}_{2})' -f $relName, $curVersion, $newSub       # the name shown everywhere
    $Version = '{0}.{1}' -f $curVersion, $newSub                              # the number InstallForge takes
    $SetupExe = Join-Path $Dist ("Basalt-WinGUI-{0}-setup.exe" -f $ReleaseLabel)

    # ------------------------------------------------------------------ what is being packaged
    Step "Source tree (release $ReleaseLabel)"
    $commit = (& git rev-parse --short HEAD).Trim()
    $changed = @(& git diff --name-only HEAD -- . ':!VERSION')
    Info "HEAD $commit; tracked files are taken from the working tree"
    if ($changed.Count) { Warn ("{0} tracked file(s) differ from HEAD and go into the package as they are now: {1}" -f $changed.Count, (($changed | Select-Object -First 8) -join ', ')) }

    # ------------------------------------------------------------------ engine
    if ($SkipEngine) {
        Step 'Engine: skipped (-SkipEngine)'
    } else {
        Step 'Engine (CMake, MSVC + CUDA)'
        . (Join-Path $Root 'tools\windows\vsenv.ps1')
        if (-not (Test-Path (Join-Path $Root 'build-win\build.ninja'))) {
            Info 'configuring build-win'
            Run cmake @('-S', '.', '-B', 'build-win', '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_CUDA_ARCHITECTURES=120', '-DCMAKE_C_COMPILER=cl', '-DCMAKE_CXX_COMPILER=cl')
        }
        (Get-Process -Id $PID).PriorityClass = 'BelowNormal'
        Run cmake @('--build', 'build-win', '--parallel', '4')
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

    $cuda = if ($env:CUDA_PATH) { $env:CUDA_PATH } else { 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.1' }
    foreach ($dll in 'cublas64_13.dll', 'cublasLt64_13.dll') {
        $p = Join-Path $cuda "bin\x64\$dll"
        if (-not (Test-Path $p)) { throw "$p not found (CUDA 13.1 toolkit; set CUDA_PATH)" }
        Copy-Item $p $bin
    }
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vs = (& $vswhere -latest -prerelease -products * -property installationPath)
    $crt = Get-ChildItem (Join-Path $vs 'VC\Redist\MSVC\*\x64\Microsoft.VC*.CRT') -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch 'Debug' } | Sort-Object { [version]($_.FullName -replace '^.*\\MSVC\\([\d.]+)\\.*$', '$1') } -Descending | Select-Object -First 1
    if (-not $crt) { throw 'the Microsoft C++ runtime (VC\Redist\MSVC\...\x64\Microsoft.VC*.CRT) was not found in Visual Studio' }
    foreach ($dll in 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') { Copy-Item (Join-Path $crt.FullName $dll) $bin }
    Info "bin: engine, cuBLAS (CUDA EULA, Attachment A), C++ runtime from $($crt.FullName)"
    $lic = Join-Path $Stage 'licenses'
    New-Item -ItemType Directory -Force $lic | Out-Null
    Copy-Item (Join-Path $cuda 'EULA.txt') (Join-Path $lic 'NVIDIA-CUDA-EULA.txt')

    # ------------------------------------------------------------------ desktop app and setup helper
    Step 'Desktop app and setup helper (.NET 8, self-contained)'
    $pub = Join-Path $Stage 'tools\windows\desktop\publish'
    foreach ($proj in 'tools\windows\desktop\BasaltDesktop.csproj', 'tools\windows\setup\BasaltSetup.csproj') {
        Run dotnet @('publish', (Join-Path $Root $proj), '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '-o', $pub, '-nologo', '-v', 'q', '-p:DebugType=None', '-p:DebugSymbols=false', '-p:NoWarn=MSB3277',
            "-p:Version=$Version.0", "-p:InformationalVersion=$ReleaseLabel", '-p:IncludeSourceRevisionInInformationalVersion=false')
    }
    Get-ChildItem $pub -Recurse -Include *.pdb, *.xml | Remove-Item -Force
    foreach ($exe in 'BasaltWinGUI.exe', 'BasaltSetup.exe') { if (-not (Test-Path (Join-Path $pub $exe))) { throw "$exe was not published" } }
    Info ("{0} files, {1:N0} MB" -f (Get-ChildItem $pub -Recurse -File).Count, ((Get-ChildItem $pub -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

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

    # ------------------------------------------------------------------ notes in the package
    @"
Basalt Win+GUI $ReleaseLabel
built $(Get-Date -Format 'yyyy-MM-dd HH:mm') from $commit$(if ($changed.Count) { " (+$($changed.Count) uncommitted tracked file(s))" })

Bundled third-party files that are not part of the repository:
  bin\cublas64_13.dll, bin\cublasLt64_13.dll   NVIDIA cuBLAS, redistributed under the CUDA EULA, Attachment A (licenses\NVIDIA-CUDA-EULA.txt)
  bin\msvcp140.dll, vcruntime140.dll, vcruntime140_1.dll   Microsoft Visual C++ runtime
  setup\python\python-$pyVersion-amd64.exe     Python $pyVersion (PSF license), installed only on a PC that has no suitable Python
  setup\wheels\                                the Python packages of requirements.txt (their own licenses)
The setup\ folder is removed by the installer once everything is in place. The Qwen model has its own license.
"@ | Set-Content -Encoding UTF8 (Join-Path $lic 'BUNDLED-FILES.txt')

    [ordered]@{ version = $curVersion; subversion = $newSub; name = $relName; release = $ReleaseLabel; commit = $commit } |
        ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $Stage 'VERSION')

    # check the staged folder with the setup helper itself (no change made: --dry-run)
    Step 'Checking the staged folder (BasaltSetup --dry-run)'
    & (Join-Path $pub 'BasaltSetup.exe') --root $Stage --dry-run --lang en | ForEach-Object { Info $_ }
    if ($LASTEXITCODE -ne 0) { throw "BasaltSetup --dry-run failed ($LASTEXITCODE)" }

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
    <Name>Basalt Windows+GUI</Name>
    <Version>1.0</Version>
    <CompanyName>Tadamoo</CompanyName>
    <WebURI>https://github.com/nuspy/basalt_win-GUI</WebURI>
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
    <ProductName>Basalt Win+GUI</ProductName>
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
    [void]$cmds.AppendChild((New-El 'Command' @{ type = 'Execute Application'; command = "$AppRel\tools\windows\desktop\publish\BasaltSetup.exe"; commandLineArguments = ''; options = '-wait' } ''))

    # the shortcuts: the app (desktop + start menu) and the uninstaller (start menu)
    $sc = Node 'InstallerConfiguration/Shortcuts'
    Clear-El $sc
    $appExe = "$AppRel\tools\windows\desktop\publish\BasaltWinGUI.exe"
    $icon = Join-Path $Root 'tools\windows\desktop\basalt.ico'             # a path on this PC, as InstallForge stores it
    foreach ($def in @(
            @{ destination = 'Desktop'; name = 'Basalt'; targetFile = $appExe },
            @{ destination = 'Startmenu'; name = 'Basalt'; targetFile = $appExe },
            @{ destination = 'Startmenu'; name = 'Uninstall Basalt'; targetFile = '<InstallPath>\Uninstall.exe' })) {
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
    if (Get-Process BasaltWinGUI -ErrorAction SilentlyContinue) {
        Warn 'BasaltWinGUI is open, so the app on this PC was not updated: close it and the next run will update it (the setup is built anyway)'
    } else {
        New-Item -ItemType Directory -Force $local | Out-Null
        Copy-Item (Join-Path $pub '*') $local -Recurse -Force
        $appUpdated = $true
        Info "updated: $local"
    }

    # ------------------------------------------------------------------ the new numbers are kept only now that everything worked
    [ordered]@{ version = $curVersion; subversion = $newSub; name = $relName; names_used = @($usedNames); release = $ReleaseLabel;
                built = (Get-Date -Format 's'); commit = $commit } | ConvertTo-Json | Set-Content -Encoding UTF8 $VersionFile

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
