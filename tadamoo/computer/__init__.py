"""Computer control: the agent looks at the screen and works this PC's mouse and keyboard (docs/computer-control.md)."""
from tadamoo.computer.backend import Backend, ComputerError, pick_backend
from tadamoo.computer.prefs import ComputerPrefs
from tadamoo.computer.session import Computer
from tadamoo.computer.tools import register_computer_tools

__all__ = ["Backend", "Computer", "ComputerError", "ComputerPrefs", "pick_backend", "register_computer_tools"]
