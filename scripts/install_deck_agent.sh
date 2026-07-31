#!/usr/bin/env bash
# Install (or reinstall) the Stream Deck daemon as an always-on launchd agent.
# Idempotent: re-run after moving the repo or editing the plist template.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
plist="$HOME/Library/LaunchAgents/com.cueclock.deck.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" \
    "$repo/scripts/com.cueclock.deck.plist.template" > "$plist"

launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "com.cueclock.deck installed + started (log: ~/Library/Logs/cueclock-deck.log)"
