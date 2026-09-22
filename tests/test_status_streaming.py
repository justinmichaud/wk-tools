"""Streaming behaviour of `wk status --text`: the renderer (wk.statusview)
over the stream the collector (wk.status) hands it.

The stream opens with one `plan` record naming every job and the machine
each one's records belong to; a `flush` ends one job, and a machine's block
is drawn when the last job the plan gave it has flushed. A run asked for
`--records` is one job of the wk that asked and carries neither.

Run: python3 tests/run.py -k tests.test_status_streaming
"""
import contextlib
import io
import json
import os
import sys
import unittest

from tests.support import REPO, WkTest, rand_suffix, requires_container_target, run, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import statusview  # noqa: E402


def _rec(**kw):
    return kw


def _plan(*jobs):
    """<(job, machine)...>: a job with no machine completes nobody's block."""
    return _rec(kind="plan", jobs=[{"job": j, "machine": m} if m else {"job": j} for j, m in jobs])


def _machine_lines(name):
    """One job's records: its machine, one workspace, and the flush that ends it."""
    return [_rec(kind="machine", name=name, self=(name == "alpha")),
            _rec(kind="workspace", machine=name, method="container", name="ws-" + name, state="present", ws="present"),
            _rec(kind="flush", job=name)]


class _Tap:
    """The renderer's output, each line stamped with how many records it had consumed when the line appeared."""

    def __init__(self, records):
        self.records = records
        self.consumed = 0
        self.lines = []

    def feed(self):
        for r in self.records:
            self.consumed += 1
            yield r

    def write(self, text):
        for line in text.rstrip("\n").split("\n"):
            self.lines.append((self.consumed, line))

    def flush(self):
        pass

    def when(self, predicate):
        for consumed, line in self.lines:
            if predicate(line):
                return consumed
        raise AssertionError("no line matched:\n" + "\n".join(l for _, l in self.lines))


def _render(records):
    tap = _Tap(records)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        statusview.render_text_stream(tap.feed(), tap, False)
    return tap, err.getvalue()


class TestTextStreamsAsRecordsArrive(unittest.TestCase):
    def test_first_machine_prints_before_second_machines_records_are_consumed(self):
        records = [_plan(("alpha", "alpha"), ("beta", "beta"))] + _machine_lines("alpha") + _machine_lines("beta") + [_rec(kind="exit", code=0)]
        tap, err = _render(records)
        alpha_at = tap.when(lambda l: l.startswith("alpha"))
        self.assertLessEqual(alpha_at, 4, "alpha's block waited for beta's records -- not streaming")
        self.assertGreater(tap.when(lambda l: l.startswith("beta")), 4)
        self.assertEqual(err, "")

    def test_a_planned_machine_shows_as_probing_before_it_answers(self):
        records = [_plan(("slowbox", "slowbox"))] + _machine_lines("slowbox") + [_rec(kind="exit", code=0)]
        tap, _ = _render(records)
        self.assertEqual(tap.when(lambda l: "probing slowbox" in l), 1)
        self.assertGreater(tap.when(lambda l: l.strip() == "slowbox"), 1)


class TestABlockWaitsForEveryPlannedJob(unittest.TestCase):
    """A macOS host feeds one machine from two jobs, the podman VM's container target and the tart vm target."""

    def test_the_first_job_flushing_empty_does_not_draw_the_machine(self):
        tap, err = _render([
            _plan(("container", "host"), ("vm", "host")),
            _rec(kind="machine", name="host", self=True),
            _rec(kind="flush", job="container"),
            _rec(kind="machine", name="host", self=True),
            _rec(kind="workspace", machine="host", method="macOS guest", name="ws-v", state="stopped", ws="present"),
            _rec(kind="flush", job="vm"),
            _rec(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertEqual(err, "")
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("host ")]), 1, out)
        self.assertIn("ws-v", out)
        self.assertNotIn("no workspaces", out)
        self.assertLess(out.index("probing vm"), out.index("host "))

    def test_a_flush_for_a_job_outside_the_plan_is_reported_and_draws_nothing(self):
        tap, err = _render([
            _plan(("container", "host")),
            _rec(kind="machine", name="host", self=True),
            _rec(kind="flush", job="vm"),
            _rec(kind="workspace", machine="host", method="container", name="ws-c", state="present", ws="present"),
            _rec(kind="flush", job="container"),
            _rec(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertIn("'vm' ended without being in the plan", err)
        self.assertIn("ws-c", out)
        self.assertNotIn("no workspaces", out)

    def test_a_job_with_no_machine_completes_no_block(self):
        tap, _ = _render([_plan(("alpha", "alpha"), ("devices", None)), _rec(kind="flush", job="devices"), *_machine_lines("alpha"),
                          _rec(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("alpha")]), 1, out)
        self.assertIn("ws-alpha", out)


class TestJsonModeUnchangedByStreamMarkers(unittest.TestCase):
    def test_json_output_equals_the_merge_of_the_same_stream_without_markers(self):
        records = ([_plan(("alpha", "alpha"), ("beta", "beta"))] + _machine_lines("alpha") + _machine_lines("beta")
                   + [_rec(kind="fleet", machine="rpi3", role="bench-device", mode="bench mode", media="sd"), _rec(kind="exit", code=2)])
        with_markers = statusview.merge(records)
        without = statusview.merge([r for r in records if r["kind"] not in ("plan", "flush")])
        self.assertEqual(with_markers, without)
        self.assertEqual(with_markers["exit"], 2)
        self.assertEqual([m["name"] for m in with_markers["machines"]], ["alpha", "beta"])
        self.assertEqual(json.loads(json.dumps(with_markers)), with_markers)


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


class TestCollectorMarkers(WkTest):
    """The stream cmd/status collects over one faked reachable target: a rendering run opens with a plan
    and ends every job with a flush; a run asked for `--records` carries neither."""

    def _status(self, *args):
        with scratch_dir(prefix="wk-test-machines-") as machdir, stub_path({"ssh": _ANSWERING_SSH}) as binp:
            env = {"WK_MACHINES_DIR": str(machdir), "WK_TARGET": "remote", "WK_REMOTE_HOST": "fake-reachable-" + rand_suffix(4),
                   "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}", "WK_PROBE_SECONDS": "1"}
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


_MARKER_LEAKING_SSH = '''#!/bin/sh
for last; do :; done
case "$last" in
    *.wk-remote*) exit 0 ;;
    *--records*)
        printf '%s\\n' '{"kind":"machine","name":"remote"}' \\
            '{"kind":"flush","job":"remote"}' \\
            '{"kind":"workspace","machine":"remote","method":"native","name":"leaky-ws","state":"running","ws":"present"}'
        exit 0 ;;
esac
exec bash -c "$last"
'''


class TestARemotesMarkersStayItsOwn(WkTest):
    """A remote's plan and flush records end its jobs, not this walk's."""

    def test_a_flush_in_a_remotes_records_does_not_draw_its_block_early(self):
        with scratch_dir(prefix="wk-test-machines-") as machdir, stub_path({"ssh": _MARKER_LEAKING_SSH}) as binp:
            cp = run("status", "--text", "--no-devices", env={
                "WK_MACHINES_DIR": str(machdir), "WK_TARGET": "remote", "WK_REMOTE_HOST": "fake-leaky-" + rand_suffix(4),
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}", "WK_PROBE_SECONDS": "1"}, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(len([l for l in cp.stdout.splitlines() if l.startswith("remote")]), 1, cp.stdout)
        self.assertRegex(cp.stdout, r"(?m)^\s+leaky-ws\s", cp.stdout)


class TestEveryWorkspaceIsInTheListing(WkTest):
    @requires_container_target()
    def test_bare_status_lists_every_workspace_ls_lists(self):
        ls = run("ls", "--json", timeout=120)
        self.assertEqual(ls.returncode, 0, ls.stdout)
        names = [w["name"] for w in json.loads(ls.stdout)["workspaces"]]
        st = run("status", "--text", "--no-devices", env={"NO_COLOR": "1"}, timeout=180)
        self.assertIn(st.returncode, (0, 2, 4), st.stdout)
        for n in names:
            self.assertRegex(st.stdout, r"(?m)^\s+%s\s" % n, f"'{n}' missing from a bare 'wk status':\n{st.stdout}")


_STOPPED_PODMAN = '''#!/bin/sh
case "$*" in
    *"machine inspect"*) echo stopped ;;
    *) exit 1 ;;
esac
'''


class TestAStoppedPodmanMachineSaysSo(WkTest):
    @unittest.skipUnless(sys.platform == "darwin", "the container target has a far side only on macOS")
    def test_the_block_names_wk_start(self):
        with stub_path({"podman": _STOPPED_PODMAN}) as binp:
            cp = run("status", "--records", env={"WK_TARGET": "container", "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}"}, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        raws = [json.loads(l) for l in cp.stdout.splitlines() if l.startswith('{"kind":"raw"')]
        self.assertEqual(len(raws), 1, cp.stdout)
        self.assertIn("is stopped -- 'wk start' brings it up", raws[0]["text"])
