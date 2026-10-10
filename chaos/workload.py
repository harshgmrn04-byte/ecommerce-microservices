"""Small steady browse workload used throughout safety recovery acceptance."""
from __future__ import annotations

import threading
import urllib.request


class BrowseLoad:
    def __init__(self, users: int = 3):
        self.users = users
        self.stop = threading.Event()
        self.requests = 0
        self.failures = 0
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    def _worker(self) -> None:
        while not self.stop.is_set():
            for path in ("products?limit=12", "categories"):
                if self.stop.is_set():
                    return
                failed = False
                try:
                    with urllib.request.urlopen(f"http://localhost:3002/{path}", timeout=2) as response:
                        response.read()
                        failed = response.status != 200
                except Exception:
                    failed = True
                with self.lock:
                    self.requests += 1
                    self.failures += int(failed)
            self.stop.wait(.5)

    def __enter__(self):
        self.threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(self.users)]
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=5)
