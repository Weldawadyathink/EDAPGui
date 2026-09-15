#!/bin/zsh

set -euo pipefail

# The source checkout may live anywhere macOS can access through Wine's Z:
# mapping. The Python environment remains in the MoltenVR bottle by default.
SCRIPT_DIR="${0:A:h}"
EDAP_DIR="${SCRIPT_DIR:h:h}"
PREFIX_DIR="${MOLTENVR_PREFIX:-$HOME/Library/Application Support/MoltenVR/Bottles/MoltenVR}"
MOLTEN_DIR="${PREFIX_DIR:h:h}"
WINE_BIN="${MOLTENVR_WINE:-$MOLTEN_DIR/wine/Libraries/Wine/bin/wine}"
WINEPATH_BIN="${WINE_BIN:h}/winepath"
PYTHON_EXE="${EDAP_PYTHON:-$PREFIX_DIR/drive_c/EDAPGui/venv/Scripts/python.exe}"
CAPTURE_BIN="$SCRIPT_DIR/macos_capture_bridge"
OVERLAY_BIN="$SCRIPT_DIR/macos_overlay_bridge"
RUNTIME_DIR="$EDAP_DIR/.wine-capture"
CAPTURE_FILE="$RUNTIME_DIR/frame.raw"
OVERLAY_FILE="$RUNTIME_DIR/overlay.json"
LOG_FILE="$EDAP_DIR/edapgui-launch.log"
CAPTURE_WIDTH="${EDAP_CAPTURE_WIDTH:-2560}"
CAPTURE_HEIGHT="${EDAP_CAPTURE_HEIGHT:-1440}"
CAPTURE_FPS="${EDAP_CAPTURE_FPS:-12}"

# EDAP installs global keyboard hooks, so a second instance is unsafe.
if pgrep -f '[p]ythonw?\.exe.*EDAPGui\.py' >/dev/null 2>&1; then
  print -r -- "EDAPGui is already running." >>"$LOG_FILE"
  exit 0
fi

for required in "$WINE_BIN" "$WINEPATH_BIN" "$PYTHON_EXE" "$CAPTURE_BIN" "$OVERLAY_BIN"; do
  if [[ ! -x "$required" ]]; then
    print -r -- "Missing executable: $required" >>"$LOG_FILE"
    exit 1
  fi
done

EDAP_WIN_DIR=$(env WINEPREFIX="$PREFIX_DIR" "$WINEPATH_BIN" -w "$EDAP_DIR" | tr -d '\r')
PYTHON_WIN=$(env WINEPREFIX="$PREFIX_DIR" "$WINEPATH_BIN" -w "$PYTHON_EXE" | tr -d '\r')
CAPTURE_WIN="$EDAP_WIN_DIR\\.wine-capture\\frame.raw"
OVERLAY_WIN="$EDAP_WIN_DIR\\.wine-capture\\overlay.json"

mkdir -p "$RUNTIME_DIR"

# Remove helpers orphaned by a forced prior shutdown. Patterns include the
# checkout-specific state files, avoiding unrelated capture processes.
pkill -TERM -f "[m]acos_capture_bridge.*${CAPTURE_FILE}" >/dev/null 2>&1 || true
pkill -TERM -f "[m]acos_overlay_bridge.*${OVERLAY_FILE}" >/dev/null 2>&1 || true
rm -f "$CAPTURE_FILE" "$OVERLAY_FILE" "$OVERLAY_FILE.tmp"

nohup nice -n 10 /bin/zsh -c '
  capture_bin="$1"
  capture_file="$2"
  overlay_bin="$3"
  overlay_file="$4"
  prefix="$5"
  wine="$6"
  width="$7"
  height="$8"
  fps="$9"
  source_win="${10}"
  python_win="${11}"
  capture_win="${12}"
  overlay_win="${13}"

  "$overlay_bin" "$overlay_file" &
  overlay_pid=$!
  "$capture_bin" --output "$capture_file" --width "$width" --height "$height" \
    --fps "$fps" --exclude-pid "$overlay_pid" &
  capture_pid=$!

  cleanup() {
    kill "$capture_pid" "$overlay_pid" >/dev/null 2>&1 || true
    wait "$capture_pid" "$overlay_pid" 2>/dev/null || true
  }
  trap cleanup EXIT HUP INT TERM

  capture_ready=0
  for _ in {1..50}; do
    if ! kill -0 "$capture_pid" >/dev/null 2>&1; then
      wait "$capture_pid"
      exit $?
    fi
    sequence=$(od -An -tu8 -N8 "$capture_file" 2>/dev/null | tr -d " ")
    if [[ -n "$sequence" && "$sequence" -gt 0 ]]; then
      capture_ready=1
      break
    fi
    sleep 0.1
  done
  if [[ "$capture_ready" -ne 1 ]]; then
    print -u2 -- "macos_capture_bridge: timed out waiting for the first frame"
    exit 1
  fi

  cd "${capture_file:h:h}"
  env \
    WINEPREFIX="$prefix" \
    WINEDEBUG=-all \
    MVK_CONFIG_LOG_LEVEL=0 \
    YOLO_CONFIG_DIR="$source_win\\configs" \
    EDAP_CAPTURE_FILE="$capture_win" \
    EDAP_NATIVE_OVERLAY_FILE="$overlay_win" \
    EDAP_GUI_X="${EDAP_GUI_X:--1650}" \
    EDAP_GUI_Y="${EDAP_GUI_Y:-25}" \
    EDAP_TORCH_THREADS="${EDAP_TORCH_THREADS:-4}" \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    FLAGS_use_mkldnn=0 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=2 \
    "$wine" "$python_win" "$source_win\\EDAPGui.py"
' edap-supervisor \
  "$CAPTURE_BIN" "$CAPTURE_FILE" "$OVERLAY_BIN" "$OVERLAY_FILE" \
  "$PREFIX_DIR" "$WINE_BIN" "$CAPTURE_WIDTH" "$CAPTURE_HEIGHT" "$CAPTURE_FPS" \
  "$EDAP_WIN_DIR" "$PYTHON_WIN" "$CAPTURE_WIN" "$OVERLAY_WIN" \
  >>"$LOG_FILE" 2>&1 &

print -r -- "EDAPGui launch started; see $LOG_FILE"
