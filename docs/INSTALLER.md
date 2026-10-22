# Building the Windows setup

`compile_and_package.ps1` (repository root) builds everything and prepares the InstallForge project; the setup itself is
built by hand: load `dist\Basalt-WinGUI.ifp` in InstallForge and build. The user gets **one setup file**; nothing is
done by hand after it.

```powershell
.\compile_and_package.ps1                    # engine, app, setup helper, Python assets, stage folder, .ifp
.\compile_and_package.ps1 -BuildSetup        # and also the setup .exe, through InstallForge's command-line builder
.\compile_and_package.ps1 -SkipEngine        # keep the binaries already in bin\
.\compile_and_package.ps1 -SkipDownloads     # no network: Python installer and wheels from dist\cache
.\compile_and_package.ps1 -Version 1.0       # the version written to the .ifp (default: the one already in it)
```

Everything is written under `dist\` (ignored by git): `stage\BasaltWinGUI\` (the program as installed),
`Basalt-WinGUI.ifp`, `cache\`, and with `-BuildSetup` also `Basalt-WinGUI-<version>-setup.exe` and `SHA256SUMS.txt`.

Needs: Visual Studio with the C++ x64 tools and the v143 toolset, CUDA 13.1, CMake and Ninja (see `WINDOWS.md`), the
.NET 8 SDK, InstallForge (to build the setup; the script uses its command-line builder `ifbuildx86.exe` only with `-BuildSetup`), a Python on the build PC (only to download wheels),
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
