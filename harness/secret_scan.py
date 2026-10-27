"""What a command that carries a secret will really do, as far as its text and the scripts it runs can say.

The destination check of the secrets broker reads sites written in the command. A command can instead name a script in the workspace that
holds the address, or build the address at run time. This adds the text of the scripts the command runs, and tells whether the program
uses the network at all, so a secret limited to some sites is not given to a program whose destination cannot be seen.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import List

SCRIPT_SUFFIXES = (".py", ".pyw", ".js", ".mjs", ".cjs", ".ps1", ".bat", ".cmd", ".sh")
MAX_SCRIPT_BYTES = 256 * 1024
MAX_SCRIPTS = 6
PROGRAM_THAT_USES_THE_NETWORK = "a program that uses the network"
NETWORK_API = re.compile(
    r"urlopen|urllib\.request|urllib2|\brequests\.|\bhttpx\b|http\.client|aiohttp|\bsocket\b|\bfetch\s*\(|XMLHttpRequest|WebSocket|net\.connect|\bhttps?\.(?:request|get)\s*\("
    r"|Invoke-WebRequest|Invoke-RestMethod|\biwr\b|\birm\b|WebClient|System\.Net|ftplib|smtplib|telnetlib|\bcurl\b|\bwget\b|\bnc\b|\bncat\b", re.I)


def _tokens(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def scripts_run(command: str, cwd: "str | Path") -> str:
    """The text of the script files in the working folder that the command names (a few, each cut at a size limit)."""
    root = Path(cwd).resolve()
    texts: List[str] = []
    for token in _tokens(command):
        name = token.strip("\"'")
        if not name.lower().endswith(SCRIPT_SUFFIXES):
            continue
        try:
            path = (root / name).resolve()
            path.relative_to(root)
            if path.is_file():
                texts.append(path.read_bytes()[:MAX_SCRIPT_BYTES].decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        if len(texts) >= MAX_SCRIPTS:
            break
    return "\n".join(texts)


def uses_network(text: str) -> bool:
    return bool(NETWORK_API.search(text))
