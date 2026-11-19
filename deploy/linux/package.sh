#!/bin/bash
# deploy/linux/package.sh - builds the two Linux release tarballs from a built tree: tadamoo-linux-complete-vX.Y.tar.gz (the
# harness, the installer and the compiled Basalt-Plus engine) and tadamoo-server-linux-vX.Y.tar.gz (the harness and the
# installer alone, for a machine that uses a provider or Strata instead). Run from the repository root, after `cmake --build`
# has put bin/basalt, bin/basalt-bench, bin/basalt-probe and bin/basalt-vision in place.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
OUT="${1:-dist}"
mkdir -p "$OUT"
VERSION=$(python3 -c "import json; d=json.load(open('VERSION')); print(f\"v{d['version']}.{d['subversion']}\")")
WORK=$(mktemp -d)
STAGE="$WORK/tadamoo-${VERSION}"
mkdir -p "$STAGE"

echo "== staging tracked files (release: ${VERSION}) =="
git ls-files | while read -r f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$f" "$STAGE/$f"
done

echo "== engine binaries =="
mkdir -p "$STAGE/bin"
cp bin/basalt bin/basalt-bench bin/basalt-probe bin/basalt-vision "$STAGE/bin/"
chmod +x "$STAGE/bin/"*

echo "== linux-complete (harness + installer + engine) =="
tar czf "$OUT/tadamoo-linux-complete-${VERSION}.tar.gz" -C "$WORK" "tadamoo-${VERSION}"

echo "== server-linux (harness + installer, no engine) =="
rm -rf "$STAGE/bin"
tar czf "$OUT/tadamoo-server-linux-${VERSION}.tar.gz" -C "$WORK" "tadamoo-${VERSION}"

rm -rf "$WORK"
echo "== done =="
ls -la "$OUT"/tadamoo-*-"${VERSION}".tar.gz
sha256sum "$OUT"/tadamoo-*-"${VERSION}".tar.gz
