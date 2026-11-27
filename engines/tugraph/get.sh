#!/usr/bin/env bash
# get.sh: fetch TuGraph v4.5.2 as its authors released it (the el8 package of the GitHub release; its source is the
# tag v4.5.2, commit 4a1c8fb69b7879396f36e491b125d297c7ca79dd) and unpack it into tugraph-4.5.2/. Nothing is built
# from source and nothing in it is changed: our counters are a separate library (build.sh). 47 MB, seconds.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ../check-tools.sh
check_tools tugraph build
VERSION=4.5.2
PKG="tugraph-${VERSION}-1.el8.x86_64.rpm"
SHA256=70a27831a37e2785f9b6918bbd75040867ac7c96f369471fbef137f10729e03e
DEST="tugraph-${VERSION}"
if [[ -x "$DEST/usr/local/bin/lgraph_server" ]]; then echo "get.sh: $DEST is already here"; exit 0; fi
mkdir -p "$DEST"
curl -fsSL --retry 3 -o "$DEST/$PKG" "https://github.com/TuGraph-family/tugraph-db/releases/download/v${VERSION}/${PKG}"
echo "${SHA256}  $DEST/$PKG" | sha256sum -c -
(cd "$DEST" && rpm2cpio "$PKG" | cpio -idm --quiet)
[[ -x "$DEST/usr/local/bin/lgraph_server" && -x "$DEST/usr/local/bin/lgraph_import" ]] \
  || { echo "get.sh: the package holds no lgraph_server / lgraph_import" >&2; exit 1; }
echo "get.sh: TuGraph ${VERSION} unpacked in engines/tugraph/$DEST"
