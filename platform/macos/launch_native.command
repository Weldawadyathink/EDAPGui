#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
EDAP_DIR="${SCRIPT_DIR:h:h}"
PREFIX_DIR="${MOLTENVR_PREFIX:-$HOME/Library/Application Support/MoltenVR/Bottles/MoltenVR}"
VENV_DIR="${EDAP_NATIVE_VENV:-$EDAP_DIR/.venv-macos}"
PYTHON_BIN="$VENV_DIR/bin/python"
CAPTURE_BIN="$SCRIPT_DIR/macos_capture_bridge"
OVERLAY_BIN="$SCRIPT_DIR/macos_overlay_bridge"
INPUT_BIN="$SCRIPT_DIR/macos_input_bridge"
HOTKEY_BIN="$SCRIPT_DIR/macos_hotkey_bridge"
RUNTIME_DIR="$EDAP_DIR/.native-runtime"
CAPTURE_FILE="$RUNTIME_DIR/frame.raw"
OVERLAY_FILE="$RUNTIME_DIR/overlay.json"
LOG_FILE="$EDAP_DIR/edapgui-native.log"
CAPTURE_WIDTH="${EDAP_CAPTURE_WIDTH:-2560}"
CAPTURE_HEIGHT="${EDAP_CAPTURE_HEIGHT:-1440}"
CAPTURE_FPS="${EDAP_CAPTURE_FPS:-15}"
ELITE_TITLE="${EDAP_ELITE_WINDOW_TITLE:-Elite - Dangerous (CLIENT)}"

if pgrep -f '[p]ython.*EDAPGui.py' >/dev/null 2>&1; then
  print -r -- "EDAPGui is already running." >>"$LOG_FILE"
  exit 0
fi

for required in "$PYTHON_BIN" "$CAPTURE_BIN" "$OVERLAY_BIN" "$INPUT_BIN" "$HOTKEY_BIN"; do
  if [[ ! -x "$required" ]]; then
    print -u2 -- "Missing executable: $required"
    print -u2 -- "Run $SCRIPT_DIR/setup_native.command first."
    exit 1
  fi
done

mkdir -p "$RUNTIME_DIR"
pkill -TERM -f "[m]acos_capture_bridge.*${CAPTURE_FILE}" >/dev/null 2>&1 || true
pkill -TERM -f "[m]acos_overlay_bridge.*${OVERLAY_FILE}" >/dev/null 2>&1 || true
rm -f "$CAPTURE_FILE" "$OVERLAY_FILE" "$OVERLAY_FILE.tmp"

"$OVERLAY_BIN" "$OVERLAY_FILE" >>"$LOG_FILE" 2>&1 &
overlay_pid=$!
"$CAPTURE_BIN" --output "$CAPTURE_FILE" --width "$CAPTURE_WIDTH" \
  --height "$CAPTURE_HEIGHT" --fps "$CAPTURE_FPS" --window-title "$ELITE_TITLE" \
  >>"$LOG_FILE" 2>&1 &
capture_pid=$!

cleanup() {
  kill "$capture_pid" "$overlay_pid" >/dev/null 2>&1 || true
  wait "$capture_pid" "$overlay_pid" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

capture_ready=0
for _ in {1..80}; do
  if ! kill -0 "$capture_pid" >/dev/null 2>&1; then
    wait "$capture_pid"
    exit $?
  fi
  sequence=$(od -An -tu8 -N8 "$CAPTURE_FILE" 2>/dev/null | tr -d ' ')
  if [[ -n "$sequence" && "$sequence" -gt 0 ]]; then
    capture_ready=1
    break
  fi
  sleep 0.1
done
if [[ "$capture_ready" -ne 1 ]]; then
  print -u2 -- "Timed out waiting for the first Elite window frame. See $LOG_FILE"
  exit 1
fi

cd "$EDAP_DIR"
env \
  MOLTENVR_PREFIX="$PREFIX_DIR" \
  EDAP_CAPTURE_FILE="$CAPTURE_FILE" \
  EDAP_NATIVE_OVERLAY_FILE="$OVERLAY_FILE" \
  EDAP_MACOS_INPUT_HELPER="$INPUT_BIN" \
  EDAP_MACOS_HOTKEY_HELPER="$HOTKEY_BIN" \
  EDAP_ELITE_WINDOW_TITLE="$ELITE_TITLE" \
  EDAP_GUI_X="${EDAP_GUI_X:-40}" \
  EDAP_GUI_Y="${EDAP_GUI_Y:-40}" \
  EDAP_ML_DEVICE="${EDAP_ML_DEVICE:-ane}" \
  EDAP_TORCH_THREADS="${EDAP_TORCH_THREADS:-6}" \
  OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" \
  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
  "$PYTHON_BIN" "$EDAP_DIR/EDAPGui.py" >>"$LOG_FILE" 2>&1
