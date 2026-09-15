# MoltenVR on macOS

This fork includes an optional compatibility layer for running EDAPGui in a
MoltenVR Wine bottle. Normal Windows behavior is unchanged unless the bridge
environment variables are set.

## Why native bridges are needed

Wine exposes Elite Dangerous as a Win32 window, but screen grabs may be black
and Win32 color-keyed overlay windows may render as opaque surfaces on macOS.
The compatibility layer therefore keeps EDAPGui and its computer vision inside
Wine while using two small native helpers:

- `macos_capture_bridge` captures the main display with ScreenCaptureKit and
  publishes its latest BGRA frame through a memory-mapped file.
- `macos_overlay_bridge` renders EDAPGui's JSON overlay state in a transparent,
  click-through AppKit panel.

## Build and run

The checkout can live anywhere on macOS; Wine runs it through its `Z:` drive.
By default, the launcher reuses the Windows virtual environment at
`drive_c/EDAPGui/venv` in the MoltenVR bottle. Then run from this checkout:

```zsh
./platform/macos/build_bridges.command
./platform/macos/launch_moltenvr.command
```

The first launch may request macOS Screen Recording permission. Grant it to
MoltenVR (or the process used to launch MoltenVR), then relaunch EDAPGui.

The launcher defaults to a 2560x1440 capture at 12 FPS. Override it when
needed:

```zsh
EDAP_CAPTURE_WIDTH=3440 EDAP_CAPTURE_HEIGHT=1440 \
  ./platform/macos/launch_moltenvr.command
```

Other supported overrides are `MOLTENVR_PREFIX`, `MOLTENVR_WINE`, `EDAP_PYTHON`,
`EDAP_CAPTURE_FPS`, `EDAP_GUI_X`, `EDAP_GUI_Y`, and `EDAP_TORCH_THREADS`.

Runtime frames, overlay state, logs, and compiled bridge executables are
ignored by Git.
