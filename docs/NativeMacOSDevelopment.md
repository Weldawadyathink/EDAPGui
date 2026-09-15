# Native macOS development handoff

This document is the maintainer map for the `native-macos` branch. User-facing
setup and launch instructions remain in [MoltenVR.md](MoltenVR.md).

## Goal and boundary

Elite Dangerous continues to run inside the MoltenVR Wine bottle. EDAPGui runs
natively on macOS and treats Elite as an external application: it reads Wine's
Elite data files, observes the Elite window, and posts only the input described
by the active Elite bindings. It does not modify Elite's process or memory.

Native hosting was selected because Tk, global hotkeys, screen capture, process
lifetime, and macOS privacy permissions are substantially more predictable
outside Wine. The existing Python navigation and vision implementation remains
shared with Windows; this is a compatibility layer, not a ground-up rewrite.

Do not launch or drive Elite as part of autonomous testing. Integration tests
that send real flight input must be done with the user present.

## Runtime architecture

The entry point is `platform/macos/launch_native.command`. The copy at
`/Applications/EDAPGui.app` is a small LaunchServices/Raycast wrapper that
resolves and executes this checkout's launcher, so source edits are the live
application after any native helper rebuild.

The launcher owns one process tree:

```text
launch_native.command
├── macos_capture_bridge  -> .native-runtime/frame.raw
├── macos_overlay_bridge  <- .native-runtime/overlay.json
└── native Python/Tk EDAPGui
    ├── macos_input_bridge   (lazy, pipe-owned)
    └── macos_hotkey_bridge  (pipe-owned)
```

The launcher holds an OS file lock for a single instance; the lock directory
is diagnostic state only. The lock file is retained to preserve inode identity. If capture
fails, Elite closes, Python exits, or the launcher receives a termination
signal, it stops the rest of the owned tree. Termination is bounded and may
escalate from TERM to KILL during cleanup. This is deterministic lifecycle
ownership, not a CPU-usage watchdog.

### Capture

`macos_capture_bridge.swift` uses ScreenCaptureKit. At startup it finds the
visible Elite window, filters capture to the owning Wine application, and crops
to Elite's frame. Application filtering deliberately survives Wine replacing
the native window object. A process source and a short-grace window monitor end
the runtime when Elite disappears, including the case where Wine itself stays
alive. Switching Spaces or minimizing does not count as closing the window. Moving or
resizing the visible window stops capture with a relaunch message because the
application-filter stream has a fixed crop. Capture and overlay also monitor
the launcher PID so a killed launcher cannot leave them running.

Frames are BGRA data in a memory-mapped file. A small header contains a
monotonic sequence and dimensions. `Screen.py` rejects a sequence that has not
advanced within the stale-frame timeout, preventing control decisions from an
old screenshot.

### Input and hotkeys

`macos_input_bridge.swift` maps the DirectInput/Set-1 scan codes read from the
user's active Elite `.binds` file to macOS virtual hardware key codes. Events
are posted to the PID that owns the Elite window, so they do not depend on
typing into the frontmost macOS application. Modifier flags, including
Alt/Option, are explicit; the user's known-working binding chords remain the
source of truth.

The helper tracks only successfully posted key-down events. Stop and shutdown
use one bounded `releaseAll` request; cleanup never starts a new helper just
to release keys after Elite is gone. Failed chords attempt a complete key-up
before propagating the error. Chords and text entry are serialized across
workers. Releases target the original key-down PID, including when its window
is hidden; EOF releases held keys. `MacOSBridge.py` uses nonblocking pipes with
a request deadline and forcibly reaps a helper that will not quit.

`macos_hotkey_bridge.swift` uses a Core Graphics event tap for configured global
start/stop hotkeys. The End hotkey sets a cooperative stop event, releases held
input, and discards commands that have not started. A later explicit action
begins a new command generation. `CooperativeStop.StopEvent` keeps the original
event bound to existing workers so restarting cannot revive stopped work.

### Tk and background work

Tk widgets are touched only on the main thread. Worker and hotkey callbacks go
through `APGui._ui_queue`, which Tk drains in bounded batches. Blocking button
actions run as daemon workers. Identical throttle commands coalesce: while one
is in flight, the latest requested setting replaces earlier pending settings
and runs when possible. Completion messages carry the exact worker identity to
avoid an old completion removing a newer task.

