# Native macOS robustness review — 2026-09-15

## Scope and result

Reviewed the native launcher and four helpers, Python bridge clients, capture
consumer, overlay publication, GUI worker dispatch, cooperative cancellation,
key sending, snapshot parsers, inference failure handling, and startup/shutdown.
Also inspected the native setup/build scripts and path resolution. The existing
12 no-Elite regression tests passed before changes.

The changes below address concrete failure paths. This is not certification of
flight behavior: Elite was neither launched nor controlled during this review.
Windows execution, live navigation, actual key delivery, and capture/overlay
alignment still require integration testing.

## Findings addressed

| Area | Failure before the review | Change |
| --- | --- | --- |
| Input transport | `select` followed by `readline` could hang forever on a partial line; writes could block too | Nonblocking byte transport with a shared deadline, bounded response size, and response-shape validation |
| Input lifetime | Shutdown could block flushing a pipe; dead helpers retained pipes; key-up could start a replacement helper | EOF shutdown, bounded TERM/KILL escalation, closed descriptors, and no helper startup for key-up |
| Native key ownership | Cleanup searched for a visible window instead of using the key-down destination; EOF did not release held keys | Retain the destination PID, release to it even when hidden, and clean up on EOF |
| Modifiers and typing | Releasing one modifier side cleared the other; interrupted text could leave Shift held; simultaneous commands could interleave chords | Derive flags from held keys, finally-based text cleanup, serialize complete chords and text |
| Stop/restart | Clearing one shared event could revive stopped workers; assist completion cleared stop; queued starts could survive End | Stable cancellation events per command, inherited by child workers; persistent stop state; invalidate pre-stop queued starts |
| Assist failure | Generic failures after a separate key-down could leave keys held | Always release keys when an assist exits, including generic exceptions |
| Throttle | `repeat` was passed as the positional hold duration | Pass `repeat` by name |
| Launcher | Directory lock could be stolen before its PID was published; capture-exit cleanup had an unbounded Python wait; early capture success could hide launch failure | OS file lock, bounded child cleanup, nonzero status for capture exit without a usable runtime |
| Helper ownership | Capture and overlay could outlive a forcibly killed launcher | Native process-exit monitors; remove broad command-line-based `pkill` cleanup |
| Capture | Incomplete stream frames were accepted; writer lacked the barrier before copying pixels; window movement left a stale crop | Accept complete frames, fence both sides of pixel publication, reject changed window geometry with a relaunch message |
| Capture resources | Invalid dimensions and close/read races could corrupt reads or reopen a closed mapping | Validate file size/dimensions, serialize mapping access and close, latch shutdown |
| Overlay | Concurrent publishers shared an unlocked temp file; a late paint could overwrite quit; vertically arranged displays used the wrong origin | Serialize publication, latch quit, convert coordinates using the primary display origin |
| Hotkeys | Old readers could use new callbacks; removed legacy bindings returned; killed helpers were not reaped; disabled taps stayed disabled | Bind callbacks to helper identity, clear legacy state, reap helpers, re-enable disabled taps; support F2–F12 and common editing keys |
| Data snapshots | Route/cargo/market/carrier/status readers could retry indefinitely or cache a timestamp newer than the data read | Bounded retry, pre-read timestamps, cooperative cancellation for control-path parsers; status waits share an overall deadline |
| Optional data | Missing Flags2 or an empty route could raise an exception | Handle absent flags and empty routes |
| Inference | Stop after Core ML prediction was caught as a model failure, triggering CPU inference; missing images could invoke YOLO's default source | Propagate cancellation and reject missing images before inference |

## Verification

- All four native helpers rebuilt and signed successfully.
- Launcher shell syntax and tracked Python compilation checks passed; new Python
  modules and test scripts were compiled explicitly as well.
- All 39 no-Elite tests passed (12 existing plus 27 added). They include
  temporary fake helper subprocesses that hang, ignore
  TERM, block writes, return partial/malformed replies, and exit early. Launcher
  tests run a copied launcher against temporary fake executables.
- Full Tk GUI smoke test passed: construct, drain callbacks, Stop All, destroy.
  It uses copied configuration, disables the assist engine and integrations,
  and blocks native input. Reproduce with:

  ```sh
  .venv-macos/bin/python tests_native/smoke_gui.py
  ```

- The actual launcher was tested with a deliberately nonexistent window title.
  It exited nonzero promptly, removed its diagnostic lock directory, and left
  no EDAP/helper processes. This avoids accidentally attaching to a real game.
- The compiled overlay exited after its temporary parent was killed with
  SIGKILL. Its state file was never populated with a game target.
- `git diff --check` passed.

The native event-posting changes are compile-checked but cannot be behaviorally
verified without sending input. The tests intentionally do not do that.

## Supervised integration checklist

1. Launch Elite yourself and launch EDAP normally. Check permissions after the
   helper rebuild, successful first frame, and continued frame updates.
2. Verify capture and overlay alignment on the actual display, including a
   vertically arranged secondary display if used.
3. Exercise an approved existing modifier chord and throttle repetition. Check
   that concurrent requests do not borrow modifiers from each other.
4. Press End during text entry, a held command, and calibration/inference; then
   immediately start a new action. Old work must remain stopped and keys must
   release. Verify start callbacks queued before End do not run afterward.
5. Minimize or switch Spaces during held input; verify cleanup reaches the
   original Wine process. Restore and confirm normal capture recovery.
6. Move or resize Elite while EDAP is idle: EDAP should stop with a relaunch
   message. **Dynamic crop tracking is not implemented.** Relaunch after moving.
7. Close Elite, and separately close EDAP while Elite remains open. Verify all
   owned helpers exit and no keys remain held.

The local bindings check reports that Home is also bound to `GalaxyMapHome`.
That existing collision was not changed: bindings are user-owned. Include it in
hotkey testing. Native interactive mouse calibration/clicking also remains
explicitly unsupported in `MousePt.py`.
