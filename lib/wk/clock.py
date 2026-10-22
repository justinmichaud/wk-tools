"""Time as a parameter: every wait polls through Clock.wait_until, and a fake clock advances when asked to sleep."""

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

    def wait_until(self, done, timeout, interval):
        start = self.monotonic()
        while not done():
            if timeout is not None and self.monotonic() - start >= timeout:
                return False
            self.sleep(interval)
        return True
