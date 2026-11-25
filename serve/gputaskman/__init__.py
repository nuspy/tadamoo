"""gputaskman - an htop for what's *inside* your GPU."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gputaskman")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "unknown"
