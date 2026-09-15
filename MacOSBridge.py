"""Client for the native macOS window and keyboard event helper."""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import select
import subprocess
import threading


class MacOSBridgeError(RuntimeError):
    pass


class MacOSBridge:
    def __init__(self):
        default = Path(__file__).parent / "platform/macos/macos_input_bridge"
        self.helper = Path(os.environ.get("EDAP_MACOS_INPUT_HELPER", default))
        self._process = None
        self._lock = threading.Lock()
        self.timeout = float(os.environ.get("EDAP_MACOS_BRIDGE_TIMEOUT", "2.0"))

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        if not self.helper.is_file():
            raise MacOSBridgeError(
                f"Native input helper is missing: {self.helper}. Run platform/macos/build_bridges.command.")
        self._process = subprocess.Popen(
            [str(self.helper), "--window-title", os.environ.get(
                "EDAP_ELITE_WINDOW_TITLE", "Elite - Dangerous (CLIENT)")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
        )

    def request(self, op, **values):
        with self._lock:
            self._start()
            request = {"op": op, **values}
            try:
                self._process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                self._process.stdin.flush()
                ready, _, _ = select.select(
                    [self._process.stdout], [], [], self.timeout)
                if not ready:
                    raise TimeoutError(
                        f"Native input helper did not respond within {self.timeout:g} seconds")
                line = self._process.stdout.readline()
            except (BrokenPipeError, OSError, TimeoutError) as exc:
                self.close()
                raise MacOSBridgeError(f"Native input helper stopped: {exc}") from exc
            if not line:
                code = self._process.poll()
                self.close()
                raise MacOSBridgeError(f"Native input helper exited unexpectedly ({code})")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self.close()
                raise MacOSBridgeError(
                    f"Native input helper returned malformed data: {exc}") from exc
            if not response.get("ok"):
                raise MacOSBridgeError(response.get("error", "Native helper request failed"))
            return response

    def key(self, scan_code: int, down: bool):
        return self.request("key", scanCode=int(scan_code), down=bool(down))

    def focus_elite(self):
        return self.request("focus")

    def window_info(self):
        return self.request("window")

    def permission_status(self):
        return self.request("permissions")

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.stdin.write('{"op":"quit"}\n')
                process.stdin.flush()
                process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()


bridge = MacOSBridge()
atexit.register(bridge.close)
