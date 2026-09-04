"""The deploy keys live in an ssh-agent outside every workspace.

`wk push` loads and empties an ssh-agent on the machine that runs the
workspaces; the private halves never leave this machine, and nothing moves on
disk in either direction. The properties that matter, and that this file
measures against a real `ssh-agent`:

  * the key bytes go in on STDIN -- never an argument (`ps` shows those to
    everyone on the machine) and never a file on the far side
  * `ssh-add -l` is the only thing anything reads for the switch's position
  * `off` empties it, and reports the agent's own answer rather than the exit
    status of the clear
  * an agent can hand back public keys and nothing else, which is the whole
    reason the keys are in one

Run: python3 -m unittest tests.test_push_agent -v
"""
import os
import shutil
import signal
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, requires_podman_vm

FORKS = ("fork", "forkwpe")

# An exec function that records every command line it is given and runs it
# here. Stands in for `podman machine ssh wk --` without a machine: what is
# under test is what push_agent_* *sends*, and a log of that is the evidence
# for "the key was never an argument".
FAKE_EXEC = '''
_fake_exec() {
    printf '%s\\n' "$1" >> "$WK_TEST_EXEC_LOG"
    sh -c "$1"
}
'''


def have_ssh_agent():
    return shutil.which("ssh-agent") and shutil.which("ssh-add")


@unittest.skipUnless(have_ssh_agent(), "needs ssh-agent and ssh-add")
class _Agent(WkTest):
    """A real ssh-agent in a scratch directory, and a key pair for each fork
    laid out the way this machine lays them out: private halves in the
    never-mounted directory, public halves in the mounted one."""

    def setUp(self):
        super().setUp()
        self.secrets = self.tmp / "secrets"
        self.held = self.tmp / "push-keys"
        self.store = self.tmp / "store"
        self.secrets.mkdir()
        self.held.mkdir()
        (self.store / "ws").mkdir(parents=True)
        self.log = self.tmp / "exec.log"
        self.log.write_text("")

        for fork in FORKS:
            priv = self.held / f"build_key_{fork}"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                            "-C", f"wk deploy key for {fork}", "-f", str(priv)],
                           check=True)
            shutil.move(str(priv) + ".pub", str(self.secrets / f"build_key_{fork}.pub"))

        self.sock = self.tmp / "agent.sock"
        self.agent_pid = None
        out = subprocess.run(["ssh-agent", "-s", "-a", str(self.sock)],
                             stdout=subprocess.PIPE, text=True, check=True).stdout
        for part in out.split(";"):
            if "SSH_AGENT_PID=" in part:
                self.agent_pid = int(part.split("=", 1)[1])
        self.addCleanup(self._kill_agent)

    def _kill_agent(self):
        if self.agent_pid:
            try:
                os.kill(self.agent_pid, signal.SIGTERM)
            except OSError:
                pass

    def env(self, extra=None):
        e = {
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_STORE": str(self.store),
            "WK_TEST_EXEC_LOG": str(self.log),
            "WK_PUSH_AGENT_SOCK": str(self.sock),
            "WK_PUSH_PAT_FILE": str(self.tmp / "pat"),
            "WK_PUSH_READ_PAT_FILE": str(self.tmp / "read-pat"),
            "WK_MACHINE": "wk-no-such-machine",
            "XDG_STATE_HOME": str(self.tmp / "state"),
        }
        if extra:
            e.update(extra)
        return e

    def sh(self, script, env=None):
        return bash('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/lib/store.sh"\n'
                    + FAKE_EXEC + script,
                    env=self.env(env))

    def ssh_add(self, *args):
        return subprocess.run(["ssh-add", *args],
                              env={**os.environ, "SSH_AUTH_SOCK": str(self.sock)},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)

    def exec_log(self):
        return self.log.read_text()


