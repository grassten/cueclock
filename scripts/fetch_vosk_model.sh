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
    exit 0
fi

mkdir -p "$repo/models"
echo "downloading $model ..."
curl -fL --progress-bar -o "$dest.zip" "https://alphacephei.com/vosk/models/$model.zip"
unzip -q "$dest.zip" -d "$repo/models"
rm "$dest.zip"
echo "installed: $dest"
