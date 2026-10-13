"""Tests for the server: python -m unittest discover -s serve/tests -t ."""
import os
import tempfile

# a test never reads or writes the user's own settings or caches: both default to a scratch folder for this run
_scratch = tempfile.mkdtemp(prefix="basalt-tests-")
os.environ.setdefault("BASALT_DATA", os.path.join(_scratch, "data"))
os.environ.setdefault("BASALT_CACHE", os.path.join(_scratch, "cache"))
# messages in one language whatever the system's (the language tests set their own)
os.environ.setdefault("BASALT_LANG", "it")
# the start script looks for running servers only on this repository's test ports, never on a real one (7232)
os.environ.setdefault("BASALT_SCAN_PORTS", "7280-7289")
