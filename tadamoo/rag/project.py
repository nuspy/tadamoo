"""The key of a project: knowledge archives belong to a project (a workspace folder), and this is how a folder is named in their `meta.json`."""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

KEY = re.compile(r"^[0-9a-f]{12}$")


def project_key(path: Any) -> str:
    """12 lowercase hex characters identifying the folder `path`: the same for every spelling of it (case where the file system ignores it,
    trailing separators, links), different for different folders."""
    real = os.path.normcase(os.path.realpath(os.fspath(path)))
    return hashlib.sha1(real.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def is_key(value: Any) -> bool:
    return isinstance(value, str) and bool(KEY.match(value))
