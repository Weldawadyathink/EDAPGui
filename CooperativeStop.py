"""Cooperative cancellation that cannot revive an older command on restart."""

from contextlib import contextmanager
from functools import wraps
import threading


class StopEvent:
    """Event-compatible stop signal with a stable event per command.

    clear() starts a fresh generation. Workers inside command() continue to
    observe their original event, including when a new action starts before
    they return from blocking work. Nested commands inherit their caller.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._local = threading.local()

    def capture(self):
        with self._lock:
            return getattr(self._local, 'event', self._event)

    def set(self):
        with self._lock:
            self._event.set()

    def clear(self):
        with self._lock:
            if self._event.is_set():
                self._event = threading.Event()

    def is_set(self):
        return self.capture().is_set()

    def wait(self, timeout=None):
        return self.capture().wait(timeout)

    @contextmanager
    def command(self, event=None):
        previous = getattr(self._local, 'event', None)
        self._local.event = previous if previous is not None else (
            event if event is not None else self.capture())
        try:
            yield
        finally:
            if previous is None:
                del self._local.event
            else:
                self._local.event = previous

    def wrap(self, target):
        """Capture the parent's generation before scheduling a child worker."""
        event = self.capture()

        @wraps(target)
        def run(*args, **kwargs):
            with self.command(event):
                return target(*args, **kwargs)
        return run


def cooperative_command(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.stop_event.command():
            if self.stop_event.is_set():
                raise InterruptedError("EDAP assist stop requested")
            result = method(self, *args, **kwargs)
            if self.stop_event.is_set():
                raise InterruptedError("EDAP assist stop requested")
            return result
    return run
