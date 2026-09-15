# Native macOS host for MoltenVR

This fork runs EDAPGui's Python application natively on macOS while Elite
Dangerous continues to run in the MoltenVR Wine bottle. The Python control and
vision code remains shared with Windows.

## Architecture

- `macos_capture_bridge` captures only the visible Elite window with
  ScreenCaptureKit and publishes the latest BGRA frame through a memory-mapped
  file. EDAP does not capture its own UI or the rest of the desktop.
- `macos_input_bridge` converts the DirectInput scan codes from the active
  Elite `.binds` file to macOS hardware key codes, then posts each event to the
  process that owns the Elite window. Alt/Option is handled as an explicit
  modifier flag.
- `macos_hotkey_bridge` watches the configured start/stop hotkeys in a native
  Core Graphics event tap. This avoids mixing macOS text-input APIs with Tk's
  main thread.
- `macos_overlay_bridge` renders EDAP's JSON overlay in a transparent,
  click-through AppKit panel.
- Journal, status, graphics, player, and bindings files are read directly from
  the MoltenVR Wine prefix.

The shipped YOLO models are converted to Core ML during setup. Runtime is
restricted to `CPU_AND_NE`, which allows the CPU and Neural Engine but excludes
the GPU. If conversion or loading fails, EDAP automatically falls back to
PyTorch on the CPU. PaddleOCR remains on the CPU.

## Setup

Install Homebrew Python and Tk if needed, then build the native environment:

```zsh
brew install python@3.12 python-tk@3.12
./platform/macos/setup_native.command
```

Start Elite in Borderless mode before launching EDAP:

```zsh
./platform/macos/launch_native.command
```

The first launch may request Screen Recording, Accessibility, or Input
Monitoring permission for the launching application. Relaunch EDAP after
granting a new permission.

The launcher defaults to a 2560x1440 Elite frame at 15 FPS. Overrides include
`MOLTENVR_PREFIX`, `EDAP_CAPTURE_WIDTH`, `EDAP_CAPTURE_HEIGHT`,
`EDAP_CAPTURE_FPS`, `EDAP_GUI_X`, `EDAP_GUI_Y`, `EDAP_TORCH_THREADS`, and
`EDAP_ELITE_WINDOW_TITLE`.

Set `EDAP_ML_DEVICE=cpu` to force CPU inference or `EDAP_ML_DEVICE=mps` to use
Metal. The default is `ane`, with automatic CPU fallback.

## Runtime safety

The native launcher is the owner of the Python app, capture process, and
overlay process. It uses an atomic single-instance lock and shuts down its
children whenever Python exits or the launcher receives a termination signal.
The input and hotkey helpers also use parent-owned pipes, so they exit on EOF
instead of becoming detached background processes.

This is lifecycle ownership rather than a watchdog: no process periodically
kills EDAP based on CPU usage or timing guesses. On the application side,
assist waits are cooperatively interruptible, held keys are released on stop,
and idle monitor loops are rate limited. A capture frame that has stopped
updating for two seconds is rejected rather than reused for control decisions.
PaddleOCR is loaded only when an OCR operation is first requested.

The application log is `autopilot.log`; native launcher and helper diagnostics
are written to `edapgui-native.log` in the source directory.

## Rollback

The launcher stored in the MoltenVR bottle runs this source checkout's native
launcher. To use the previous Wine-hosted Python process for one launch:

```zsh
EDAP_USE_WINE=1 "/Users/Spenser/Library/Application Support/MoltenVR/Bottles/MoltenVR/drive_c/Launch EDAPGui.command"
```

Runtime frames, generated Core ML models, logs, virtual environments, and
compiled helpers are ignored by Git.
