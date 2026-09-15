#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
SOURCE_APP="$SCRIPT_DIR/EDAPGui.app"
DESTINATION_DIR="$HOME/Applications"
DESTINATION_APP="$DESTINATION_DIR/EDAPGui.app"

mkdir -p "$DESTINATION_DIR"
ditto "$SOURCE_APP" "$DESTINATION_APP"
chmod +x "$DESTINATION_APP/Contents/MacOS/EDAPGui"
codesign --force --deep --sign - "$DESTINATION_APP"

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
  "$LSREGISTER" -f "$DESTINATION_APP"
fi
mdimport "$DESTINATION_APP" >/dev/null 2>&1 || true

print -r -- "Installed $DESTINATION_APP"
print -r -- "Open Raycast and type EDAPGui to launch it."
