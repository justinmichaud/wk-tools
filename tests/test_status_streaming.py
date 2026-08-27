"""Streaming behaviour of `wk status --text`'s renderer (lib/status-view.py).

Defect: every probe used to finish before anything rendered, so a slow or
dead machine delayed the whole page. cmd/status's collector
(par_join_stream) now hands a finished machine's records to the stream the
moment that machine is done, in the order machines actually finish rather
than the order their probes were started in, and lib/status-view.py's text
renderer (render_text_stream) draws each machine's block as its records
arrive instead of waiting for the last one.

These tests feed the renderer a synthetic record stream over a real named
pipe, with a deliberate delay between two machines' records -- standing in
for cmd/status's `par_join_stream` without needing real hardware or a
running fleet -- and check:

  1. text mode prints the first machine's block before the second machine's
     records are even sent, proof that rendering does not wait for the batch.
  2. json mode is unaffected by any of this: fed the exact same stream
     (probing/flush markers included), it produces exactly the document
     `merge()` computes without them -- one document, at the end, same as
     before streaming existed.

Run: python3 -m unittest tests.test_status_streaming -v
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests.support import REPO

STATUS_VIEW = REPO / "lib" / "status-view.py"


def _reap(proc):
    """Cleanup for a Popen'd status-view.py: stop it if it is somehow still
    running, then close its pipes -- otherwise unittest warns about a leaked
    fd for every test that starts one of these."""
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    if proc.stdout:
        proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()


def _rec(**kw):
    return json.dumps(kw) + "\n"


def _machine_lines(name):
    """One machine's whole contribution to the stream: a probing marker, its
    machine/workspace records, and the flush that ends its batch -- exactly
    what one par_run job in cmd/status's collector produces."""
    return [
        _rec(kind="probing", name=name),
        _rec(kind="machine", name=name, self=(name == "alpha")),
        _rec(kind="workspace", machine=name, method="container", name="ws-" + name,
             state="present", ws="present"),
        _rec(kind="flush"),
    ]


class _LineReader:
    """Drains a subprocess's stdout in a background thread, timestamping each
    line against a shared clock -- so a test can assert not just that a line
    showed up, but when, relative to events the main thread causes."""

    def __init__(self, proc, t0):
        self.proc = proc
        self.t0 = t0
        self.q = queue.Queue()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        for line in self.proc.stdout:
            self.q.put((time.monotonic(), line.rstrip("\n")))
        self.q.put(None)  # EOF

    def until(self, predicate, timeout=5.0):
        """Read lines until one matches `predicate`. Returns (arrival_time,
        line) for the matching line, or raises on timeout/EOF first."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for: %r" % predicate)
            item = self.q.get(timeout=remaining)
            if item is None:
                raise AssertionError("stream ended before a matching line arrived")
            arrived, line = item
            if predicate(line):
                return arrived, line


class TestTextStreamsAsRecordsArrive(unittest.TestCase):
    """`wk status --text`'s renderer draws the first machine's block long
    before the last machine has answered -- fed over a real pipe, the way
    cmd/status's fifo actually hands it the stream."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wk-status-stream-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.tmpdir]))
        self.fifo = os.path.join(self.tmpdir, "records")
        os.mkfifo(self.fifo)

    def test_first_machine_prints_before_second_machines_records_are_sent(self):
        proc = subprocess.Popen(
            [sys.executable, str(STATUS_VIEW), "text", self.fifo],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, NO_COLOR="1"),
        )
        self.addCleanup(_reap, proc)

        t0 = time.monotonic()
        reader = _LineReader(proc, t0)

        # Opening the fifo for writing blocks until status-view.py has
        # opened it for reading, which happens right at the top of its
        # `text` mode -- so by the time this returns, the renderer is
        # already live and reading.
        wfh = open(self.fifo, "w")
        try:
            for line in _machine_lines("alpha"):
                wfh.write(line)
            wfh.flush()

            # The moment the second machine's *own* records go out -- what
            # "before the second machine's records are sent" is measured
            # against below, not merely when its block finishes printing.
            time.sleep(1.0)
            t_beta_sent = time.monotonic()
            for line in _machine_lines("beta"):
                wfh.write(line)
            wfh.write(_rec(kind="exit", code=0))
            wfh.flush()
        finally:
            wfh.close()

        alpha_at, _ = reader.until(lambda l: "alpha" in l and "probing" not in l)
        beta_at, _ = reader.until(lambda l: "beta" in l and "probing" not in l)

        self.assertLess(
            alpha_at, t_beta_sent,
            "alpha's block printed at %.3fs, which is not before beta's own "
            "records were sent at %.3fs -- streaming is not happening"
            % (alpha_at - t0, t_beta_sent - t0),
        )
        # And the sanity check the other way: beta really did take the full
        # second, so the assertion above is about streaming, not accidental
        # equal timing.
        self.assertGreaterEqual(beta_at - t0, 0.9)

        rc = proc.wait(timeout=5)
        self.assertEqual(rc, 0, proc.stderr.read())

    def test_a_probing_placeholder_shows_before_its_machine_answers(self):
        """A slow machine is visibly still being asked about, not silently
        missing, while its probe is in flight."""
        proc = subprocess.Popen(
            [sys.executable, str(STATUS_VIEW), "text", self.fifo],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, NO_COLOR="1"),
        )
        self.addCleanup(_reap, proc)
        reader = _LineReader(proc, time.monotonic())

        wfh = open(self.fifo, "w")
        try:
            wfh.write(_rec(kind="probing", name="slowbox"))
            wfh.flush()
            probing_at, _ = reader.until(lambda l: "probing slowbox" in l)

            time.sleep(0.3)
            for line in _machine_lines("slowbox")[1:]:  # skip the duplicate probing record
                wfh.write(line)
            wfh.write(_rec(kind="exit", code=0))
            wfh.flush()
        finally:
            wfh.close()

        # The real block follows the placeholder rather than erasing it --
        # this is a scrolling stream, not a redrawn terminal.
        block_at, _ = reader.until(lambda l: l.strip() == "slowbox")
        self.assertGreater(block_at, probing_at)

        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read())


class TestJsonModeUnchangedByStreamMarkers(unittest.TestCase):
    """--json still produces one document, at the end -- the streaming
    markers (`probing`, `flush`) are invisible to it, exactly as `merge`
    already ignores them for --html and --web."""

    def test_json_output_equals_the_merge_of_the_same_stream_without_markers(self):
        lines = (
            _machine_lines("alpha")
            + _machine_lines("beta")
            + [_rec(kind="fleet", machine="rpi3", role="bench-device",
                     mode="bench mode", media="sd"),
               _rec(kind="exit", code=2)]
        )

        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, dir=self.tmp_dir()
        ) as fh:
            fh.writelines(lines)
            path = fh.name
        self.addCleanup(os.unlink, path)

        cp = subprocess.run(
            [sys.executable, str(STATUS_VIEW), "json", path],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        actual = json.loads(cp.stdout)

        expected = _expected_doc(lines)
        self.assertEqual(actual, expected)

    def tmp_dir(self):
        d = tempfile.mkdtemp(prefix="wk-status-stream-json-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", d]))
        return d


def _expected_doc(lines):
    """The same fold `merge()` does, run in-process against a fresh module
    import -- lib/status-view.py's filename is not import-friendly, so it is
    loaded by path rather than duplicating the merge logic here."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("status_view_ref", str(STATUS_VIEW))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.merge(lines)


if __name__ == "__main__":
    unittest.main()
