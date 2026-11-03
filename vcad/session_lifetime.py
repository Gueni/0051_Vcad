"""Keep the local backend alive while browser viewer pages are connected."""

import threading
import time


class ViewerLifetime:
    def __init__(self, shutdown, grace=3.0):
        self._shutdown, self._grace = shutdown, grace
        self._condition = threading.Condition()
        self._connections = set()
        self._deadline = None
        self.stopping = threading.Event()
        self._monitor = threading.Thread(target=self._watch, name="viewer-lifetime", daemon=True)
        self._monitor.start()

    def connect(self):
        with self._condition:
            if self.stopping.is_set():
                return None
            connection = object()
            self._connections.add(connection)
            self._deadline = None
            self._condition.notify_all()
            return connection

    def disconnect(self, connection):
        with self._condition:
            if connection not in self._connections:
                return
            self._connections.remove(connection)
            if not self._connections and not self.stopping.is_set():
                self._deadline = time.monotonic() + self._grace
                self._condition.notify_all()

    @property
    def connected(self):
        with self._condition:
            return bool(self._connections)

    def close(self):
        with self._condition:
            self.stopping.set()
            self._condition.notify_all()

    def _watch(self):
        with self._condition:
            while not self.stopping.is_set():
                if self._deadline is None:
                    # --no-open and slow browser startup may wait indefinitely
                    # before the first real viewer page connects.
                    self._condition.wait()
                elif (remaining := self._deadline - time.monotonic()) > 0:
                    self._condition.wait(remaining)
                else:
                    # Commit shutdown under the same lock as connect: a late
                    # reconnect cannot revive a server that has begun exiting.
                    self.stopping.set()
                    break
            else:
                return
        self._shutdown()
