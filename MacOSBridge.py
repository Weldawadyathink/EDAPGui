"""Client for the native macOS window and keyboard event helper."""

from __future__ import annotations

import atexit
import json
import math
import os
from pathlib import Path
import select
import subprocess
import threading
import time


class MacOSBridgeError(RuntimeError):
    pass


class MacOSBridge:
    def __init__(self):
        default = Path(__file__).parent / "platform/macos/macos_input_bridge"
        self.helper = Path(os.environ.get("EDAP_MACOS_INPUT_HELPER", default))
        self._process = None
        # close() can be reached while request() is handling a broken helper.
        # An RLock keeps that recovery path serialized without deadlocking it.
        self._lock = threading.RLock()
        self.timeout = float(os.environ.get("EDAP_MACOS_BRIDGE_TIMEOUT", "2.0"))
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("EDAP_MACOS_BRIDGE_TIMEOUT must be finite and positive")

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self.close()
        if not self.helper.is_file():
            raise MacOSBridgeError(
                f"Native input helper is missing: {self.helper}. Run platform/macos/build_bridges.command.")
        self._process = subprocess.Popen(
            [str(self.helper), "--window-title", os.environ.get(
                "EDAP_ELITE_WINDOW_TITLE", "Elite - Dangerous (CLIENT)")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)

    def _exchange(self, payload):
        # Readiness of a pipe guarantees bytes, not a complete JSON line.
        # Keep both writes and reads nonblocking under one response deadline.
        deadline = time.monotonic() + self.timeout
        output = bytearray()
        stdin = self._process.stdin.fileno()
        stdout = self._process.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Native input helper did not respond within {self.timeout:g} seconds")
            readable, writable, _ = select.select(
                [] if payload else [stdout], [stdin] if payload else [], [], remaining)
            if writable:
                try:
                    payload = payload[os.write(stdin, payload):]
                except BlockingIOError:
                    continue
            if readable:
                try:
                    chunk = os.read(stdout, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    raise OSError("EOF before a complete helper response")
                output.extend(chunk)
                if len(output) > 65536:
                    raise ValueError("Helper response exceeds 64 KiB")
                if b"\n" in output:
                    line, extra = output.split(b"\n", 1)
                    if extra:
                        raise ValueError("Unexpected extra helper response")
                    return json.loads(line)

    def request(self, op, *, start_helper=True, **values):
        with self._lock:
            if not start_helper and (
                    self._process is None or self._process.poll() is not None):
                return None
            try:
                self._start()
                payload = (json.dumps({"op": op, **values}, separators=(",", ":")) + "\n").encode()
                response = self._exchange(payload)
                if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                    raise ValueError("Helper response must be an object with a boolean 'ok'")
            except (OSError, ValueError) as exc:
                self.close()
                raise MacOSBridgeError(f"Native input helper failed: {exc}") from exc
            if not response["ok"]:
                raise MacOSBridgeError(response.get("error", "Native helper request failed"))
            return response

    def key(self, scan_code: int, down: bool):
        return self.request("key", start_helper=bool(down), scanCode=int(scan_code), down=bool(down))

    def focus_elite(self):
        return self.request("focus")

    def window_info(self):
        return self.request("window")

    def permission_status(self):
        return self.request("permissions")

    def release_all(self):
        """Release input owned by an existing helper without starting one."""
        return self.request("releaseAll", start_helper=False)

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            try:
                # EOF asks the helper to release its held keys and exit. Never
                # flush a potentially full pipe as part of bounded cleanup.
                process.stdin.close()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
            finally:
                process.stdout.close()


bridge = MacOSBridge()
atexit.register(bridge.close)
