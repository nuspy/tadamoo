"""The original files of the documents, kept as they were added and given back as files.

An original is stored under the SHA-256 of its bytes (`ab/cd/<sha256>`), so the same file added to several archives, or added again, is one copy; the document's meta already holds that
hash, so nothing else is recorded. The copy is made read-only and is never opened or changed here: what a person gets is a copy to save somewhere else."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from pathlib import Path
from typing import Iterator, Optional, Set

KEY = re.compile(r"^[0-9a-f]{64}$")


class Originals:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not KEY.match(sha256):
            raise ValueError("an original is named by the SHA-256 of its bytes")
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def has(self, sha256: str) -> bool:
        try:
            return self.path(sha256).is_file()
        except ValueError:
            return False

    def get(self, sha256: str) -> Optional[Path]:
        try:
            p = self.path(sha256)
        except ValueError:
            return None
        return p if p.is_file() else None

    def put(self, source: Path, sha256: str) -> bool:
        """Copies `source` in, checking on the way that its bytes are those the hash names; False when the copy is already there. The copy is read-only."""
        target = self.path(sha256)
        with self._lock:
            if target.is_file():
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + f".{os.getpid()}.part")
            h = hashlib.sha256()
            try:
                with open(source, "rb") as src, open(tmp, "wb") as out:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        h.update(block)
                        out.write(block)
                if h.hexdigest() != sha256:
                    raise ValueError("the file is not the one its hash names")
                os.replace(tmp, target)
                os.chmod(target, stat.S_IREAD)
            finally:
                tmp.unlink(missing_ok=True)
        return True

    def keys(self) -> Iterator[str]:
        for f in self.root.glob("??/??/*"):
            if KEY.match(f.name):
                yield f.name

    def sweep(self, keep: Set[str]) -> int:
        """Removes the copies whose hash is not in `keep` (the hashes of the documents archives still hold); how many went."""
        gone = 0
        with self._lock:
            for key in list(self.keys()):
                if key not in keep:
                    p = self.path(key)
                    try:
                        os.chmod(p, stat.S_IWRITE)
                        p.unlink()
                        gone += 1
                    except OSError:
                        continue
        return gone
