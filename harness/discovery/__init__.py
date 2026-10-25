"""Finding a missing capability online (MCP servers and skills), scoring what is found and installing the one the person chooses.

sources     the public services asked (the MCP registry, npm, PyPI, GitHub), standard library only
scoring     a transparent 0-100 score of popularity and upkeep (not of safety) with plain-word warnings
finder      turns the need into scored candidates
installer   fixed commands only, integrity checks, registration as approved, removal
tool        `find_online_capability` for the agent and the question put to the person
"""
