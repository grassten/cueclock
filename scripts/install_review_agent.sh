#!/usr/bin/env bash
# Install (or reinstall) the session review page (#57, port 8001) as an always-on launchd agent.
# Run it from the main checkout (it pins the agent to this script's repo).
# Idempotent: re-run after moving the repo or editing the plist template.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
plist="$HOME/Library/LaunchAgents/com.cueclock.review.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" \
    "$repo/scripts/com.cueclock.review.plist.template" > "$plist"

launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "com.cueclock.review installed + started (log: ~/Library/Logs/cueclock-review.log)"
