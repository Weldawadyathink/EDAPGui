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
LOCK_DIR="$RUNTIME_DIR/launcher.lock"
LOG_FILE="$EDAP_DIR/edapgui-native.log"
CAPTURE_WIDTH="${EDAP_CAPTURE_WIDTH:-2560}"
CAPTURE_HEIGHT="${EDAP_CAPTURE_HEIGHT:-1440}"
CAPTURE_FPS="${EDAP_CAPTURE_FPS:-15}"
ELITE_TITLE="${EDAP_ELITE_WINDOW_TITLE:-Elite - Dangerous (CLIENT)}"

show_launch_failure() {
  local message="$1"
  print -u2 -r -- "$message"
  if [[ "${EDAP_SUPPRESS_LAUNCH_ALERT:-0}" != "1" ]]; then
    /usr/bin/osascript \
      -e 'on run argv' \
      -e 'display alert "EDAPGui could not be launched" message (item 1 of argv) as critical' \
      -e 'end run' \
      "$message" >/dev/null 2>&1 || true
  fi
}

mkdir -p "$RUNTIME_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  [[ -f "$LOCK_DIR/pid" ]] && read -r lock_pid <"$LOCK_DIR/pid"
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    print -r -- "EDAPGui is already running (launcher PID $lock_pid)." >>"$LOG_FILE"
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR"
fi
print -r -- "$$" >"$LOCK_DIR/pid"

capture_pid=""
overlay_pid=""
cleanup() {
  [[ -n "$capture_pid" ]] && kill "$capture_pid" >/dev/null 2>&1 || true
  [[ -n "$overlay_pid" ]] && kill "$overlay_pid" >/dev/null 2>&1 || true
  [[ -n "$capture_pid" ]] && wait "$capture_pid" 2>/dev/null || true
  [[ -n "$overlay_pid" ]] && wait "$overlay_pid" 2>/dev/null || true
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for required in "$PYTHON_BIN" "$CAPTURE_BIN" "$OVERLAY_BIN" "$INPUT_BIN" "$HOTKEY_BIN"; do
  if [[ ! -x "$required" ]]; then
    print -u2 -- "Missing executable: $required"
    print -u2 -- "Run $SCRIPT_DIR/setup_native.command first."
    exit 1
  fi
done

pkill -TERM -f "[m]acos_capture_bridge.*${CAPTURE_FILE}" >/dev/null 2>&1 || true
pkill -TERM -f "[m]acos_overlay_bridge.*${OVERLAY_FILE}" >/dev/null 2>&1 || true
rm -f "$CAPTURE_FILE" "$OVERLAY_FILE" "$OVERLAY_FILE.tmp"

"$OVERLAY_BIN" "$OVERLAY_FILE" >>"$LOG_FILE" 2>&1 &
overlay_pid=$!
"$CAPTURE_BIN" --output "$CAPTURE_FILE" --width "$CAPTURE_WIDTH" \
  --height "$CAPTURE_HEIGHT" --fps "$CAPTURE_FPS" --window-title "$ELITE_TITLE" \
  >>"$LOG_FILE" 2>&1 &
capture_pid=$!

capture_ready=0
for _ in {1..80}; do
  if ! kill -0 "$capture_pid" >/dev/null 2>&1; then
    set +e
    wait "$capture_pid"
    capture_status=$?
    set -e
    capture_error=$(tail -n 1 "$LOG_FILE" 2>/dev/null || true)
    [[ -z "$capture_error" ]] && capture_error="The native capture helper exited before producing a frame. See $LOG_FILE"
    show_launch_failure "$capture_error"
    exit "$capture_status"
  fi
  sequence=$(od -An -tu8 -N8 "$CAPTURE_FILE" 2>/dev/null | tr -d ' ' || true)
  if [[ -n "$sequence" && "$sequence" -gt 0 ]]; then
    capture_ready=1
    break
  fi
  sleep 0.1
done
if [[ "$capture_ready" -ne 1 ]]; then
  show_launch_failure "Timed out waiting for the first Elite window frame. Make sure Elite is visible, then try again. See $LOG_FILE"
  exit 1
fi

cd "$EDAP_DIR"
env \
  MOLTENVR_PREFIX="$PREFIX_DIR" \
  EDAP_CAPTURE_FILE="$CAPTURE_FILE" \
  EDAP_CAPTURE_WIDTH="$CAPTURE_WIDTH" \
  EDAP_CAPTURE_HEIGHT="$CAPTURE_HEIGHT" \
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
