# Windows setup, GUI only

A variant of the Windows setup for a PC that only needs the **window**: no engine, no Python, no server on this PC. The window shows a TaDaMoo server that runs elsewhere (a Linux install behind nginx, another
PC). At its start it asks which server to use ([desktop-servers.md](desktop-servers.md)). The full setup is `compile_and_package.ps1` ([INSTALLER.md](INSTALLER.md)).

## Build (on the build PC)

    .\tools\windows\gui_only\Make-GuiOnly.ps1

Needs the .NET 8 SDK only (no CUDA, no Python, no InstallForge). It makes `dist\gui-only\TaDaMooGUI-<release>-setup.zip` and `SHA256SUMS.txt`: the app (self-contained), a file `gui-only.flag` that makes the
app start as the window alone (the same as `--gui-only`), and the two scripts below. The release label is read from `VERSION`. Nothing is uploaded.

## Install (on the PC that gets it)

Unzip, then run `Install-TaDaMooGUI.ps1` (it asks for administrator rights). It copies the folder to `C:\Program Files\Tadamoo\TaDaMooGUI`, adds the Start menu shortcut "TaDaMoo (window only)" and an entry in
"Installed apps". It says so when the WebView2 runtime is missing (the window needs it; without it the page opens in the browser). `Remove-TaDaMooGUI.ps1` (or "Installed apps") removes it; the list of servers
(`servers.json` in the user's app data) is kept.

The window alone uses its own single-instance name, so it can sit next to the full app on one PC.
