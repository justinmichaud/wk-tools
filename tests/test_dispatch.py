"""The dispatcher's rules (lib/wk/dispatch.py), each one body over every command's table, driven in process."""
import contextlib
import dataclasses
import io
import os
import re
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.support import REPO, WkTest, clean_env, fake_workspace, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import act, decl, dispatch, places, presets, project, repos, sshalias, store, workspace  # noqa: E402
from wk.decl import Command  # noqa: E402
from tests.fakes import Fake  # noqa: E402

TABLES = [(name, m.COMMAND) for name, m in decl.commands(REPO)]


class Handed(Exception):
    def __init__(self, how, payload, readonly=None):
        self.how, self.payload, self.env, self.readonly = how, payload, dict(os.environ), readonly


def dispatched(argv, macos=False, place="container", delegates=False, env=None, refuse=lambda *a: None, run=False, ask=False):
    """(how, what was handed on, the environment it ran under), or ("exit", status, output): nothing reaches a machine."""
    def here(inv):
        raise Handed("here", inv.args)

    def forward(*a):
        raise Handed("forward", list(a[-1]))

    def delegate(t, cmd, args, readonly):
        raise Handed("delegate", args, readonly)

    patches = [mock.patch.dict(os.environ, clean_env(env, wk_root=False), clear=True),
               mock.patch.object(dispatch, "_registry", None), mock.patch.object(dispatch, "is_macos", lambda: macos),
               mock.patch.object(dispatch, "in_vm", lambda: False), mock.patch.object(store.Store, "is_local", lambda self: not macos),
               mock.patch.object(dispatch, "vm_argv", lambda inv, argv: argv), mock.patch.object(dispatch, "exec_flushed", forward),
               mock.patch.object(dispatch.Invocation, "forward", forward), mock.patch.object(dispatch, "delegate_run", delegate),
               mock.patch.object(dispatch, "delegate_driver", lambda t: object() if delegates else None),
               mock.patch.object(dispatch, "resolve_place", lambda *a: place),                mock.patch.object(dispatch, "check_needs", lambda *a: None),
               mock.patch.object(workspace, "refuse_unsaved_before_forward", refuse),
               mock.patch.object(sshalias, "alias_remove", lambda *a: None)]
    patches += [] if run else [mock.patch.object(dispatch.Invocation, "run", here)]
    patches += [] if ask else [mock.patch.object(dispatch, "ask_place", lambda *a: None)]
    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stderr(out))
        stack.enter_context(contextlib.redirect_stdout(out))
        try:
            dispatch.main(list(argv))
        except Handed as h:
            return h.how, h, h.env
        except act.Refused as e:
            return "exit", e.status, out.getvalue()
    raise AssertionError("dispatch.main returned")


def invocations(name, table, verbs=()):
    """(words, the table they make) for the command, each verb under it and each option that changes it."""
    if table.subs:
        for verb, fields in table.subs.items():
            yield from invocations(name, decl.under(table, fields), (*verbs, verb))
        return
    words = list(verbs) + (["ws1"] if table.named() else [])
    words += [presets.names()[0] if table.preset == "arg" and i == 0 else "a%d" % i for i in range(len(table.takes))]
    yield words, table
    for o in table.opts:
        if o.sets:
            t = dataclasses.replace(table, **o.sets)
            yield list(verbs) + (["ws1"] if t.named() else []) + ["a%d" % i for i in range(len(t.takes))] + [o.flag] + (
                ["v"] if o.value and not o.optional else []), t


def expected(name, table, words, macos, delegates):
    with mock.patch.dict(os.environ, clean_env(wk_root=False), clear=True):
        t, args, _ = decl.parse(name, table, words)
        where = t.where(args, places.Registry(REPO)) if callable(t.where) else t.where
    if where in ("host", "local"):
        return "here"
    if where == "store":
        return "forward" if macos else "here"
    if delegates and t.name != "none" and not t.here and not t.lifecycle:
        return "delegate"
    return "forward" if macos and t.forward and not t.here else "here"


