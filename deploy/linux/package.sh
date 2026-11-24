#!/bin/bash
# deploy/linux/package.sh COMPLETE.tar.gz [OUT] - makes the server archive of a Linux release from its complete archive.
#
# The complete archive (TaDaMoo-linux-complete-vX.Y.tar.gz) is built by the release-linux workflow: the harness tree, the installer,
# agent-desktop and agent-browser in tools/vendored/, and the Basalt-Plus engine in engines/basalt/. The server archive
# (TaDaMoo-server-linux-vX.Y.tar.gz) is the same tree without the engine, for a machine that answers through providers or Strata.
# Both get a .sha256 file in OUT (default dist); the complete archive is copied there unchanged.
set -euo pipefail

COMPLETE="${1:?usage: deploy/linux/package.sh COMPLETE.tar.gz [OUT]}"
OUT="${2:-dist}"
BASE="$(basename "$COMPLETE")"
case "$BASE" in
  TaDaMoo-linux-complete-v*.tar.gz) ;;
  *) echo "the first argument must be TaDaMoo-linux-complete-vX.Y.tar.gz, not $BASE" >&2; exit 1 ;;
esac
VERSION="${BASE#TaDaMoo-linux-complete-v}"
VERSION="${VERSION%.tar.gz}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

tar -xzf "$COMPLETE" -C "$WORK"
TOP="$(ls "$WORK")"
if [ "$(echo "$TOP" | wc -l)" -ne 1 ] || [ ! -d "$WORK/$TOP/engines/basalt" ]; then
  echo "$BASE does not hold one folder with engines/basalt inside" >&2
  exit 1
fi

rm -rf "$WORK/$TOP/engines/basalt"
rmdir "$WORK/$TOP/engines" 2>/dev/null || true
SERVER="TaDaMoo-server-linux-v${VERSION}.tar.gz"
tar -czf "$OUT/$SERVER" -C "$WORK" "$TOP"
[ "$(cd "$(dirname "$COMPLETE")" && pwd)" = "$(cd "$OUT" && pwd)" ] || cp "$COMPLETE" "$OUT/$BASE"

(cd "$OUT" && sha256sum "$BASE" > "$BASE.sha256" && sha256sum "$SERVER" > "$SERVER.sha256" && cat "$BASE.sha256" "$SERVER.sha256")
ls -la "$OUT/$BASE" "$OUT/$SERVER"
