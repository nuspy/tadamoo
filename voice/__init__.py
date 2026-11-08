"""The voice service of TaDaMoo. Its modules import `callbridge` by that name, so this folder is put on sys.path."""
import sys
from pathlib import Path

_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)
