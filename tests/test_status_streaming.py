"""Streaming behaviour of `wk status --text`'s renderer (lib/status-view.py)
and the stream cmd/status collects for it.

The stream opens with one `plan` record naming every job and the machine
each one's records belong to; a `flush` ends one job, and a machine's block
is drawn when the last job the plan gave it has flushed. A run asked for
`--records` is one job of the wk that asked and carries neither.

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

from tests.support import REPO, WkTest, rand_suffix, requires_podman_vm, run, scratch_dir, stub_path

STATUS_VIEW = REPO / "lib" / "status-view.py"


def _reap(proc):
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    if proc.stdout:
        proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()


def _rec(**kw):
    return json.dumps(kw) + "\n"


def _plan(*jobs):
    """<(job, machine)...>: a job with no machine completes nobody's block."""
    return _rec(kind="plan", jobs=[
        {"job": j, "machine": m} if m else {"job": j} for j, m in jobs
    ])


def _machine_lines(name):
    """One job's records: its machine, one workspace, and the flush that ends it."""
    return [
        _rec(kind="machine", name=name, self=(name == "alpha")),
        _rec(kind="workspace", machine=name, method="container", name="ws-" + name,
             state="present", ws="present"),
        _rec(kind="flush", job=name),
    ]


def _render(lines):
    recs = os.path.join(tempfile.mkdtemp(prefix="wk-status-stream-"), "records")
    with open(recs, "w") as fh:
        fh.writelines(lines)
    try:
        return subprocess.run([sys.executable, str(STATUS_VIEW), "text", recs],
                              cwd=str(REPO), capture_output=True, text=True,
                              env=dict(os.environ, NO_COLOR="1"), timeout=30)
    finally:
        subprocess.run(["rm", "-rf", os.path.dirname(recs)])