class TestWhere(unittest.TestCase):
    def test_every_invocation_runs_where_its_table_says_and_is_told_its_workspace(self):
        checked = 0
        for macos, delegates in ((True, False), (False, False), (False, True)):
            for name, table in TABLES:
                for words, t in invocations(name, table):
                    with self.subTest(cmd=name, words=words, macos=macos, delegates=delegates):
                        want = expected(name, table, words, macos, delegates)
                        how, h, env = dispatched([name, "--quiet", *words], macos=macos, delegates=delegates)
                        self.assertEqual(how, want, "wk %s %s -> %s %s" % (name, " ".join(words), how, h))
                        self.assertEqual(env.get("WK_QUIET"), "1")
                        if how in ("forward", "delegate"):
                            self.assertNotIn("--quiet", h.payload)
                        if how == "delegate":
                            self.assertEqual(h.readonly, t.readonly)
                        if how == "here" and env.get("WK_NAME"):
                            self.assertEqual((env["WK_NAME"], env.get("WK_PLACE", "container")), ("ws1", "container"))
                        checked += 1
        self.assertGreater(checked, 3 * len(TABLES))

    def test_a_far_wk_is_handed_argv_as_typed(self):
        how, h, _ = dispatched(["pr", "--yes", "ws1", "1234"], macos=True)
        self.assertEqual((how, h.payload), ("forward", ["ws1", "1234"]))

    def test_a_signal_reaches_the_commands_own_handler_and_a_far_child_is_handed_it(self):
        def main(args, inv):
            got = []
            signal.signal(signal.SIGTERM, lambda s, f: got.append(s))
            os.kill(os.getpid(), signal.SIGTERM)
            return 0 if got == [signal.SIGTERM] else 1
        fake = types.SimpleNamespace(COMMAND=Command(where="local"), main=main, __doc__="probe")
        saved = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(decl, "load", lambda root, name: fake), self.assertRaises(act.Refused) as cm:
                dispatch.main(["gc"])
        finally:
            signal.signal(signal.SIGTERM, saved)
        self.assertEqual(cm.exception.status, 0)

        class Child:
            sent = []

            def send_signal(self, signum):
                self.sent.append(signum)

            def wait(self):
                os.kill(os.getpid(), signal.SIGTERM)
                return 143
        self.assertEqual((dispatch.relayed(Child()), Child.sent, signal.getsignal(signal.SIGTERM)), (143, [signal.SIGTERM], saved))

    def test_the_dispatcher_asks_before_it_forwards_a_removal(self):
        how, status, _ = dispatched(["rm", "ws1"], macos=True, run=True, env={"WK_YES": "1"},
                                    refuse=lambda reg, names: act.die("wk bench export task"))
        self.assertEqual((how, status), ("exit", 1))
        self.assertEqual(dispatched(["rm", "ws1"], macos=True, run=True)[0], "forward")


class TestDestructiveAsksOnce(unittest.TestCase):
    def test_every_destructive_invocation_is_armed_and_nothing_else_is(self):
        armed = 0
        for name, table in TABLES:
            for words, t in invocations(name, table):
                with self.subTest(cmd=name, words=words):
                    how, h, env = dispatched([name, *words])
                    self.assertEqual(env.get("WK_DESTRUCTIVE") == "1", t.destructive and how == "here", (how, h))
                    if t.destructive and how == "here":
                        self.assertNotIn("WK_CONFIRMED", env)
                        self.assertEqual(dispatched([name, *words, "--yes"])[2].get("WK_YES"), "1")
                        armed += 1
        self.assertGreater(armed, 10)

    def test_a_forwarded_command_carries_the_answer_and_the_force_and_no_place(self):
        line = places.Container("container", str(REPO), {}, Fake()).wk_cmd(
            ["rm", "ws1"], {"WK_YES": "1", "WK_FORCE": "1", "WK_DESTRUCTIVE": "1", "WK_CONFIRMED": "1", "WK_PLACE": "box",
                            "WK_STORE": "/here", "WK_SDK_IMAGE": "ghcr.io/igalia/wkdev-sdk:2.55-v1-abc"})
        for want in ("WK_YES=1 ", "WK_FORCE=1 ", "WK_SDK_IMAGE=ghcr.io/igalia/wkdev-sdk:2.55-v1-abc "):
            self.assertIn(want, line)
        for absent in ("WK_CONFIRMED", "WK_DESTRUCTIVE", "WK_PLACE", "WK_STORE"):
            self.assertNotIn(absent, line)


class TestForceNamesWhatItCrosses(unittest.TestCase):
    def test_force_is_carried_as_environment(self):
        self.assertEqual(dispatched(["gc", "--force"])[2].get("WK_FORCE"), "1")

    def test_force_is_read_only_to_cross_a_barrier_or_to_record_a_result_as_forced(self):
        offenders = []
        for p in sorted((REPO / "lib" / "wk").rglob("*.py")):
            lines = p.read_text().splitlines()
            for i, line in enumerate(lines):
                if p.name != "act.py" and re.search(r"\bforced\(", line) and '"forced=" + ' not in line and '"forced": ' not in line \
                        and not any("barrier(" in l for l in lines[i:i + 3]):
                    offenders.append("%s:%d: %s" % (p.relative_to(REPO), i + 1, line.strip()))
        self.assertEqual(offenders, [])


