# EDAPGui agent handoff

This checkout is the active source tree for Spenser's MoltenVR installation:

`/Users/Spenser/Library/Application Support/MoltenVR/Source/EDAPGui`

Read [docs/NativeMacOSDevelopment.md](docs/NativeMacOSDevelopment.md) before
changing the native macOS path. It records the architecture, design decisions,
build/test commands, runtime ownership rules, and manual integration checklist.

## Working rules

- Elite Dangerous runs in MoltenVR/Wine; EDAPGui itself runs as a native macOS
  Python/Tk application. Do not move Python control back into Wine unless the
  user explicitly asks to abandon the native design.
- Never launch or control Elite Dangerous unattended. Code, helper, lifecycle,
  and no-Elite tests are safe to run autonomously. Coordinate any live flight or
  input test with the user.
- Preserve user-owned files under `configs/`, Elite `.binds` files, and MoltenVR
  bottle data. Generated runtime files and local configuration are gitignored.
- Keep Tk calls on the main thread. Blocking vision, network, calibration, and
  input work belongs in cooperative background work that honors `stop_event`.
- Preserve launcher ownership of child processes. Prefer bounded cleanup and
  EOF/parent lifetime to CPU watchdogs or detached processes.
- Elite bindings remain the source of truth. Native input translates their PC
  DirectInput scan codes and posts directly to the Wine-owned Elite process.
  Do not invent a second macOS keymap or replace working modifier chords.
- Favor small reliability fixes with regression tests over broad rewrites or
  speculative performance work.

## Required checks

From this directory, run before committing native changes:

```zsh
./platform/macos/build_bridges.command
zsh -n platform/macos/launch_native.command
git ls-files -z '*.py' | xargs -0 .venv-macos/bin/python -m py_compile
.venv-macos/bin/python -m unittest discover -s tests_native -v
git diff --check
```

The public fork is `Weldawadyathink/EDAPGui`; active development is on branch
`native-macos`. `origin` is the fork and `upstream` is the original project.
