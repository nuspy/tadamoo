# The command line

The PowerShell module of the API-CLI project (github.com/nuspy/API-CLI, the tag named in `compile_and_package.ps1`) is downloaded when the setup is built and goes into the program folder
as `tools/terminal/PSLLMKit`; it puts the harness in the terminal. The setup offers it and, if asked, writes a block in the
user's PowerShell profiles (`WindowsPowerShell` and `PowerShell`) that loads it and calls `Enable-TaDaMooPrefix`; deleting the block takes it away.

- A line that starts with the prefix (default `§`, a setting) is not run by the shell: it goes to the harness. One prefix opens a new conversation in the current folder and
  deletes the one this window had; two go on with that conversation and send along the terminal's text since the last line that began with the prefix (taken from a transcript).
- The agent's answer is printed under the line in a colour (default `#2E7BFF`) with a bar on its left; the lines of its tools are grey. A permission question is asked in the terminal.
- `tadamoo` opens a conversation with an agent in the current folder, `tadamoo "task"` does one task.
- To change something in the person's own shell (the folder, a variable, a virtual environment, an ssh connection) the agent calls `terminal_run`, which writes ONE command line on the
  prompt and waits for Enter; the harness never runs it.

Settings > General > Command line holds the prefix, the colour, the server, its key and the level; they are kept in `terminal.json` in the user's data folder, which the module reads.
A message that comes from the terminal carries a `terminal` field (shell, version, system, working folder, virtual environment); the conversation process (`tadamoo/terminal.py`)
tells the agent once, and again when it changes, and offers `terminal_run` from then on. The command arrives as a `terminal_command` event of the conversation.

The setup also writes two profiles, "TaDaMoo CLI (PowerShell 7)" and "TaDaMoo CLI (Windows PowerShell)", as a Windows Terminal fragment (`%ProgramData%\Microsoft\Windows Terminal\Fragments\TaDaMoo	adamoo.json`; a
copy of the module in a folder of the person's own goes in `%LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\TaDaMoo`), so they appear in the terminal's menu and in the right click on its icon; they start the shell with
the module loaded and carry the icon `tools/terminal/tadamoo-cli.ico` (made from `icon.gif` of API-CLI; the sources are in `tools/windows/terminal`). The PowerShell 7 profile (the Windows PowerShell one when 7 is not installed) also becomes the **default profile** of
Windows Terminal, so its icon opens the TaDaMoo command line: the setup changes only the value of `defaultProfile` in `settings.json` (store, preview and unpackaged installs) and keeps the file as `settings.json.before-tadamoo.bak`.