class TestRefusals(unittest.TestCase):
    def test_an_unknown_command_or_none_prints_every_command(self):
        for argv in (["nosuchcommand"], []):
            how, status, out = dispatched(argv)
            self.assertEqual((how, status), ("exit", 2))
            self.assertEqual([n for n, _ in TABLES if "\n  %s " % n not in out], [])

    def test_a_far_end_no_conf_names_is_refused_naming_the_conf_to_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "wk-remote")
            with open(marker, "w") as f:
                f.write("place=box\n")
            how, status, out = dispatched(["gc"], env={"WK_REMOTE_MARKER": marker})
        self.assertEqual((how, status), ("exit", 1), out)
        self.assertIn("no machines/<name>.conf\n    names it", out)

    def test_a_name_no_workspace_answers_to_is_refused_with_the_usage_line(self):
        with mock.patch.object(places.Registry, "exists_on", lambda *a: False):
            how, status, out = dispatched(["status", "nosuchws", "--log"], ask=True)
        self.assertEqual((how, status), ("exit", 2), out)
        self.assertIn("no such workspace: nosuchws", out)
        self.assertIn("usage: wk status", out)

    def test_dry_run_is_refused_only_where_the_table_says_exempt(self):
        self.assertEqual([n for n, t in TABLES if not t.dryrun], ["selftest"])
        how, status, out = dispatched(["selftest", "--dry-run"])
        self.assertEqual((how, status), ("exit", 2))
        self.assertIn("exempt from --dry-run", out)

    def test_inside_a_workspace_a_host_command_and_a_name_are_refused_naming_the_way_on(self):
        with fake_workspace() as ws:
            for argv, said in ((["machine", "setup", "rpi4", "--dry-run"], "From the host:  wk machine setup rpi4 --dry-run"),
                               (["gc"], "acts on a host"), (["zed"], "From the host:  wk zed selftest-ws"),
                               (["doctor", "other-ws"], "unexpected argument: other-ws (this is workspace 'selftest-ws'")):
                with self.subTest(argv=argv):
                    how, status, out = dispatched(argv, env=ws.env())
                    self.assertEqual(how, "exit")
                    self.assertIn(said, out)
            self.assertEqual(dispatched(["boot", "rpi4", "--status"], env=ws.env())[0], "here", "the broker's door")


class TestPresets(unittest.TestCase):
    def test_the_preset_reaches_the_command_as_wk_preset_never_inherited(self):
        for argv, want in ((["run", "ws1", "--preset=gtk-release", "--", "--preset=x"], "gtk-release"),
                           (["build", "ws1", "jsc-debug", "--", "x"], "jsc-debug"), (["build", "--list"], None)):
            with self.subTest(argv=argv):
                self.assertEqual(dispatched(argv)[2].get("WK_PRESET"), want)

    def test_one_presets_does_not_hold_or_an_exported_one_argv_does_not_name_is_refused(self):
        self.assertIn("unknown preset: jsc-relase", dispatched(["test", "ws1", "--preset=jsc-relase"])[2])
        for argv, remedy in ((["run", "ws1"], "--preset gtk-release"), (["build", "ws1", "jsc-release"], "wk build <workspace> gtk-release")):
            how, status, out = dispatched(argv, env={"WK_PRESET": "gtk-release"})
            self.assertEqual((how, status), ("exit", 2))
            self.assertIn(remedy, out)
        self.assertEqual(dispatched(["run", "ws1", "--preset", "gtk-release"], env={"WK_PRESET": "gtk-release"})[0], "here")


class TestACommandServesItsRepos(unittest.TestCase):
    OTHER = next(r for r in repos.names() if r != project.get("REPO"))

    def held(self, repo, argv):
        loaded = []

        class Driver:
            def repo(self, name):
                return repos.Repo(repo)
        with mock.patch.object(places.Registry, "load", lambda reg, place: loaded.append(place) or Driver()):
            return dispatched(argv), loaded

    def test_every_table_naming_repos_refuses_another_repos_workspace_and_one_naming_none_asks_nothing(self):
        checked = 0
        for name, table in TABLES:
            for words, t in invocations(name, table):
                if t.repos and t.name == "required" and not callable(t.where) and t.where == "workspace":
                    (how, rc, out), _ = self.held(self.OTHER, [name, *words])
                    self.assertEqual((how, rc), ("exit", 1), out)
                    self.assertIn("holds %s" % self.OTHER, out)
                    self.assertEqual("here", self.held(project.get("REPO"), [name, *words])[0][0])
                    checked += 1
        self.assertGreater(checked, 5)
        (how, _, _), loaded = self.held(self.OTHER, ["enter", "ws1"])
        self.assertEqual((how, loaded), ("here", []))


