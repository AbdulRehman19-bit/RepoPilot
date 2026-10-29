"""Hit and miss counters for caches."""


class Stats:
    def __init__(self):
        self.hits = 0
        self.misses = 0

    def record(self, hit):
        if hit:
            self.hits += 1
        else:
            self.misses += 1

    def ratio(self):
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