class _LineReader:
    """Drains a subprocess's stdout in a background thread, timestamping each
    line, so a test can assert when a line showed up relative to what the
    main thread did."""

    def __init__(self, proc):
        self.proc = proc
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.q.put((time.monotonic(), line.rstrip("\n")))
        self.q.put(None)

    def until(self, predicate, timeout=5.0):
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
    """Fed over a real fifo, the way cmd/status hands it the stream."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wk-status-stream-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.tmpdir]))
        self.fifo = os.path.join(self.tmpdir, "records")
        os.mkfifo(self.fifo)
        self.proc = subprocess.Popen(
            [sys.executable, str(STATUS_VIEW), "text", self.fifo],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=dict(os.environ, NO_COLOR="1"),
        )
        self.addCleanup(_reap, self.proc)
        self.reader = _LineReader(self.proc)

    def test_first_machine_prints_before_second_machines_records_are_sent(self):
        t0 = time.monotonic()
        # Opening the fifo for writing returns once the renderer has opened it for reading.
        wfh = open(self.fifo, "w")
        try:
            wfh.write(_plan(("alpha", "alpha"), ("beta", "beta")))
            wfh.writelines(_machine_lines("alpha"))
            wfh.flush()

            time.sleep(1.0)
            t_beta_sent = time.monotonic()
            wfh.writelines(_machine_lines("beta"))
            wfh.write(_rec(kind="exit", code=0))
            wfh.flush()
        finally:
            wfh.close()

        alpha_at, _ = self.reader.until(lambda l: l.startswith("alpha"))
        beta_at, _ = self.reader.until(lambda l: l.startswith("beta"))
        self.assertLess(alpha_at, t_beta_sent,
                        "alpha's block waited for beta's records -- not streaming")
        self.assertGreaterEqual(beta_at - t0, 0.9)
        self.assertEqual(self.proc.wait(timeout=5), 0, self.proc.stderr.read())

    def test_a_planned_machine_shows_as_probing_before_it_answers(self):
        wfh = open(self.fifo, "w")
        try:
            wfh.write(_plan(("slowbox", "slowbox")))
            wfh.flush()
            probing_at, _ = self.reader.until(lambda l: "probing slowbox" in l)
            time.sleep(0.3)
            wfh.writelines(_machine_lines("slowbox"))
            wfh.write(_rec(kind="exit", code=0))
            wfh.flush()
        finally:
            wfh.close()
        # The block follows the placeholder rather than erasing it: a scrolling stream, not a redrawn terminal.
        block_at, _ = self.reader.until(lambda l: l.strip() == "slowbox")
        self.assertGreater(block_at, probing_at)
        self.assertEqual(self.proc.wait(timeout=5), 0, self.proc.stderr.read())


class TestABlockWaitsForEveryPlannedJob(unittest.TestCase):
    """A macOS host feeds one machine from two jobs, the podman VM's
    container target and the tart vm target. The block is drawn once, after
    both, whatever order and however far apart they flush."""

    def test_the_first_job_flushing_empty_does_not_draw_the_machine(self):
        cp = _render([
            _plan(("container", "host"), ("vm", "host")),
            _rec(kind="machine", name="host", self=True),
            _rec(kind="flush", job="container"),
            _rec(kind="machine", name="host", self=True),
            _rec(kind="workspace", machine="host", method="macOS guest",
                 name="ws-v", state="stopped", ws="present"),
            _rec(kind="flush", job="vm"),
            _rec(kind="exit", code=0),
        ])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(len([l for l in cp.stdout.splitlines() if l.startswith("host ")]), 1, cp.stdout)
        self.assertIn("ws-v", cp.stdout)
        self.assertNotIn("no workspaces", cp.stdout)
        self.assertLess(cp.stdout.index("probing vm"), cp.stdout.index("host "))

    def test_a_flush_for_a_job_outside_the_plan_is_reported_and_draws_nothing(self):
        cp = _render([
            _plan(("container", "host")),
            _rec(kind="machine", name="host", self=True),
            _rec(kind="flush", job="vm"),
            _rec(kind="workspace", machine="host", method="container",
                 name="ws-c", state="present", ws="present"),
            _rec(kind="flush", job="container"),
            _rec(kind="exit", code=0),
        ])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("'vm' ended without being in the plan", cp.stderr)
        self.assertIn("ws-c", cp.stdout)
        self.assertNotIn("no workspaces", cp.stdout)

    def test_a_job_with_no_machine_completes_no_block(self):
        cp = _render([
            _plan(("alpha", "alpha"), ("devices", None)),
            _rec(kind="flush", job="devices"),
            *_machine_lines("alpha"),
            _rec(kind="exit", code=0),
        ])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(len([l for l in cp.stdout.splitlines() if l.startswith("alpha")]), 1, cp.stdout)
        self.assertIn("ws-alpha", cp.stdout)


class TestJsonModeUnchangedByStreamMarkers(unittest.TestCase):
    """--json is one document at the end; the plan and the flushes are
    invisible to it, exactly as `merge` ignores them for --html and --web."""

    def test_json_output_equals_the_merge_of_the_same_stream_without_markers(self):
        lines = (
            [_plan(("alpha", "alpha"), ("beta", "beta"))]
            + _machine_lines("alpha")
            + _machine_lines("beta")
            + [_rec(kind="fleet", machine="rpi3", role="bench-device",
                    mode="bench mode", media="sd"),
               _rec(kind="exit", code=2)]
        )
        tmp = tempfile.mkdtemp(prefix="wk-status-stream-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp]))
        with_markers = os.path.join(tmp, "with")
        without = os.path.join(tmp, "without")
        with open(with_markers, "w") as fh:
            fh.writelines(lines)
        with open(without, "w") as fh:
            fh.writelines(l for l in lines if '"plan"' not in l and '"flush"' not in l)

        outs = []
        for path in (with_markers, without):
            cp = subprocess.run([sys.executable, str(STATUS_VIEW), "json", path],
                                cwd=str(REPO), capture_output=True, text=True, timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            outs.append(json.loads(cp.stdout))
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0]["exit"], 2)
        self.assertEqual([m["name"] for m in outs[0]["machines"]], ["alpha", "beta"])


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


class TestCollectorMarkers(WkTest):
    """The stream cmd/status collects over one faked reachable target (the
    scaffolding tests/test_fleet_walk.py uses): a rendering run opens with a
    plan and ends every job with a flush; a run asked for `--records` is one
    job of the wk that asked and carries neither."""

    def _status(self, *args):
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _ANSWERING_SSH}) as binp:
            env = {
                "WK_MACHINES_DIR": str(machdir),
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-" + rand_suffix(4),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            }
            return run("status", *args, env=env, timeout=60)

    def test_records_carry_no_markers(self):
        cp = self._status("--records")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        kinds = [json.loads(l)["kind"] for l in cp.stdout.splitlines() if l.startswith("{")]
        self.assertIn("machine", kinds, cp.stdout)
        self.assertNotIn("plan", kinds, cp.stdout)
        self.assertNotIn("flush", kinds, cp.stdout)

    def test_text_draws_the_planned_machine_once(self):
        cp = self._status("--text", "--no-devices")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("probing remote", cp.stdout)
        self.assertEqual(len([l for l in cp.stdout.splitlines() if l.startswith("remote")]), 1, cp.stdout)


class TestEveryWorkspaceIsInTheListing(WkTest):
    """On this macOS host the podman machine answers for the container target
    and tart for the vm target, both as this machine: the bare listing names
    every workspace `wk ls` names."""

    @requires_podman_vm()
    def test_bare_status_lists_every_workspace_ls_lists(self):
        ls = run("ls", "--json", timeout=120)
        self.assertEqual(ls.returncode, 0, ls.stdout)
        names = [w["name"] for w in json.loads(ls.stdout)["workspaces"]]
        st = run("status", "--text", "--no-devices", env={"NO_COLOR": "1"}, timeout=180)
        self.assertEqual(st.returncode & ~4, 0, st.stdout)  # 4 is an unreachable peer, not this machine
        for n in names:
            self.assertRegex(st.stdout, r"(?m)^\s+%s\s" % n, f"'{n}' missing from a bare 'wk status':\n{st.stdout}")


_STOPPED_PODMAN = '''#!/bin/sh
case "$*" in
    *"machine inspect"*) echo stopped ;;
    *) exit 1 ;;
esac
'''


class TestAStoppedPodmanMachineSaysSo(WkTest):
    """On macOS the container target's far side is the podman machine. Stopped,
    it is reported on this machine's block with the command that brings it
    up, never as an empty target."""

    @unittest.skipUnless(sys.platform == "darwin", "the container target has a far side only on macOS")
    def test_the_block_names_wk_start(self):
        with stub_path({"podman": _STOPPED_PODMAN}) as binp:
            cp = run("status", "--records", env={
                "WK_TARGET": "container",
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            }, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        raws = [json.loads(l) for l in cp.stdout.splitlines() if l.startswith('{"kind":"raw"')]
        self.assertEqual(len(raws), 1, cp.stdout)
        self.assertIn("is stopped -- 'wk start' brings it up", raws[0]["text"])
