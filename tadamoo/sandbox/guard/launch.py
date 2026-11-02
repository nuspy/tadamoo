"""Runs the snippet given as the only argument with the guard installed, in isolated mode like a direct run."""
import os
import runpy
import sys
import traceback

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import basalt_guard  # noqa: E402

basalt_guard.install()
sys.path.remove(here)
script = sys.argv[1]
sys.argv = [script]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit:
    raise
except BaseException as exc:
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != script:       # the traceback starts at the snippet, not at this launcher
        tb = tb.tb_next
    traceback.print_exception(type(exc), exc, tb if tb is not None else exc.__traceback__)
    sys.exit(1)