class TestHelp(unittest.TestCase):
    def explained(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), self.assertRaises(act.Refused) as cm:
            dispatch.Invocation(argv[0], list(argv[1:]))
        self.assertEqual(cm.exception.status, 0)
        return out.getvalue()

    def test_every_command_explains_itself_from_its_table_and_docstring(self):
        for name, table in TABLES:
            if table.values:
                continue   # asked of the command itself: tests/test_help_values.py
            with self.subTest(cmd=name):
                text = self.explained(name, "-h")
                self.assertTrue(text.startswith("usage: %s\n" % decl.usage(name, table)), text)
                self.assertIn("what it does (from cmd/%s)" % name, text)
                for _, t, opts in decl.variants(name, table):
                    for o in opts:
                        self.assertIn(o.help, text)
        new = self.explained("new", "--help").split("valid values (--repo):\n")[1].splitlines()
        self.assertEqual(["%s%s" % (r, " (the default)" if r == project.get("REPO") else "") for r in repos.names()],
                         [v.strip() for v in new[:len(repos.names())]])

    def test_a_command_line_beside_h_is_previewed_or_its_refusal_shown(self):
        self.assertIn("this command line runs on: this host's own hardware", self.explained("gc", "--rows", "-h"))
        text = self.explained("build", "ws1", "jsc-release", "-h")
        self.assertIn("    workspace: ws1\n    preset: jsc-release\n", text)
        text = self.explained("gc", "--nonesuch", "-h")
        self.assertIn("unknown option: --nonesuch", text)
        self.assertIn("this command line is refused, as above", text)
        self.assertNotIn("this command line", self.explained("gc", "-h"))


class TestDispatcherEffects(unittest.TestCase):
    """`tailnet` need and the podman machine's start go through the Machine seam."""

    def test_the_tailnet_need(self):
        on = Fake()
        on.answer(("tailscale", "status", "--json"), out='{"Self": {"DNSName": "here.tail.ts.net.", "TailscaleIPs": ["100.64.0.1"]}}')
        off = Fake()
        off.answer(("tailscale", "status", "--json"), rc=1)
        for fake, refused in ((on, False), (off, True), (Fake(), True)):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    dispatch.check_needs("x", Command(needs=("tailnet",)), fake)
                except act.Refused:
                    pass
            self.assertEqual("not on the tailnet" in err.getvalue(), refused)

    def test_the_podman_machine_starts_through_the_machine_asking_first_by_a_terminal(self):
        start = ("podman", "machine", "start", dispatch.MACHINE)
        for tty, dry, effects in ((True, False, [("run_tty", start, None)]), (True, True, []), (False, False, [])):
            fake, err = Fake(), io.StringIO()
            fake.answer(start)
            with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"} if dry else {}), mock.patch.object(dispatch.guest, "podman_admit"), \
                    contextlib.redirect_stderr(err):
                try:
                    dispatch.start_podman_machine(fake, "enter", tty)
                except act.Refused:
                    self.assertIn("wk start", err.getvalue())
            self.assertEqual(fake.effects, effects)

    def test_a_peer_delegates_and_one_that_does_not_answer_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "hosts"))
            with open(os.path.join(tmp, "hosts", "peer.conf"), "w") as f:
                f.write("kind=peer\ntools=/opt/wk-tools\n")
            env = {"HOME": tmp, "XDG_STATE_HOME": tmp + "/state", "WK_STORE": tmp + "/store", "WK_MACHINES_DIR": tmp + "/hosts",
                   "WK_IN_VM": "1", "PATH": os.environ.get("PATH", "")}
            fake = Fake("host")
            with mock.patch.object(dispatch, "_registry", places.Registry(REPO, env=env, machine=fake)):
                peer = dispatch.delegate_driver("peer")
                self.assertEqual([peer.name, dispatch.delegate_driver("nosuch"), dispatch.delegate_driver("container")], ["peer", None, None])
                with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
                    dispatch.delegate_run(peer, "status", ["ws"])
        self.assertIn("'status' acts on a workspace on peer, and peer did not answer.", err.getvalue())


