"""Copy one section out of a .basalt file, a local one or one on Hugging Face (HTTP range requests: the header and that section only).

    python tools/convert/pack_section.py list SOURCE
    python tools/convert/pack_section.py copy SOURCE SECTION --out FILE

SOURCE is a path or an https URL of a .basalt file. The section's SHA-256 is checked against the file's own table. The tuned drafter layer
of the official packs (`mtp/dense`) is what `convert.py --mtp-dense` takes.
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from model_file import ENTRY, HEADER, HEADER_BYTES  # noqa: E402

CHUNK = 64 << 20


def _is_url(source: str) -> bool:
    return source.startswith(("https://", "http://"))


def _read(source: str, start: int, end: int) -> bytes:
    """Bytes start..end (inclusive) of the file."""
    if _is_url(source):
        import mtp_fetch
        return mtp_fetch.get(source, start, end)
    with open(source, "rb") as f:
        f.seek(start)
        data = f.read(end - start + 1)
    if len(data) != end - start + 1:
        raise IOError(f"{source}: short read")
    return data


def table(source: str) -> dict:
    """{section name: (offset, size, sha256 bytes)} of a .basalt file."""
    head = _read(source, 0, HEADER_BYTES - 1)
    magic, version, n, _, _ = HEADER.unpack_from(head, 0)
    if not magic.startswith(b"BASALTMF"):
        raise ValueError(f"{source}: not a .basalt model file")
    rows = {}
    for i in range(n):
        name, off, size, digest, _ = ENTRY.unpack_from(head, HEADER.size + i * ENTRY.size)
        rows[name.rstrip(b"\0").decode()] = (off, size, digest)
    return rows


def copy(source: str, section: str, out: str) -> int:
    rows = table(source)
    if section not in rows:
        raise ValueError(f"{source} has no section {section}: {', '.join(rows)}")
    off, size, digest = rows[section]
    h = hashlib.sha256()
    tmp = out + ".part"
    with open(tmp, "wb") as f:
        pos = off
        while pos < off + size:
            end = min(pos + CHUNK, off + size) - 1
            data = _read(source, pos, end)
            h.update(data)
            f.write(data)
            pos = end + 1
            print(f"progress {pos - off} {size}", flush=True)
    if h.digest() != digest[:32]:
        os.remove(tmp)
        raise ValueError(f"{section}: its SHA-256 is not the one in the file's table")
    os.replace(tmp, out)
    return size


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("source")
    c = sub.add_parser("copy")
    c.add_argument("source")
    c.add_argument("section")
    c.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        if a.cmd == "list":
            for name, (off, size, _) in table(a.source).items():
                print(f"{name:32} {size:>14}")
        else:
            print(f"copied {copy(a.source, a.section, a.out)} bytes to {a.out}")
    except (OSError, ValueError) as e:
        sys.exit(f"pack_section: {e}")


if __name__ == "__main__":
    main()
