#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SOURCE_APP="$SCRIPT_DIR/EDAPGui.app"
DESTINATION_DIR="${1:-$HOME/Applications}"
DESTINATION_APP="$DESTINATION_DIR/EDAPGui.app"

mkdir -p "$DESTINATION_DIR"
ditto "$SOURCE_APP" "$DESTINATION_APP"
chmod +x "$DESTINATION_APP/Contents/MacOS/EDAPGui"
# The bundle executable is a local shell wrapper, so keep its signature ad-hoc.
# Permission-sensitive native helpers are stably signed by build_bridges.command.
codesign --force --deep --sign - --identifier com.weldawadyathink.edapgui "$DESTINATION_APP"
xattr -d com.apple.quarantine "$DESTINATION_APP" >/dev/null 2>&1 || true
xattr -d com.apple.provenance "$DESTINATION_APP" >/dev/null 2>&1 || true

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
  "$LSREGISTER" -f "$DESTINATION_APP"
fi
mdimport "$DESTINATION_APP" >/dev/null 2>&1 || true

print -r -- "Installed $DESTINATION_APP"
print -r -- "Open Raycast and type EDAPGui to launch it."
