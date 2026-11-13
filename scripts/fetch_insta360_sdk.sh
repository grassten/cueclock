#!/usr/bin/env bash
# Fetch Insta360's Link SDK (macOS) into gitignored vendor.nosync/ and build
# tools/insta360/link_settings.cc against it, for the side-angle camera's
# settings (docs/hardware.md Part F, `cueclock sidecam`).
#
# The SDK publishes no license, so it is never committed here: this script
# downloads the exact files cueclock was built against, pinned to a commit and
# checked by SHA-256, and fails loudly if any of them changed.
# `.nosync` keeps iCloud from evicting the library out from under the deck
# daemon (CLAUDE.md "Dev workflow").
# Idempotent: re-running rebuilds the helper and skips files already present.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
dest="$repo/vendor.nosync/insta360-link-sdk"
commit="2dd2c78186b0335a530042ae49accbef249d6a95"  # Insta360Develop/Link-SDK, 2026-09-08
base="https://raw.githubusercontent.com/Insta360Develop/Link-SDK/$commit/UVCCamera_mac"

files=(
    "bin/libUVCCamera.dylib 8da0c2fedf90145d1635178afa7663f3cbdbf3726042fe1be39284655555219e"
    "include/checksum.h 998b27f26abb6002696514ee05e27297e405585fa4ae6cd5fee3737dad90d11a"
    "include/uvc_camera.h b0630e9f3ec5e60329a63be0761d62a193b694769f40d7cc44f20425ec466a3b"
    "include/uvc_common.h cc4b11374676b37a75ed5f0f4a095191b1e5c11dd7dc537a0129aab982006848"
)

mkdir -p "$dest/bin" "$dest/include"
for entry in "${files[@]}"; do
    path="${entry% *}"
    sum="${entry#* }"
    if [ ! -f "$dest/$path" ]; then
        echo "downloading $path ..."
        curl -fsSL -o "$dest/$path.part" "$base/$path"
        mv "$dest/$path.part" "$dest/$path"
    fi
    echo "$sum  $dest/$path" | shasum -a 256 -c --quiet - || {
        echo "checksum mismatch: $path — refusing to build against an unpinned SDK" >&2
        rm -f "$dest/$path"
        exit 1
    }
done

# The dylib is built for macOS 26.2, so the helper targets the same.
clang++ -std=c++17 -O2 -Wall -Wextra -mmacosx-version-min=26.2 \
    -I"$dest/include" "$repo/tools/insta360/link_settings.cc" \
    -L"$dest/bin" -lUVCCamera -Wl,-rpath,@executable_path \
    -o "$dest/bin/link-settings"
echo "built: $dest/bin/link-settings"
