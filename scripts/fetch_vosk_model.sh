#!/usr/bin/env bash
# Fetch the Vosk small-EN model (~40 MB) into gitignored models/ for the
# voice "extension" listener (docs/hardware.md Part D).
# Idempotent: skips the download when the model directory already exists.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
model="vosk-model-small-en-us-0.15"
dest="$repo/models/$model"

if [ -d "$dest" ]; then
    echo "already present: $dest"
else
    mkdir -p "$repo/models"
    echo "downloading $model ..."
    curl -fL --progress-bar -o "$dest.zip" "https://alphacephei.com/vosk/models/$model.zip"
    unzip -q "$dest.zip" -d "$repo/models"
    rm "$dest.zip"
    echo "installed: $dest"
fi

# Endpointing: decide 0.2 s into the silence after a word, not the shipped
# 0.5/0.75/1.0 s. Measured 2026-11-16 (#58) on a synthesized "extension"
# with 100 ms chunks: final 935 ms after the word ends as shipped, 435 ms
# at 0.2 s; 0.15 s gained nothing. Re-applied on every run (idempotent).
sed -i.orig -E 's/^(--endpoint\.rule[234]\.min-trailing-silence=).*/\10.2/' \
    "$dest/conf/model.conf"
rm -f "$dest/conf/model.conf.orig"
echo "endpointing: $(grep -c 'min-trailing-silence=0.2' "$dest/conf/model.conf") rules at 0.2 s"
