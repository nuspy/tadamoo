# Image sources

A request names an image as a `data:` URL, an `http(s)` URL, a `file:` URL or a path, and the server reads it for any client: there is no list
of allowed hosts or directories and no limit on the size of an image. A [document](documents.md) named by URL or path is read the same way.

| Source | Read |
|---|---|
| `data:` URL | yes |
| `http(s)` URL | any host |
| path or `file:` URL | any regular file the server's user can read |

What is kept: 60 s for a whole read (name lookup and connect included), regular files only (not a pipe or a device), and 1 GiB for a video.
A file is read in pieces, so a large one never needs a buffer of its size at once.

On a shared machine, or behind a proxy that lets other people's requests reach the server, set `--api-key`: the key is what tells a client
from another, and a client that has it may name any file the server's user can read.
