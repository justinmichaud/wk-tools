"""Time as a seam: every wait polls through Clock.wait_until, so a test's clock advances instead of sleeping."""

import time


class Clock:
    def now(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)

    def wait_until(self, done, timeout, interval=1):
        """Poll `done()` every `interval` seconds; True once it holds, False after `timeout` seconds (None: no limit)."""
        start = self.now()
        while not done():
            if timeout is not None and self.now() - start >= timeout:
                return False
            self.sleep(interval)
        return True
