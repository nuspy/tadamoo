# Separately distributed packages

These programs travel with TaDaMoo but are **separate packages**: each is installed, started and removed on its own, runs as its own process, and keeps its own licence. They are
not part of the TaDaMoo licence (`LICENSE`) and are not covered by `THIRD_PARTY_LICENSES`. The licence of each applies to its code; this file says where its source is and what
you must know before using or passing it on.

## Silo (the community fork of MinIO): image storage for the knowledge archives

| | |
|---|---|
| What it does | An S3-compatible object store. TaDaMoo keeps the images of the documents in it (`docs/rag-figures.md`) when the knowledge setting `images_store` is `s3` |
| Program | `silo` (the fork of MinIO), release `RELEASE.2026-11-12T00-00-00Z`, Windows x64 zip, 37,717,959 bytes, SHA-256 `4064756dcea753b21480f985bde045e111e4b68a593a5ad0e4e932625574e8e0` |
| Source | https://github.com/pgsty/silo (the source of every release is in the repository, tag `RELEASE.2026-11-12T00-00-00Z`) |
| Licence | GNU Affero General Public License v3 (AGPL-3.0), as MinIO. The licence text is in the release's source; read it there before relying on this line |
| How TaDaMoo uses it | Unmodified, started by TaDaMoo as a separate process on 127.0.0.1 with its own data folder and port (9400-9499), and spoken to over the network protocol. TaDaMoo does not link to it or change it |
| Why a separate notice | The AGPL asks that whoever passes the program on also passes on the licence and the way to get its source. This file and the address above are that notice |
| Upstream state | MinIO's own community edition is archived (no new releases, no security fixes from MinIO). The fork is maintained by someone else, with no promise from TaDaMoo about it |
| If one is already installed | The setup finds it and offers to use it or to install a separate copy: `tadamoo/minio_setup.py` |

If you pass TaDaMoo on with this program inside, keep this file next to it and give the recipient the source address above (or the source itself).

## Avatar packages with their own licences

Talking-head models are installed as packages of their own (`voice/avatar/`). Their licences are on their code and are kept apart from TaDaMoo's. As recorded in this project's
research notes (check each upstream before relying on it):

| Package | Licence situation |
|---|---|
| MuseTalk | The code and weights carry a **non-commercial** condition. Use for commercial work is not allowed by that licence |
| Wav2Lip (the newer version) | No licence file in its repository: all rights stay with the authors, so nothing is granted by default |
| SadTalker | No licence file for the whole code (parts have their own): same situation |
| ER-NeRF | No licence file: same situation |

TaDaMoo does not decide for you. It installs these packages on request, tells you here and at install time that their licence is separate, missing or non-commercial, and leaves
the responsibility for their use and for passing them on to you.
