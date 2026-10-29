"""A cache whose entries expire after a number of seconds."""

import time


class TTLCache:
    def __init__(self, ttl, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._data = {}

    def put(self, key, value):
        self._data[key] = (value, self._clock() + self.ttl)

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None or entry[1] <= self._clock():
            self._data.pop(key, None)
            return default
        return entry[0]
