"""A vision projector the engine's image encoder can read, made from one it cannot.

The encoder reads a qwen3vl_merger projector with its matrices in BF16 and its vectors in F32 (src/vision/mmproj.cpp). Projectors
are also published with the matrices in F16, or the vectors in F16 or BF16. They hold the same weights, so the load rewrites the
types: a vector goes to F32 exactly; a matrix goes to BF16, exactly when the weights were BF16 to begin with (F16 has more
mantissa bits than BF16) and rounded to nearest otherwise. The result has the size of the original and is kept in the cache, so
it is made once.
"""
from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from pathlib import Path

CHUNK = 1 << 24                                          # bytes of one tensor read at a time


def _f32(raw: bytes, kind: int):
    import numpy as np
    if kind == 0:
        return np.frombuffer(raw, dtype="<f4")
    if kind == 1:
        return np.frombuffer(raw, dtype="<f2").astype(np.float32)
    return (np.frombuffer(raw, dtype="<u2").astype(np.uint32) << 16).view(np.float32)


def _bf16(x) -> bytes:
    """float32 values as BF16 bytes, rounded to nearest even; a NaN stays a NaN."""
    import numpy as np
    u = np.ascontiguousarray(x, dtype=np.float32).view(np.uint32).astype(np.uint64)
    out = ((u + ((u >> 16) & 1) + 0x7FFF) >> 16).astype(np.uint16)
    out[np.isnan(np.ascontiguousarray(x, dtype=np.float32))] = 0x7FC0
    return out.astype("<u2").tobytes()


def _write_tensor(src, out, kind: int, goal: int, n: int, nbytes: int) -> int:
    """The tensor at the source's position as `goal` ({0: F32, 30: BF16}); the bytes written."""
    import numpy as np
    if kind == goal:
        left = nbytes
        while left:
            b = src.read(min(left, CHUNK))
            if not b:
                raise OSError("the projector ends inside a tensor")
            out.write(b)
            left -= len(b)
        return nbytes
    width = 4 if kind == 0 else 2
    raw = src.read(n * width)
    if len(raw) != n * width:
        raise OSError("the projector ends inside a tensor")
    values = _f32(raw, kind)
    data = values.astype("<f4").tobytes() if goal == 0 else _bf16(values)
    out.write(data)
    return len(data)


def rewrite(source: str, dest: str, offset: int = 0) -> int:
    """Writes the projector GGUF stored at `offset` of `source` to `dest` with the types the encoder reads (a partial file is never
    left under `dest`); the number of tensors whose type changed. ValueError when the projector has a problem a rewrite does not solve."""
    from serve import models
    g = models.read_gguf(source, offset)
    problem = models.projector_problem(g)
    if problem:
        raise ValueError(problem["en"])
    layers, proj = int(g["meta"]["clip.vision.block_count"]), int(g["meta"]["clip.vision.projection_dim"])
    want = models._projector_wants(layers, proj)
    align = g["alignment"]
    infos, position = [], 0
    for name, shape, t, off in g["tensors"]:
        n = models._elements(shape)
        goal = want[name][0] if name in want else int(t)
        nbytes = n * (4 if goal == 0 else 2) if name in want else models._tensor_bytes(shape, t)
        if nbytes is None:
            raise ValueError(f"tensor {name} has a type the rewrite cannot copy ({models.type_name(t)})")
        infos.append((name, shape, int(t), goal, off, n, nbytes, position))
        position += (nbytes + align - 1) // align * align
    head = bytearray()
    with open(source, "rb") as f:
        f.seek(offset)
        head += f.read(g["kv_end"])
    for name, shape, _t, goal, _off, _n, _nb, pos in infos:
        raw = name.encode("utf-8")
        head += struct.pack("<Q", len(raw)) + raw + struct.pack("<I", len(shape)) + struct.pack(f"<{len(shape)}Q", *shape) + struct.pack("<IQ", goal, pos)
    head += b"\0" * ((-len(head)) % align)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(Path(dest).parent), prefix=Path(dest).name + ".")
    changed = 0
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            out.write(head)
            for name, _shape, t, goal, off, n, nbytes, _pos in infos:
                src.seek(offset + g["data_start"] + off)
                written = _write_tensor(src, out, t, goal, n, nbytes) if name in want else _copy(src, out, nbytes)
                changed += t != goal
                out.write(b"\0" * ((-written) % align))
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return changed


def _copy(src, out, nbytes: int) -> int:
    left = nbytes
    while left:
        b = src.read(min(left, CHUNK))
        if not b:
            raise OSError("the projector ends inside a tensor")
        out.write(b)
        left -= len(b)
    return nbytes


def made_name(source: str, offset: int, size: int, tag: str = "") -> str:
    """The cache file name for a rewritten projector: it changes with the source's place, size and date."""
    try:
        stamp = os.stat(source).st_mtime_ns
    except OSError:
        stamp = 0
    key = f"{os.path.abspath(source)}|{offset}|{size}|{stamp}|{tag}".encode("utf-8")
    return f"mmproj-{hashlib.sha1(key).hexdigest()[:12]}.bf16.gguf"
