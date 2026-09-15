"""Cross-platform global hotkeys without putting macOS APIs on Tk threads."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading


logger = logging.getLogger(__name__)


if sys.platform == "win32":
    import keyboard as _keyboard

    add_hotkey = _keyboard.add_hotkey
    remove_all_hotkeys = _keyboard.remove_all_hotkeys

    def configure(bindings):
        remove_all_hotkeys()
        for chord, (callback, args) in bindings.items():
            add_hotkey(chord, callback, args=args)
else:
    _lock = threading.Lock()
    _callbacks = {}
    _legacy_bindings = {}
    _process = None

    def _reader(process, ready_event, startup):
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                    if event.get("ready"):
                        startup["ready"] = True
                        ready_event.set()
                        continue
                    callback = _callbacks.get(event.get("id"))
                    if callback:
                        try:
                            callback()
                        except Exception:
                            logger.exception("Global hotkey callback failed")
                except (json.JSONDecodeError, RuntimeError):
                    continue
        finally:
            ready_event.set()

    def configure(bindings):
        global _process, _callbacks
        remove_all_hotkeys()
        if not bindings:
            return
        helper = Path(os.environ.get(
            "EDAP_MACOS_HOTKEY_HELPER",
            Path(__file__).parent / "platform/macos/macos_hotkey_bridge"))
        if not helper.is_file():
            raise RuntimeError(f"Native hotkey helper is missing: {helper}")
        command = [str(helper)]
        callbacks = {}
        for index, (chord, (callback, args)) in enumerate(bindings.items()):
            binding_id = str(index)
            command.extend(("--binding", f"{binding_id}={chord}"))
            callbacks[binding_id] = lambda callback=callback, args=args: callback(*args)
        with _lock:
            _callbacks = callbacks
            _process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, text=True, bufsize=1)
            ready_event = threading.Event()
            startup = {"ready": False}
            threading.Thread(
                target=_reader, args=(_process, ready_event, startup),
                name="EDAP-Hotkeys", daemon=True).start()

        ready_event.wait(timeout=1.0)
        if not startup["ready"] or _process.poll() is not None:
            code = _process.poll()
            remove_all_hotkeys()
            if code == 77:
                raise RuntimeError(
                    "Input Monitoring permission is required for native global hotkeys")
            raise RuntimeError(
                f"Native hotkey helper failed to start (exit status {code})")

    def add_hotkey(chord, callback, args=()):
        _legacy_bindings[chord] = (callback, args)
        configure(dict(_legacy_bindings))

    def remove_all_hotkeys():
        global _process, _callbacks
        with _lock:
            process, _process = _process, None
            _callbacks = {}
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