class TestTheDirectoryNamesTheWorkspaceOnABuildBox(WkTest):
    def name(self, cwd, remote=True):
        marker, fleet = self.tmp / "remote-marker", self.tmp / "far-fleet"
        fleet.mkdir(exist_ok=True)
        marker.unlink(missing_ok=True)
        if remote:
            marker.write_text("inputs=x\n")
            host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip().lower()
            (fleet / "fake.conf").write_text(f"kind=build\nhostname={host}\nroot={self.tmp / 'wk'}\n")
        cp = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(REPO / 'lib')!r})\nfrom wk import dispatch\n"
                             "print(dispatch.cwd_workspace() or 'NONE')"], cwd=cwd, capture_output=True, text=True, timeout=30,
                            env={**os.environ, "WK_REMOTE_MARKER": str(marker), "WK_MACHINES_DIR": str(fleet), "PWD": str(cwd)})
        return cp.stdout.strip()

    def test_standing_in_or_under_a_workspace_names_it_and_anywhere_else_nothing(self):
        ws = self.tmp / "wk" / "ws" / "image-decoders"
        (ws / "WebKit").mkdir(parents=True)
        (self.tmp / "wk" / "cache").mkdir()
        for cwd, remote, want in ((ws, True, "image-decoders"), (ws / "WebKit", True, "image-decoders"),
                                  (self.tmp / "wk" / "cache", True, "NONE"), (ws, False, "NONE")):
            with self.subTest(cwd=cwd, remote=remote):
                self.assertEqual(want, self.name(cwd, remote))


_ASK = ("import sys; sys.path.insert(0, sys.argv[1]); from wk import places\n"
        "a = getattr(places.Registry(sys.argv[2]), sys.argv[3])(sys.argv[4])\nprint(a if isinstance(a, str) else ' '.join(a))\n")
_MACHINE_CONF = "kind=build\ndriver=remote\nhost={host}\n"
_LOCAL_CONF = "kind=build\ndriver=remote\nlocal=1\nroot={root}\nstore={store}\n"


class TestTheFleetIsWalkedOnce(WkTest):
    """Resolving a workspace's place: every machine asked at once, a local one first, a silent one named."""

    def ask(self, fn, ws, ssh, **env):
        with stub_path({"ssh": ssh}) as binp:
            return subprocess.run([sys.executable, "-c", _ASK, str(REPO / "lib"), str(REPO), fn, ws], capture_output=True, text=True,
                                  timeout=120, env=clean_env(dict(env, WK_MACHINES_DIR=str(self.tmp / "hosts"), PATH="%s:%s" % (
                                      binp, os.environ.get("PATH", "")), XDG_STATE_HOME=str(self.tmp / "state"))))

    def setUp(self):
        super().setUp()
        (self.tmp / "hosts").mkdir()

    def test_two_machines_are_asked_before_either_answers(self):
        for m in ("fakea", "fakeb"):
            (self.tmp / "hosts" / f"{m}.conf").write_text(_MACHINE_CONF.format(host=f"{m}.invalid"))
        log = self.tmp / "asked"
        barrier = ('#!/bin/sh\ncase " $* " in *" -G "*) exit 0 ;; esac\necho asked >> "$LOG"\nn=0\n'
                   'while [ "$(grep -c asked "$LOG")" -lt 2 ] && [ $n -lt 100 ]; do sleep 0.05; n=$((n + 1)); done\n'
                   'echo answered >> "$LOG"\nexit 255\n')
        cp = self.ask("locate", "no-such-workspace", barrier, LOG=str(log))
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(log.read_text().split(), ["asked", "asked", "answered", "answered"])

    def test_a_local_name_never_reaches_the_fleet_and_a_silent_machine_is_named(self):
        (self.tmp / "root" / "ws" / "here-ws").mkdir(parents=True)
        (self.tmp / "hosts" / "fakelocal.conf").write_text(_LOCAL_CONF.format(root=self.tmp / "root", store=self.tmp / "store"))
        (self.tmp / "hosts" / "fakedown.conf").write_text(_MACHINE_CONF.format(host="fakedown.invalid"))
        witness = self.tmp / "witness"
        cp = self.ask("ws_place", "here-ws", '#!/bin/sh\necho asked >> "$W"\nexit 255\n', W=str(witness))
        self.assertEqual((cp.stdout.strip(), witness.exists()), ("fakelocal", False), cp.stderr)
        cp = self.ask("ws_place", "no-such-workspace", "#!/bin/sh\nexit 255\n")
        self.assertEqual(cp.stdout.strip(), "container", cp.stderr)
        self.assertIn("could not ask", cp.stderr)
        self.assertIn("fakedown", cp.stderr)


if __name__ == "__main__":
    unittest.main()
