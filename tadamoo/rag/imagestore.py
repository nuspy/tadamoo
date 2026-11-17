"""The images of the archives' documents (figures, pictures of pages and of web pages) as files in a folder, named by what they hold.

An image is stored under a key made of the hash of its bytes, so the same picture used by several documents, versions or archives is one file, and a file is never rewritten.
The files are spread over two levels of folders (`ab/cd/<key>.webp`) so that tens of thousands of them do not sit in one folder. The archives' databases keep only the key."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Iterator, Optional, Set

KEY = re.compile(r"^[0-9a-f]{16,64}(_t)?$")
EXT = ".webp"


class ImageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        if not isinstance(key, str) or not KEY.match(key):
            raise ValueError("an image key is the hash of its bytes (and `_t` for its thumbnail)")
        return self.root / key[:2] / key[2:4] / (key + EXT)

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def put(self, key: str, data: bytes) -> bool:
        """Keeps `data` under `key`; False when it is already there. The file appears whole or not at all."""
        target = self.path(key)
        with self._lock:
            if target.is_file():
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + f".{os.getpid()}.part")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        return True

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self.path(key).read_bytes()
        except (OSError, ValueError):
            return None

    def keys(self) -> Iterator[str]:
        for f in self.root.glob("??/??/*" + EXT):
            if KEY.match(f.stem):
                yield f.stem

    def sweep(self, keep: Set[str]) -> int:
        """Removes every file whose key is not in `keep` (the keys that archives still name) and says how many went."""
        gone = 0
        with self._lock:
            for key in list(self.keys()):
                if key not in keep:
                    try:
                        self.path(key).unlink()
                        gone += 1
                    except OSError:
                        continue
        return gone