class TestLoading(_Agent):
    def test_the_key_bytes_go_in_on_stdin_and_are_never_an_argument(self):
        """The one property the whole arrangement rests on: a key passed as an
        argument is in `ps` for every account on that machine, and a key
        written to a file there is a key that machine now holds."""
        cp = self.sh(f'push_agent_load _fake_exec "{self.sock}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for fork in FORKS:
            self.assertIn(f"{fork} loaded", cp.stdout)

        priv = (self.held / "build_key_fork").read_text()
        secret_line = [ln for ln in priv.splitlines() if "PRIVATE KEY" not in ln][0]
        log = self.exec_log()
        self.assertNotIn(secret_line, log,
                         "a private key half reached the far side as an argument")
        self.assertNotIn("PRIVATE KEY", log)
        self.assertIn("ssh-add -", log)

    def test_the_agent_then_holds_one_identity_per_fork(self):
        self.sh(f'push_agent_load _fake_exec "{self.sock}"')
        listed = self.ssh_add("-l").stdout
        self.assertEqual(len(FORKS), len([ln for ln in listed.splitlines() if ln.strip()]),
                         listed)

    def test_an_agent_hands_back_public_keys_and_nothing_else(self):
        """`ssh-add -L` is everything an agent will ever give a client that
        asks for its identities -- so a workspace holding this socket can sign
        and can never obtain the key."""
        self.sh(f'push_agent_load _fake_exec "{self.sock}"')
        pub = self.ssh_add("-L").stdout
        self.assertIn("ssh-ed25519 ", pub)
        self.assertNotIn("PRIVATE KEY", pub)
        for line in pub.splitlines():
            if line.strip():
                self.assertTrue(line.startswith("ssh-"), line)

    def test_a_fork_with_no_private_half_is_reported_not_invented(self):
        (self.held / "build_key_forkwpe").unlink()
        cp = self.sh(f'push_agent_load _fake_exec "{self.sock}"')
        self.assertIn("fork loaded", cp.stdout)
        self.assertIn("forkwpe no-key", cp.stdout)


class TestEvidence(_Agent):
    def test_ensure_tells_an_empty_agent_from_no_agent(self):
        """`ssh-add -l` exits 1 for "no identities" and 2 for "no agent": the
        difference between the switch being off and there being no switch."""
        cp = self.sh(f'push_agent_ensure _fake_exec "{self.sock}" && echo YES || echo NO')
        self.assertIn("YES", cp.stdout, cp.stderr)
        cp = self.sh(f'push_agent_ensure _fake_exec "{self.tmp}/not-a-socket" && echo YES || echo NO')
        self.assertIn("NO", cp.stdout, cp.stderr)

    def test_list_is_fingerprints_and_comments_only(self):
        self.sh(f'push_agent_load _fake_exec "{self.sock}"')
        cp = self.sh(f'push_agent_list _fake_exec "{self.sock}"')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("SHA256:", cp.stdout)
        self.assertNotIn("PRIVATE KEY", cp.stdout)

    def test_list_of_an_empty_agent_is_empty_not_a_sentence(self):
        """'The agent has no identities.' counted as one line would make an
        empty agent read as a loaded one everywhere this is counted."""
        cp = self.sh(f'push_agent_list _fake_exec "{self.sock}"')
        self.assertEqual("", cp.stdout.strip(), cp.stdout)

    def test_clear_empties_it(self):
        self.sh(f'push_agent_load _fake_exec "{self.sock}"')
        self.sh(f'push_agent_clear _fake_exec "{self.sock}"')
        self.assertIn("no identities", self.ssh_add("-l").stdout)


class TestTheApiToken(_Agent):
    def test_the_token_is_written_and_removed_by_the_same_switch(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        pat = self.tmp / "pat"
        cp = self.sh(f'push_agent_pat_write _fake_exec "{pat}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("ghp-not-a-real-token\n", pat.read_text())
        self.assertEqual(0o600, pat.stat().st_mode & 0o777)

        self.sh(f'push_agent_pat_clear _fake_exec "{pat}"')
        self.assertFalse(pat.exists())

    def test_the_token_is_never_an_argument_either(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.sh(f'push_agent_pat_write _fake_exec "{self.tmp}/pat"')
        self.assertNotIn("ghp-not-a-real-token", self.exec_log())

    def test_writing_with_no_token_here_fails_rather_than_writing_nothing(self):
        """An empty token file would be a token file: the injector reads the
        first line and would send `Authorization: Bearer`."""
        pat = self.tmp / "pat"
        cp = self.sh(f'push_agent_pat_write _fake_exec "{pat}"')
        self.assertNotEqual(cp.returncode, 0)
        self.assertFalse(pat.exists())


class TestTheStandingReadToken(_Agent):
    """The read token is not the switch's: reading GitHub is open whatever
    position `wk push` is in, so the machine keeps a standing copy of this
    device's token and every converging call writes or removes it."""

    def read_pat(self):
        return self.tmp / "read-pat"

    def test_it_is_written_from_the_token_this_device_holds(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        cp = self.sh(f'push_agent_pat_sync _fake_exec "{self.read_pat()}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("ghp-not-a-real-token\n", self.read_pat().read_text())
        self.assertEqual(0o600, self.read_pat().stat().st_mode & 0o777)

    def test_a_token_withdrawn_here_is_removed_there_by_the_same_call(self):
        """Write-or-clear, not write-only: a `wk key set github-pat --replace`
        that stored nothing must not leave the old token on the machine."""
        self.read_pat().write_text("ghp-the-old-one\n")
        cp = self.sh(f'push_agent_pat_sync _fake_exec "{self.read_pat()}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse(self.read_pat().exists())

    def test_it_is_never_an_argument_either(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.sh(f'push_agent_pat_sync _fake_exec "{self.read_pat()}"')
        self.assertNotIn("ghp-not-a-real-token", self.exec_log())

    def test_a_far_side_that_refuses_is_reported_not_swallowed(self):
        """The caller warns on this: the machine is stopped, and a workspace
        reads nothing until the next converging call."""
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        cp = self.sh('_no_exec() { return 1; }\n'
                     f'push_agent_pat_sync _no_exec "{self.read_pat()}" && echo YES || echo NO')
        self.assertIn("NO", cp.stdout, cp.stdout + cp.stderr)

    def test_its_path_is_beside_the_switchs_and_carries_no_quotes(self):
        """It reaches `wk doctor` as user-facing text, and the far side's
        quoting is push_agent_pat_*'s job (TestAPathWithASpaceInIt)."""
        store = self.tmp / "store"
        cp = bash('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/lib/store.sh"\n'
                  'printf "[%s]\\n" "$(push_agent_machine_read_pat)"',
                  env={**self.env(), "WK_STORE": str(store),
                       "WK_PUSH_READ_PAT_FILE": ""})
        self.assertEqual(f"[{store}/read-github-pat]", cp.stdout.strip())
        self.assertNotIn("$", cp.stdout)

    def test_the_switch_does_not_touch_it(self):
        """`wk push on|off` is over writing. A read that stopped working when
        the switch was thrown would be the split not existing."""
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.read_pat().write_text("ghp-standing\n")
        for action in ("on", "off"):
            with self.subTest(action=action):
                self.run_wk("push", action, env=self.env())
                self.assertEqual("ghp-standing\n", self.read_pat().read_text())

    def test_both_host_stages_deliver_it_beside_the_injector_unit(self):
        """./setup is the other convergence point, and the one a machine made
        from scratch depends on: the unit is installed and the standing token
        goes in beside it, in that order, or a workspace reads nothing."""
        for f in ("host/macos/vmtools.sh", "host/linux/sdk.sh"):
            with self.subTest(host=f):
                text = (REPO / f).read_text()
                self.assertIn('push_agent_pat_sync push_agent_exec '
                              '"$(push_agent_machine_read_pat)"', text)
                self.assertLess(text.index("unit_start wk-github-inject.service"),
                                text.index("push_agent_pat_sync"))

    def test_the_switch_writes_and_removes_only_the_write_token(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")
        self.run_wk("push", "on", env=self.env())
        self.assertTrue((self.tmp / "pat").exists())
        self.assertFalse(self.read_pat().exists(),
                         "'wk push on' delivered the standing read token, which is "
                         "./setup's and 'wk key set github-pat's to deliver")
        self.run_wk("push", "off", env=self.env())
        self.assertFalse((self.tmp / "pat").exists())


class TestDoctorNamesTheReadToken(WkTest):
    """New machine-local state is a line in `wk doctor`'s machine-local section
    or it is a bug: that section is the checklist a reinstall works from.
    `regenerable`, because both ./setup and `wk key set github-pat` write it
    again from the token this device holds -- losing it costs nothing."""

    DOCTOR = (REPO / "cmd" / "doctor").read_text()
    ROW = 'local_state "$(push_agent_machine_read_pat)"'

    def row(self):
        for line in self.DOCTOR.splitlines():
            if line.startswith(self.ROW):
                return line
        raise AssertionError("the machine-local section does not name the read token")

    def test_it_is_regenerable_and_the_line_names_what_writes_it(self):
        line = self.row()
        self.assertIn("regenerable", line)
        self.assertIn("./setup", line)
        self.assertIn("wk key set github-pat", line)

    def test_it_is_reported_from_the_machine_and_absent_is_not_a_fault(self):
        """Driven: the real `local_state` and the real row against a scratch
        store. WK_IN_VM=1 for the reason tests/test_pi_agent.py gives -- on a
        macOS host that function forwards a store path into the podman machine,
        and doctor never starts one."""
        store = self.tmp / "store"
        store.mkdir()
        fn = self.DOCTOR[self.DOCTOR.index("local_state() { # <path> <kind>"):]
        fn = fn[:fn.index("\n}\n") + 3]
        script = ('. "$WK_ROOT/lib/common.sh"\n'
                  f'WK_STORE={store}\n'
                  '. "$WK_ROOT/lib/store.sh"\n'
                  "ok()   { printf 'ok %s\\n' \"$*\"; }\n"
                  "miss() { printf 'miss %s -> %s\\n' \"$1\" \"$2\"; }\n"
                  "unk()  { printf 'unk %s -> %s\\n' \"$1\" \"$2\"; }\n"
                  + fn + self.row() + "\n")

        cp = bash(script, env={"WK_IN_VM": "1"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertTrue(out.startswith("unk "), out)
        self.assertIn("read-github-pat", out)

        (store / "read-github-pat").write_text("ghp-not-a-real-token\n")
        cp = bash(script, env={"WK_IN_VM": "1"})
        out = cp.stdout + cp.stderr
        self.assertTrue(out.startswith("ok "), out)
        self.assertIn("regenerable", out)
        self.assertNotIn("ghp-not-a-real-token", out)


class TestAPathWithASpaceInIt(_Agent):
    """Every one of these paths becomes part of a shell command line on the
    machine that holds the workspaces, and a path is not a shell word: the
    store root is `$WK_STORE` and a person's home directory can have a space
    in it. Unquoted, `cat > $2` writes two files and `rm -f $2` removes the
    wrong one -- and the wrong one here is a credential."""

    def setUp(self):
        super().setUp()
        self.spaced = self.tmp / "a dir with spaces"
        self.spaced.mkdir()
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")

    def test_the_token_round_trips_through_a_path_with_a_space(self):
        pat = self.spaced / "push-github-pat"
        cp = self.sh(f'push_agent_pat_write _fake_exec "{pat}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("ghp-not-a-real-token\n", pat.read_text())
        self.assertEqual([pat.name], [p.name for p in self.spaced.iterdir()],
                         "the unquoted path made more than one file")

        cp = self.sh(f'if push_agent_pat_present _fake_exec "{pat}"; '
                     f'then echo present; else echo absent; fi')
        self.assertIn("present", cp.stdout, cp.stdout + cp.stderr)

        cp = self.sh(f'push_agent_pat_clear _fake_exec "{pat}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse(pat.exists())

    def test_the_resolved_path_is_what_the_store_says_and_carries_no_quotes(self):
        """It reaches `wk push status` and `wk verify` as user-facing text, so
        a shell word with its own quote characters in it is printed at a
        person -- and the far side's quoting is push_agent_pat_*'s job."""
        store = self.spaced / "store"
        cp = bash('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/lib/store.sh"\n'
                  'printf "[%s]\\n" "$(push_agent_machine_pat)"',
                  env={**self.env(), "WK_STORE": str(store), "WK_PUSH_PAT_FILE": ""})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(f"[{store}/push-github-pat]", cp.stdout.strip())
        self.assertNotIn('"', cp.stdout)
        self.assertNotIn("$", cp.stdout)


class TestTheSwitchEndToEnd(_Agent):
    """`wk push on|off|status` against the same real agent."""

    def wk(self, *args, env=None):
        return self.run_wk(*args, env=self.env(env))

    def test_on_loads_off_empties_and_status_reads_the_agent(self):
        (self.held / "github-pat").write_text("ghp-not-a-real-token\n")

        cp = self.wk("push", "status")
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("push is OFF", cp.stdout)

        cp = self.wk("push", "on")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("push is ON", cp.stdout)
        self.assertEqual(len(FORKS),
                         len([ln for ln in self.ssh_add("-l").stdout.splitlines() if ln.strip()]))
        self.assertTrue((self.tmp / "pat").exists())

        cp = self.wk("push", "status")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("push allowed (in the agent)", cp.stdout)

        cp = self.wk("push", "off")
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("push is OFF", cp.stdout)
        self.assertIn("no identities", self.ssh_add("-l").stdout)
        self.assertFalse((self.tmp / "pat").exists())

    def test_the_private_halves_never_move(self):
        """Nothing moves on disk: a switch that moved files could be killed
        between the two positions and leave a key in the mounted directory."""
        before = sorted(p.name for p in self.held.iterdir())
        self.wk("push", "on")
        self.wk("push", "off")
        self.assertEqual(before, sorted(p.name for p in self.held.iterdir()))
        self.assertEqual([], [p.name for p in self.secrets.iterdir()
                              if p.name.startswith("build_key_")
                              and not p.name.endswith(".pub")])

    def test_status_reports_an_exposed_private_half_in_the_mounted_directory(self):
        """Nothing here ever puts one there, so one that is there is readable
        by every workspace and this is the only place that would notice."""
        (self.secrets / "build_key_strays").write_text("not a key\n")
        cp = self.wk("push", "status")
        self.assertIn("readable by every workspace", cp.stdout)
        self.assertIn("build_key_strays", cp.stdout)

    def test_on_without_an_agent_refuses_and_names_the_remedy(self):
        cp = self.wk("push", "on", env={"WK_PUSH_AGENT_SOCK": str(self.tmp / "nope.sock")})
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no ssh-agent answers", cp.stdout)
        self.assertIn("wk-ssh-agent.service", cp.stdout)

    def test_off_reports_the_agents_own_answer_not_the_clears_exit_status(self):
        """A `ssh-add -D` that returned 0 against an agent that ignored it
        would otherwise report a switch that is not thrown."""
        src = (REPO / "cmd" / "push").read_text()
        body = src[src.index("\noff)\n"):src.index("\nstatus)\n")]
        self.assertIn("push_agent_list", body)
        self.assertLess(body.index("push_agent_clear"), body.index("push_agent_list"))


class TestAMachineWithNoAgentSaysSoRatherThanHeldBack(_Agent):
    """A build box is a plain checkout with no container around it, so it names
    no ssh-agent socket and `remote/provision.sh` points its `core.sshCommand`
    straight at the private half. The key in `push-keys` is therefore *live*
    there whatever any switch says -- and `status` reported it as "held back",
    which is what `wk push off --all` then aggregated into a fleet reported as
    off while that box could still push.

    Installing an agent there is owed work (docs/HANDOFF-sandboxing.md); until
    it lands, saying so is the honest report."""

    MACHINE = "wk-test-buildbox"

    def env(self, extra=None):
        """A machine that *is* a build box: a `remote` target of its own, which
        names no t_agent_sock (lib/target.sh's default is `return 1`), and a
        `.wk-remote` marker saying so -- which is what remote/provision.sh
        leaves behind and what makes `default_target` that target here."""
        registry = self.tmp / "registry"
        registry.mkdir(exist_ok=True)
        (registry / f"{self.MACHINE}.conf").write_text(
            "WK_TARGET_KIND=remote\n"
            "WK_REMOTE_HOST=nonexistent.invalid\n"
            f"WK_REMOTE_ROOT={self.tmp}\n")
        marker = self.tmp / "wk-remote"
        marker.write_text(f"target={self.MACHINE}\nroot={self.tmp}\n")
        return super().env({"WK_TARGET_REGISTRY": str(registry),
                            "WK_REMOTE_MARKER": str(marker),
                            **(extra or {})})

    def test_status_says_the_switch_does_not_exist_and_the_key_is_live(self):
        cp = self.run_wk("push", "status", env=self.env())
        out = cp.stdout
        self.assertIn("always live", out)
        self.assertNotIn("held back (", out)
        self.assertIn("no ssh-agent socket", out)
        self.assertIn("HANDOFF-sandboxing.md", out)

    def test_that_position_is_on_so_a_fleet_is_not_reported_as_off(self):
        """cmd/ai reads `push_switch status || return 0` as a closed switch, so
        exit 1 here is the sentence "nothing can push" about a machine that
        can."""
        cp = self.run_wk("push", "status", env=self.env())
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("cannot be switched off", cp.stdout)

    def test_off_refuses_here_rather_than_claiming_to_have_thrown_it(self):
        cp = self.run_wk("push", "off", env=self.env())
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no ssh-agent socket", cp.stdout)
        self.assertIn("HANDOFF-sandboxing.md", cp.stdout)
        self.assertNotIn("push is OFF", cp.stdout)

    def test_on_refuses_here_too(self):
        cp = self.run_wk("push", "on", env=self.env())
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no ssh-agent socket", cp.stdout)

    def test_a_machine_that_does_have_one_still_says_held_back(self):
        """The container target names a socket, so nothing above changes the
        report on the machine the switch actually works on."""
        cp = self.run_wk("push", "status", env=super().env())
        self.assertIn("held back", cp.stdout)
        self.assertNotIn("always live", cp.stdout)
        self.assertEqual(1, cp.returncode, cp.stdout)


class TestTheConfigEveryWorkspaceIncludes(_Agent):
    def test_on_and_off_both_write_it(self):
        """A workspace that cannot resolve the fork alias fails with a hostname
        error, a long way from the actual state of the switch -- so the config
        is regenerated in both positions."""
        for action in ("on", "off"):
            with self.subTest(action=action):
                (self.secrets / "ssh_config").unlink(missing_ok=True)
                self.run_wk("push", action, env=self.env())
                text = (self.secrets / "ssh_config").read_text()
                self.assertIn("Host github-webkit", text)
                self.assertIn("IdentityAgent /run/wk/ssh-agent.sock", text)
                self.assertIn("IdentityFile /secrets/build_key_fork.pub", text)
                self.assertIn("IdentitiesOnly yes", text)

    def test_it_names_a_public_half_and_never_a_private_one(self):
        self.run_wk("push", "on", env=self.env())
        for line in (self.secrets / "ssh_config").read_text().splitlines():
            if line.strip().startswith("IdentityFile"):
                self.assertTrue(line.strip().endswith(".pub"), line)

    def test_the_account_name_goes_beside_it_for_the_injector(self):
        self.run_wk("push", "on", env=self.env())
        self.assertEqual("justinmichaud",
                         (self.secrets / "github-user").read_text().strip())


class TestTheSourceHasNoMoveLeft(unittest.TestCase):
    def test_the_switch_names_no_file_move(self):
        """A second way to expose a key is a second thing to get wrong, so the
        switch has exactly one: the agent's contents."""
        src = (REPO / "cmd" / "push").read_text()
        for gone in ("move_keys", "purge_copies", "unreachable_copies", "$MOVED"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def test_firstrun_links_no_key_and_includes_the_config(self):
        text = (REPO / "container" / "firstrun.sh").read_text()
        self.assertNotIn("/secrets/build_key_", text)
        self.assertIn("Include /secrets/ssh_config", text)

    def test_the_container_target_names_the_socket_in_the_mounted_directory(self):
        text = (REPO / "targets" / "container.sh").read_text()
        self.assertIn("t_agent_sock() { echo /run/wk/ssh-agent.sock; }", text)
        self.assertIn("--volume $rt:/run/wk", text)

    def test_a_target_with_no_agent_refuses_rather_than_guessing(self):
        cp = bash('. "$WK_ROOT/lib/target.sh"; t_agent_sock && echo YES || echo NO')
        self.assertIn("NO", cp.stdout)

    def test_both_hosts_install_the_agent_unit(self):
        """One body, installed by both (tests/test_host_units.py holds the
        installer itself); what matters here is that the unit each host puts
        on its machine runs an ssh-agent on the socket every container
        bind-mounts."""
        for f in ("host/macos/vmtools.sh", "host/linux/sdk.sh"):
            with self.subTest(host=f):
                self.assertIn("unit_start wk-ssh-agent.service ",
                              (REPO / f).read_text())
        body = (REPO / "host" / "units" / "wk-ssh-agent.service").read_text()
        self.assertIn("ssh-agent -a %t/wk/ssh-agent.sock", body)


def _machine(cmd, timeout=60):
    return subprocess.run(["podman", "machine", "ssh", "wk", "--", cmd],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)


@requires_podman_vm()
class TestLiveAgentInTheMachine(unittest.TestCase):
    """The one thing no stub can answer: whether this machine's own agent is
    where every container's mount says it is. Read-only -- the switch is not
    thrown here, because a test that leaves push on is worse than a test that
    does not run.

    Skipped with the remedy when the machine has not been set up for this yet:
    the unit is installed by ./setup, and a machine made before it has no
    agent at all.
    """

    def test_the_unit_is_installed_and_the_socket_is_in_the_mounted_directory(self):
        cp = _machine("systemctl --user cat wk-ssh-agent.service >/dev/null 2>&1 && echo YES || echo NO")
        if "YES" not in cp.stdout:
            self.skipTest("the machine has no wk-ssh-agent.service yet:  ./setup --stage sdk")
        cp = _machine("systemctl --user is-active wk-ssh-agent.service")
        self.assertIn("active", cp.stdout, cp.stdout)
        cp = _machine('test -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/ssh-agent.sock" '
                      '&& echo SOCKET || echo NONE')
        self.assertIn("SOCKET", cp.stdout, cp.stdout)

    def test_ssh_add_answers_there_which_is_what_the_switch_reads(self):
        cp = _machine("systemctl --user cat wk-ssh-agent.service >/dev/null 2>&1 && echo YES || echo NO")
        if "YES" not in cp.stdout:
            self.skipTest("the machine has no wk-ssh-agent.service yet:  ./setup --stage sdk")
        # 0 = holds identities, 1 = empty, 2 = no agent. Only 2 is a fault.
        cp = _machine('SSH_AUTH_SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/ssh-agent.sock" '
                      'ssh-add -l >/dev/null 2>&1; echo rc=$?')
        self.assertRegex(cp.stdout, r"rc=[01]\b", cp.stdout)

    def test_the_injector_publishes_its_ca_where_a_container_reads_it(self):
        cp = _machine("systemctl --user cat wk-github-inject.service >/dev/null 2>&1 && echo YES || echo NO")
        if "YES" not in cp.stdout:
            self.skipTest("the machine has no wk-github-inject.service yet:  ./setup --stage sdk")
        cp = _machine('test -s "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/wk-github-ca.pem" '
                      '&& echo CA || echo NONE')
        self.assertIn("CA", cp.stdout, cp.stdout)
        # And its own socket is *not* in that directory: a workspace must reach
        # the injector through the egress policy, not around it.
        cp = _machine('test -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/github-inject.sock" '
                      '&& echo EXPOSED || echo NO')
        self.assertIn("NO", cp.stdout, cp.stdout)


@requires_podman_vm()
@unittest.skipUnless(os.environ.get("WK_TEST_LIVE_PUSH") == "1",
                     "throws the real switch; set WK_TEST_LIVE_PUSH=1 to run it")
class TestLivePushFromAContainer(unittest.TestCase):
    """`wk push on`, then a real `git ls-remote` from a workspace through the
    agent. Opt-in because it throws this machine's own switch; the position it
    found is restored either way."""

    def setUp(self):
        self.was_on = subprocess.run([str(REPO / "wk"), "push", "status"],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL).returncode == 0
        self.addCleanup(self._restore)

    def _restore(self):
        subprocess.run([str(REPO / "wk"), "push", "on" if self.was_on else "off"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _workspaces(self):
        cp = subprocess.run([str(REPO / "wk"), "ls"], stdout=subprocess.PIPE,
                            text=True, timeout=120)
        return [ln.split()[0] for ln in cp.stdout.splitlines()[1:] if ln.split()]

    def test_a_container_can_push_through_the_agent_while_on(self):
        names = self._workspaces()
        if not names:
            self.skipTest("no workspace here to push from ('wk new <name>')")
        ws = names[0]
        cp = subprocess.run([str(REPO / "wk"), "push", "on"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=180)
        self.assertEqual(0, cp.returncode, cp.stdout)
        alias = subprocess.run(
            ["bash", "-c", ". lib/common.sh; . lib/store.sh; "
                           "wk_push_forks | awk 'NF{print $3; exit}'"],
            cwd=str(REPO), stdout=subprocess.PIPE, text=True).stdout.strip()
        cp = subprocess.run([str(REPO / "wk"), "enter", ws,
                             "git", "ls-remote", f"git@{alias}:", "HEAD"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=180)
        self.assertEqual(0, cp.returncode, cp.stdout)
