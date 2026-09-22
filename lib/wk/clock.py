"""Time as a parameter: every wait in the core takes a clock, and a test
hands it one that advances when asked to sleep."""

import time


class Clock:
    def now(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def stamp(self):
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now()))

    def iso(self):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now()))


class FakeClock(Clock):
    def __init__(self, start=1_700_000_000.0):
        self.t = float(start)
        self.slept = []

    def now(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds
