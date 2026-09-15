"""Bounded reads of JSON snapshots rewritten by Elite and companion apps."""

import json
import os
import time


def read_json_snapshot(path, *, timeout=2.0, stop_event=None):
    """Return (object, pre-read mtime); never cache a timestamp read afterward."""
    deadline = time.monotonic() + timeout
    backoff = 0.05
    while True:
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("EDAP assist stop requested")
        try:
            with open(path, encoding='utf-8') as source:
                modified = os.fstat(source.fileno()).st_mtime
                data = json.load(source)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data, modified
        except (OSError, ValueError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Unable to read {path}: {exc}") from exc
            delay = min(backoff, remaining)
            if stop_event is None:
                time.sleep(delay)
            elif stop_event.wait(delay):
                raise InterruptedError("EDAP assist stop requested")
            backoff = min(backoff * 2, 0.5)
