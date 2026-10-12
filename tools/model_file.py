"""Read and write the .basalt model file (the layout in include/basalt/core/model_format.hpp).

    w = ModelWriter(path)
    w.add_bytes("meta", json_bytes)
    w.add_file("experts", "/path/experts.bin")        # streamed and hashed while copied
    w.add_chunks("dense", chunk_iterator)             # streamed
    w.close()

    m = ModelFile(path)
    m.names(); m.section("meta") -> (offset, bytes); m.read("meta") -> bytes; m.verify("dense") -> bool

    python tools/model_file.py verify FILE.basalt      # every section, with progress lines (exit 3: a bad one)
"""
from __future__ import annotations

import hashlib
import os
import struct

MAGIC = b"BASALTMF"
VERSION = 1
SECTION_ALIGN = 2 << 20
HEADER_BYTES = 64 << 10
TENSOR_ALIGN = 4096                            # inside the `gguf` section
HEADER = struct.Struct("<8sIIQ40s")           # 64 bytes
ENTRY = struct.Struct("<64sQQ32s16s")         # 128 bytes
MAX_SECTIONS = (HEADER_BYTES - HEADER.size) // ENTRY.size
COPY_CHUNK = 64 << 20
VERIFY_CHUNK = 16 << 20                        # verify(): one buffer, reused
# general.sampling.* (llama.cpp's recommended-sampling keys in a GGUF header) -> the meta "sampling" block's keys
GGUF_SAMPLING = {"general.sampling.temp": "temperature", "general.sampling.top_p": "top_p",
                 "general.sampling.top_k": "top_k", "general.sampling.min_p": "min_p",
                 "general.sampling.penalty_repeat": "repetition_penalty",
                 "general.sampling.penalty_last_n": "penalty_last_n"}


def gguf_sampling(kv: dict) -> dict:
    """The sampling defaults a GGUF header declares (general.sampling.*), as the meta "sampling" block's keys; a value
    the server would refuse (serve/config.py sampling_defaults_from_config) is left out."""
    out = {}
    for key, name in GGUF_SAMPLING.items():
        v = (kv or {}).get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if name in ("top_k", "penalty_last_n"):
            if v != int(v) or (name == "top_k" and not 1 <= v <= 64) or (name == "penalty_last_n" and v < 0):
                continue
            out[name] = int(v)
            continue
        if (name == "temperature" and v < 0) or (name == "top_p" and not 0 < v <= 1) or \
                (name == "min_p" and not 0 <= v <= 1) or (name == "repetition_penalty" and v <= 0):
            continue
        out[name] = round(float(v), 6)                 # an f32 in the header: 0.95 reads 0.949999988079071
    return out


def meta_sampling(meta: dict) -> tuple[dict, str]:
    """(a model file's sampling defaults, their source). A file whose meta names its sampling_source has the block
    as written. An older file's block was a fixed fallback: the source GGUF's own general.sampling.* keys, kept in
    the meta's copy of that header (gguf_kv), win over it."""
    block = dict(meta.get("sampling") or {}) if isinstance(meta.get("sampling"), dict) else {}
    if meta.get("sampling_source"):
        return block, str(meta["sampling_source"])
    found = gguf_sampling(meta.get("gguf_kv") if isinstance(meta.get("gguf_kv"), dict) else {})
    return ({**block, **found}, "gguf") if found else (block, "file")


def _align(n: int) -> int:
    return (n + SECTION_ALIGN - 1) // SECTION_ALIGN * SECTION_ALIGN


