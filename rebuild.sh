#!/bin/bash
# Rebuild the .app (after dependency changes or moving the folder), reset the
# privacy grants the rebuild invalidates (new ad-hoc signature), and relaunch.
# Afterwards re-grant Accessibility + Input Monitoring when prompted.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_ID="com.personal.dictation"
LABEL="com.personal.dictation"

source "$PROJECT_DIR/.venv/bin/activate"
(cd "$PROJECT_DIR" && python setup.py py2app -A >/dev/null)
echo "Rebuilt dist/Personal Dictation.app"

# Quit the running copy; launchd's `open -W` wrapper exits with it.
pkill -x "Personal Dictation" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -x "Personal Dictation" >/dev/null || break
    sleep 0.5
done

for service in ListenEvent PostEvent Accessibility; do
    tccutil reset "$service" "$BUNDLE_ID" >/dev/null
done
echo "Reset Input Monitoring, Accessibility and PostEvent grants"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart "gui/$(id -u)/$LABEL"
else
    open "$PROJECT_DIR/dist/Personal Dictation.app"
fi
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

echo ""
echo "Now: turn on Personal Dictation under Accessibility, then under Input Monitoring."
echo "The menubar shows ⚠ until both are granted, then switches back to the mic by itself."
