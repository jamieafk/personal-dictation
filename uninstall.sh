#!/bin/bash
# Remove Personal Dictation from login items and stop the running instance.
set -e

PLIST_NAME="com.personal.dictation"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm "$PLIST_PATH"
    echo "Uninstalled. Personal Dictation will no longer auto-launch."
else
    echo "Not installed."
fi

# Stop any running instance
pkill -f "Personal Dictation" 2>/dev/null || true
