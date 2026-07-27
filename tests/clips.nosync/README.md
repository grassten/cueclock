# Test clips (local-only)

Real Basler table footage used by the clip-replay test suite. **Clips are gitignored and
never published** — this directory is empty in fresh clones, and clip-based tests skip
cleanly when fixtures are absent.

The directory's real name is `clips.nosync` (the suffix excludes it from iCloud Drive
sync — Steven's Desktop is iCloud-synced, and iCloud otherwise retry-loops uploading
tens of GB of raw footage); code and docs use the `tests/clips` symlink.

The recording shopping list (labeled scenarios to capture once the camera is mounted) is
in `docs/cv-app-rewrite.md` §2.3.
