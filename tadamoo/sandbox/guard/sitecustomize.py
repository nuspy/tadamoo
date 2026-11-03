"""Loaded by every Python the agent's commands start: installs the guard when the environment asks for it."""
try:
    import tadamoo_guard
    tadamoo_guard.install()
except ImportError:
    pass
