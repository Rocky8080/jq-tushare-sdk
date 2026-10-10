"""Reentrant, cross-process coordination for one local market-data cache."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading
import time


_registry_lock = threading.Lock()
_locks = {}
_held = threading.local()


@contextmanager
def cache_write_lock(cache_db, *, cancelled=None):
    """Coordinate cache mutations, including nested backend writes.

    A synchronizer can hold this across several API calls, while the backend
    takes it around individual transactions. Other threads and processes wait;
    same-thread nested calls reuse the outer file lock. Cancellation applies to
    acquisition only, leaving an acquired transaction responsible for cleanup.
    """
    key = str(Path(cache_db).expanduser().resolve())
    with _registry_lock:
        lock = _locks.setdefault(key, threading.RLock())

    def check_cancelled():
        if cancelled is not None and cancelled():
            raise InterruptedError("Market-data cache lock acquisition cancelled")

    check_cancelled()
    while not lock.acquire(timeout=0.2):
        check_cancelled()
    descriptor = None
    held = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    nested = key in held
    try:
        check_cancelled()
        if not nested:
            path = Path(key + ".sync.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            while True:
                check_cancelled()
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.2)
            held.add(key)
        yield
    finally:
        if not nested:
            held.discard(key)
            if descriptor is not None:
                os.close(descriptor)
        lock.release()