Snapshot JSON reads have bounded retries and cache the pre-read modification
time; status waits include these retries in their overall deadline.

Long assist waits use `stop_event.wait(...)` or explicit stop checks rather than
uninterruptible sleeps. Keep new control loops cooperative and avoid adding
unbounded work to Tk callbacks.

### Overlay and machine learning

`macos_overlay_bridge.m` is a transparent, click-through AppKit panel fed by
JSON state. Capture-pixel coordinates are scaled to macOS window points, which
matters on Retina displays.

Setup converts supported YOLO models to Core ML. The default
`EDAP_ML_DEVICE=ane` selects Core ML `CPU_AND_NE`, excluding the GPU so Elite can
retain it. Conversion/load failures fall back to bounded-thread PyTorch CPU
inference. PaddleOCR stays on CPU and is loaded lazily. Do not make GPU or model
pipeline changes without measurements from an actual reported bottleneck.

## Source and generated files

Key native files:

- `PlatformPaths.py`: native paths into the MoltenVR prefix.
- `Screen.py`: capture-file consumer and Windows/native selection.
- `MacOSBridge.py`, `directinput.py`, `GlobalHotkeys.py`: Python bridge clients.
- `Overlay.py`: JSON overlay producer.
- `platform/macos/*.swift`, `*.m`: native helper sources.
- `platform/macos/build_bridges.command`: build and stable-sign helpers.
- `platform/macos/setup_native.command`: create the Python 3.12 environment,
  install dependencies, build helpers, and prepare Core ML models.
- `platform/macos/install_app.command`: install/register the Raycast app wrapper.
- `tests_native/test_native_runtime.py`: no-Elite regression tests.

Compiled helpers, `.venv-macos`, `.native-runtime`, Core ML output, logs, caches,
and local/user configuration are intentionally ignored by Git.

## Build and verify without Elite

From the repository root:

```zsh
./platform/macos/build_bridges.command
zsh -n platform/macos/launch_native.command
git ls-files -z '*.py' | xargs -0 .venv-macos/bin/python -m py_compile
.venv-macos/bin/python -m unittest discover -s tests_native -v
git diff --check
```

Useful no-Elite lifecycle check:

```zsh
EDAP_SUPPRESS_LAUNCH_ALERT=1 ./platform/macos/launch_native.command
```

It should exit nonzero promptly, remove `.native-runtime/launcher.lock`, and
leave no EDAP or native helper processes. A headless Tk construction/destruction
test is also safe; see the repository history and native tests for the pattern.

After helper changes, `build_bridges.command` uses the available Apple
Development identity. Stable helper identities preserve Screen Recording,
Accessibility, and Input Monitoring grants across rebuilds. The shell-wrapper
app remains ad-hoc signed because development-signing that script bundle caused
Gatekeeper to reject launches silently on this machine.

## Manual integration checklist

Only perform this with the user and Elite running:

1. Launch `EDAPGui` through Raycast or `/Applications/EDAPGui.app`.
2. Confirm `edapgui-native.log` reports capture of
   `Elite - Dangerous (CLIENT)` owned by Wine.
3. Confirm the frame sequence advances and the overlay aligns with the Elite
   window at Retina scale.
4. Exercise a harmless, user-approved input such as an existing throttle bind;
   verify repeated clicks send the latest requested setting.
5. Hold or start a cooperative action and use End; verify input releases and the
   UI remains responsive.
6. Close Elite; EDAPGui and all helpers should exit within a few seconds.

If macOS denies a helper, inspect System Settings > Privacy & Security for
Screen & System Audio Recording, Accessibility, and Input Monitoring. Logs are
`autopilot.log` for the Python app and `edapgui-native.log` for the launcher and
helpers.

## Source control

- Public fork: `https://github.com/Weldawadyathink/EDAPGui`
- Development branch and fork default: `native-macos`
- `origin`: the public fork
- `upstream`: `https://github.com/SumZer0-git/EDAPGui`

Keep commits focused and checkpoint working reliability improvements. Preserve
the native compatibility layer when incorporating upstream changes, and do not
commit user configurations, Wine bottle contents, generated models, binaries,
or runtime logs.

## Robustness review

See [NativeMacOSReview.md](NativeMacOSReview.md) for the September 2026 findings,
verification scope, and additional supervised integration checks.
