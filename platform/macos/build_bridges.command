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

xcrun clang -O2 -fobjc-arc \
  -framework AppKit \
  "$SCRIPT_DIR/macos_overlay_bridge.m" \
  -o "$SCRIPT_DIR/macos_overlay_bridge"

print -r -- "Built native bridges in $SCRIPT_DIR"