class ModelWriter:
    """Sections are appended in order; the header and table are written by close()."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(b"\0" * HEADER_BYTES)
        self.entries: list[tuple[str, int, int, bytes]] = []

    def _start(self, name: str) -> int:
        if len(name.encode()) >= 64:
            raise ValueError(f"section name too long: {name}")
        if any(e[0] == name for e in self.entries):
            raise ValueError(f"duplicate section: {name}")
        if len(self.entries) >= MAX_SECTIONS:
            raise ValueError("too many sections")
        off = _align(self.f.tell())
        self.f.write(b"\0" * (off - self.f.tell()))
        return off

    def add_bytes(self, name: str, data: bytes) -> None:
        off = self._start(name)
        self.f.write(data)
        self.entries.append((name, off, len(data), hashlib.sha256(data).digest()))

    def add_file(self, name: str, src: str, offset: int = 0, size: int | None = None) -> None:
        """Copy `size` bytes of `src` from `offset` (the whole rest of the file by default)."""
        off = self._start(name)
        h = hashlib.sha256()
        left = (os.path.getsize(src) - offset) if size is None else size
        n = 0
        with open(src, "rb") as s:
            s.seek(offset)
            while left > 0:
                b = s.read(min(COPY_CHUNK, left))
                if not b:
                    raise IOError(f"{src}: short read")
                h.update(b)
                self.f.write(b)
                left -= len(b)
                n += len(b)
        self.entries.append((name, off, n, h.digest()))

    def add_chunks(self, name: str, chunks) -> int:
        """One section from an iterator of byte chunks (streamed, hashed as written).  Returns its size."""
        off = self._start(name)
        h = hashlib.sha256()
        n = 0
        for b in chunks:
            h.update(b)
            self.f.write(b)
            n += len(b)
        self.entries.append((name, off, n, h.digest()))
        return n

    def add_parts(self, name: str, parts, align: int = TENSOR_ALIGN) -> list[int]:
        """One section of several parts, each at a multiple of `align` (zero padded). A part is bytes or
        (src_path, offset, size); returns each part's offset. The hash covers the whole section."""
        off = self._start(name)
        h = hashlib.sha256()
        pos, offsets = 0, []

        def put(b: bytes) -> None:
            nonlocal pos
            h.update(b)
            self.f.write(b)
            pos += len(b)

        for part in parts:
            pad = (-pos) % align
            if pad:
                put(b"\0" * pad)
            offsets.append(pos)
            if isinstance(part, (bytes, bytearray)):
                put(bytes(part))
                continue
            src, o, size = part
            left = size
            with open(src, "rb") as s:
                s.seek(o)
                while left > 0:
                    b = s.read(min(COPY_CHUNK, left))
                    if not b:
                        raise IOError(f"{src}: short read")
                    put(b)
                    left -= len(b)
        self.entries.append((name, off, pos, h.digest()))
        return offsets

    def close(self) -> None:
        end = self.f.tell()
        self.f.seek(0)
        self.f.write(HEADER.pack(MAGIC, VERSION, len(self.entries), end, b"\0" * 40))
        for name, off, n, digest in self.entries:
            self.f.write(ENTRY.pack(name.encode(), off, n, digest, b"\0" * 16))
        self.f.close()


class ModelFile:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
        magic, version, n, file_bytes, _ = HEADER.unpack_from(head, 0)
        if magic != MAGIC:
            raise ValueError(f"{path}: not a .basalt model file")
        if version != VERSION:
            raise ValueError(f"{path}: model file version {version}, this build reads {VERSION}")
        if os.path.getsize(path) < file_bytes:
            raise ValueError(f"{path}: truncated ({os.path.getsize(path)} of {file_bytes} bytes)")
        self.sections: dict[str, tuple[int, int, bytes]] = {}
        for i in range(n):
            name, off, size, digest, _ = ENTRY.unpack_from(head, HEADER.size + i * ENTRY.size)
            self.sections[name.rstrip(b"\0").decode()] = (off, size, digest)

    def names(self) -> list[str]:
        return list(self.sections)

    def section(self, name: str) -> tuple[int, int]:
        off, size, _ = self.sections[name]
        return off, size

    def read(self, name: str) -> bytes:
        off, size = self.section(name)
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(size)

    def verify(self, name: str, progress=None, buffer: bytearray | None = None) -> bool:
        """The section's SHA-256 against the table, read through one reused 16 MiB buffer (a new 64 MiB bytes object
        per read could fail with MemoryError on a machine short of commit, and churned the allocator). `progress(n)`
        is called with the bytes of each read; `buffer` lets several sections share one buffer."""
        off, size, digest = self.sections[name]
        h = hashlib.sha256()
        view = memoryview(buffer if buffer is not None else bytearray(VERIFY_CHUNK))
        with open(self.path, "rb", buffering=0) as f:
            f.seek(off)
            left = size
            while left > 0:
                n = f.readinto(view[:min(len(view), left)])
                if not n:
                    return False
                h.update(view[:n])
                left -= n
                if progress is not None:
                    progress(n)
        return h.digest() == digest


def main(argv=None) -> int:
    """python tools/model_file.py verify FILE: every section's SHA-256 against the table, through one reused 16 MiB
    buffer. Prints `progress DONE TOTAL` about every GiB, one line per section and a last JSON line; exit 0 when all
    match, 3 when a section does not, 2 when the file cannot be read."""
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(prog="python tools/model_file.py")
    ap.add_argument("cmd", choices=["verify"])
    ap.add_argument("file")
    a = ap.parse_args(argv)
    try:
        m = ModelFile(a.file)
    except (OSError, ValueError) as e:
        print(f"error: {e}", flush=True)
        print(json.dumps({"ok": False, "error": str(e)}), flush=True)
        return 2
    total = sum(size for _, size, _ in m.sections.values())
    done, shown = 0, 0
    buf = bytearray(VERIFY_CHUNK)

    def tick(n):
        nonlocal done, shown
        done += n
        if done - shown >= 1 << 30 or done == total:
            shown = done
            print(f"progress {done} {total}", flush=True)

    bad = []
    for name in m.names():
        try:
            ok = m.verify(name, tick, buf)
        except OSError as e:
            print(f"error: {name}: {e}", flush=True)
            ok = False
        print(f"{'ok ' if ok else 'BAD'} {name}", flush=True)
        if not ok:
            bad.append(name)
    print(f"progress {total} {total}", flush=True)
    print(json.dumps({"ok": not bad, "bad": bad, "sections": len(m.sections), "bytes": total}), flush=True)
    return 0 if not bad else 3


if __name__ == "__main__":
    raise SystemExit(main())
