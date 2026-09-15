#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
EDAP_DIR="${SCRIPT_DIR:h:h}"
PYTHON_BIN="${EDAP_NATIVE_PYTHON:-/opt/homebrew/bin/python3.12}"
VENV_DIR="${EDAP_NATIVE_VENV:-$EDAP_DIR/.venv-macos}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  print -u2 -- "Python 3.12 is required. Install it with: brew install python@3.12 python-tk@3.12"
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import tkinter' >/dev/null 2>&1; then
  print -u2 -- "Tk for Python 3.12 is required. Install it with: brew install python-tk@3.12"
  exit 1
fi

"$SCRIPT_DIR/build_bridges.command"
"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel setuptools
"$VENV_DIR/bin/python" -m pip install -r "$EDAP_DIR/requirements-macos.txt"
if ! "$VENV_DIR/bin/python" "$SCRIPT_DIR/prepare_coreml.py"; then
  print -u2 -- "Core ML conversion was unavailable; EDAP will use its fast CPU fallback."
fi

print -r -- "Native EDAP environment is ready at $VENV_DIR"
