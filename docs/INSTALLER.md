# Building the Windows setup

`compile_and_package.ps1` (repository root) is the only command. It builds the engine, the desktop app and the setup
helper, stages the program folder, updates the InstallForge project, builds the setup, updates the app on this PC and
raises the version. The user gets **one setup file**; nothing is done by hand after it. Nothing is uploaded anywhere.

```powershell
.\compile_and_package.ps1                   # everything, in order, stopping at the first failure
.\compile_and_package.ps1 -NewVersion       # the same, starting a new version instead of a new subversion
```

Rare options: `-SkipEngine` (keep the binaries already in `bin\`: needed while the engine is running, because a running
program keeps its file locked), `-SkipDownloads` (no network: the Python installer, the wheels and the name generator
from `dist\cache`), `-NoSetup` (stop after the `.ifp` and build the setup by hand in InstallForge).

## The version

It grows by itself and has a name: `<name>_(v_<version>_<subversion>)`, for example `bizarre-smarttv_(v_1_1)`.
The subversion goes up by one at every run that succeeds (a failed run uses no number); `-NewVersion` raises the version
by one, starts the subversion again and makes a new name. The name belongs to the version, is made with
[unique-namer](https://github.com/aziele/unique-namer) (MIT, used only while building, not shipped) and is never one
used before. The numbers and the names used are in `VERSION` at the repository root; commit it when you like. The setup's
file name, the InstallForge version (`<version>.<subversion>`), the app's window title, tray tip and file properties, and
the `VERSION` file inside the installed folder all show it.

Everything is written under `dist\` (ignored by git): `stage\BasaltWinGUI\` (the program as installed),
`Basalt-WinGUI.ifp`, `Basalt-WinGUI-<release>-setup.exe`, `SHA256SUMS.txt` and `cache\`. The app in
`tools\windows\desktop\publish\` is updated too (when the app is open it is left as it is, with a warning, and the next
run updates it).

Needs: Visual Studio with the C++ x64 tools and the v143 toolset, CUDA 13.1, CMake and Ninja (see `WINDOWS.md`), the
.NET 8 SDK, InstallForge (to build the setup; the script uses its command-line builder `ifbuildx86.exe`), a Python on the build PC (only to download wheels),
and the internet the first time (Python installer from python.org, wheels from PyPI; both are kept in `dist\cache`).

## What the installed program looks like

* Folder: `C:\Program Files\Tadamoo\BasaltWinGUI`. The `.ifp` has one entry, the folder `dist\stage\BasaltWinGUI`.
* Shortcuts **Basalt** (desktop and Start menu) start `tools\windows\desktop\publish\BasaltWinGUI.exe`, the app the user
  runs. It is self-contained (.NET included). `Avvia-Basalt.ps1` and `Ferma-Basalt.ps1` stay in the folder because the
  app calls them for "server only" and "stop"; the user does not start them.
* After the files are copied, InstallForge runs `tools\windows\desktop\publish\BasaltSetup.exe` (also self-contained, it
  needs no Python). It:
  1. looks for a 64-bit Python 3.10-3.14 with `venv` and `pip` (registry, then `PATH`; the Microsoft Store stub is not
     one), preferring the bundled version, then one outside a user profile;
  2. if there is none, installs the bundled Python for all users (`setup\python`, signature checked at build time, hash
     checked at install time; no `PATH` change, no `py` launcher);
  3. creates `.venv` and installs `requirements.txt` from `setup\wheels` (no internet); if the wheels do not fit the
     Python found, from the internet;
  4. checks that the packages and `serve.server` import;
  5. creates `logs\` writable for users and lets users **create** files in the folder root (`basalt.pid`,
     `basalt_config.json`); programs, scripts and libraries stay read-only for users;
  6. precompiles `serve\` (the folder is not writable afterwards), then removes `setup\`.
  Its log is `setup.log` in the installed folder. It shows a small progress window.
* User data (profiles, model folders, preferences) stays in `%APPDATA%\BasaltPannello`, as in a working copy.
* The setup is not code-signed: SmartScreen asks for confirmation. InstallForge supports signing (`BuildConfiguration`).
* The installer asks for administrator rights (InstallForge always does). The app is not started at the end of the
  setup, so that it does not run elevated.

## The `.ifp` file

If `dist\Basalt-WinGUI.ifp` exists the script **updates** it: version, install folder, command, shortcuts, finish page,
and the build paths. Everything else is kept, including what you changed in InstallForge (images, texts, languages,
the license page). Delete it to have it created again.

## Not covered by the build

The Qwen model files are not in the setup: the app downloads them (Loader, "Download models"). A PC without the WebView2
runtime opens the interface in the default browser.
