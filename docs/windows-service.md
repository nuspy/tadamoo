# The server as a Windows service

The server can be kept up by a Windows service (NSSM) that runs `Avvia-TaDaMoo.ps1 -PerApp -Standby`. The service starts the server again when it ends. Turn off and Restart have to
agree with that.

- **The launcher's exit code.** `Avvia-TaDaMoo.ps1` ends with 0 when the server ended with 0 (a clean exit: the server's own shutdown) and with 1 when the server
  ended with any other code (a crash, a kill).
- **The service's reaction.** NSSM restarts the launcher after 10 seconds by default, whatever the code. With `AppExit 0 Exit` a clean exit stays stopped and every other code
  starts the server again. Run once, from an elevated PowerShell:

  ```
  nssm set TaDaMoo AppExit 0 Exit
  nssm set TaDaMoo AppEnvironmentExtra TADAMOO_SUPERVISED=1
  ```

- **Restart (Settings > Admin).** Under a supervisor (`TADAMOO_SUPERVISED=1`) the server answers, then exits with `RESTART_EXIT` (75, `tadamoo/restart.py`), so the service starts
  it again. Without the variable a plain restart relaunches the command by a detached helper; with `TADAMOO_SERVICE_NAME` set it asks the service manager (`sc stop`, `sc start`),
  which needs the right to stop the service.
- **Turn off.** The tray's Turn off runs `Ferma-TaDaMoo.ps1`, which stops the Windows service itself, with an elevation prompt (UAC) when the shell is not elevated.
  A server that is only killed (`taskkill /F`) ends with a non-zero code and the service starts it again.
- **Stopping the service by hand:** `Stop-Service TaDaMoo -Force` from an elevated PowerShell.

Tests: `tests/test_restart.py`.
