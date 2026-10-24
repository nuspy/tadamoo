"""Tests never touch the user's own Basalt data (profiles, journals, plans, the control secret): they get a folder of their own.

The folder is set before any test module is imported, because the server reads it when its modules load."""
import atexit
import os
import shutil
import tempfile

_DATA = tempfile.mkdtemp(prefix="basalt-test-data-")
os.environ["BASALT_DATA"] = _DATA
atexit.register(shutil.rmtree, _DATA, True)
