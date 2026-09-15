#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"

xcrun swiftc -O \
  -framework ScreenCaptureKit \
  -framework CoreMedia \
  -framework CoreVideo \
  -framework CoreGraphics \
  "$SCRIPT_DIR/macos_capture_bridge.swift" \
  -o "$SCRIPT_DIR/macos_capture_bridge"

xcrun swiftc -O \
  -framework AppKit \
  -framework ApplicationServices \
  -framework CoreGraphics \
  "$SCRIPT_DIR/macos_input_bridge.swift" \
  -o "$SCRIPT_DIR/macos_input_bridge"

xcrun swiftc -O \
  -framework ApplicationServices \
  -framework CoreGraphics \
  "$SCRIPT_DIR/macos_hotkey_bridge.swift" \
  -o "$SCRIPT_DIR/macos_hotkey_bridge"

xcrun clang -O2 -fobjc-arc \
  -framework AppKit \
  "$SCRIPT_DIR/macos_overlay_bridge.m" \
  -o "$SCRIPT_DIR/macos_overlay_bridge"

# A stable development signature keeps macOS privacy grants attached across
# local rebuilds. Fall back to ad-hoc signing on machines without an identity.
SIGN_IDENTITY="${EDAP_CODESIGN_IDENTITY:-}"
if [[ -z "$SIGN_IDENTITY" ]]; then
  SIGN_IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/Apple Development:/ { print $2; exit }')
fi
[[ -z "$SIGN_IDENTITY" ]] && SIGN_IDENTITY="-"

codesign --force --sign "$SIGN_IDENTITY" --identifier com.weldawadyathink.edapgui.capture \
  "$SCRIPT_DIR/macos_capture_bridge"
codesign --force --sign "$SIGN_IDENTITY" --identifier com.weldawadyathink.edapgui.input \
  "$SCRIPT_DIR/macos_input_bridge"
codesign --force --sign "$SIGN_IDENTITY" --identifier com.weldawadyathink.edapgui.hotkeys \
  "$SCRIPT_DIR/macos_hotkey_bridge"
codesign --force --sign "$SIGN_IDENTITY" --identifier com.weldawadyathink.edapgui.overlay \
  "$SCRIPT_DIR/macos_overlay_bridge"

print -r -- "Built native bridges in $SCRIPT_DIR"
