"""How a workspace reaches the outside: what the proxy allows, and how the
variables naming that proxy get into a macOS guest's shells.

Two halves of one boundary. `container/proxy/wk-proxy.py` decides what may be
reached and is shared by containers and guests alike, so an entry widens both.
`~/.wk-egress` carries the variables naming that proxy into a guest. The host
writes it on every start and vm/shell-rc.sh sources it from all four rc files,
so every shell that reads an rc gets it -- not login shells alone: an editor's
terminal pane is not a login shell, and a pane with no proxy has no egress at
all and reports it as every host in the world being unreachable.

Run: python3 -m unittest tests.test_egress -v
"""
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import assert_guest_start_converges, REPO, WkTest

PROXY = REPO / "container" / "proxy" / "wk-proxy.py"
RC = REPO / "shell" / "bashrc"
SHELL_RC = REPO / "vm" / "shell-rc.sh"

# The six a proxied machine has to export. curl and git read the lowercase
# pair, some tools only the uppercase, and no_proxy keeps a workspace's own
# loopback services off the proxy.
VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "no_proxy", "NO_PROXY")


def _policy():
    spec = importlib.util.spec_from_file_location("wkproxy", str(PROXY))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Policy(tempfile.mkdtemp(prefix="wk-test-store-"))


class TestDevelopmentAllowlist(unittest.TestCase):
    """The hosts ordinary development needs, each measured as a refusal before
    it was added. Suffix matching is on a dot boundary, so one entry covers the
    CDN subdomain a registry actually serves from -- and covers nothing else."""

    def test_package_registries_are_reachable(self):
        p = _policy()
        for host in ("registry.npmjs.org", "formulae.brew.sh", "ghcr.io",
                     "crates.io", "static.crates.io", "static.rust-lang.org",
                     "sh.rustup.rs"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertTrue(ok, why)

    def test_xcode_and_software_update_are_reachable(self):
        p = _policy()
        for host in ("developer.apple.com", "download.developer.apple.com",
                     "swscan.apple.com", "swcdn.apple.com",
                     "updates.cdn-apple.com", "mesu.apple.com",
                     "gdmf.apple.com"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertTrue(ok, why)

    def test_certificate_validation_works_on_80(self):
        """Gatekeeper will not launch a downloaded binary without a
        notarization check, and OCSP and CRL are http by design."""
        p = _policy()
        for host in ("valid.apple.com", "ocsp.apple.com", "crl.apple.com",
                     "i.pki.goog"):
            with self.subTest(host=host):
                self.assertTrue(p.host_allowed(host, 80)[0])
                self.assertTrue(p.host_allowed(host, 443)[0])

    def test_apple_is_named_host_by_host_not_by_suffix(self):
        """The refusals a guest produces are mostly iCloud, Siri, Spotlight,
        ads and news. None of that is development, and an `apple.com` suffix
        would have swept it all in."""
        p = _policy()
        for host in ("gsa.apple.com", "gateway.icloud.com",
                     "weatherkit.apple.com", "iadsdk.apple.com",
                     "api-spotlight-ausw2b.smoot.apple.com"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertFalse(ok, why)

    def test_a_registry_lookalike_is_still_refused(self):
        """The match is on a dot boundary, not a substring."""
        p = _policy()
        for host in ("evilregistry.npmjs.org.attacker.net", "notcrates.io",
                     "ghcr.io.example.com"):
            with self.subTest(host=host):
                self.assertFalse(p.host_allowed(host, 443)[0])

    def test_the_widening_is_declared_for_the_audit(self):
        """docs/HANDOFF-sandboxing.md audits the allowlist; every widening
        says so in the file, where the next person reading it will look."""
        text = PROXY.read_text()
        block = text[text.index('"registry.npmjs.org"'):]
        self.assertIn("SANDBOX AUDIT", text[:text.index('"registry.npmjs.org"')][-2000:])
        self.assertIn("BLOCKED_NETS", text)
        self.assertTrue(block)


EGRESS = (
    "# wk: written by targets/vm.sh on every start\n"
    "export http_proxy=http://192.168.2.1:3128\n"
    "export https_proxy=http://192.168.2.1:3128\n"
    "export HTTP_PROXY=http://192.168.2.1:3128\n"
    "export HTTPS_PROXY=http://192.168.2.1:3128\n"
    "export no_proxy=localhost,127.0.0.1,::1\n"
    "export NO_PROXY=localhost,127.0.0.1,::1\n"
)

LEGACY = (
    "# wk-tools: egress goes through the proxy on the host; Softnet denies the rest.\n"
    "export http_proxy=http://192.168.64.1:3128\n"
    "export https_proxy=http://192.168.64.1:3128\n"
    "export HTTP_PROXY=http://192.168.64.1:3128\n"
    "export HTTPS_PROXY=http://192.168.64.1:3128\n"
    "export no_proxy=localhost,127.0.0.1,::1\n"
    "export NO_PROXY=localhost,127.0.0.1,::1\n"
)


def _wire(home, times=1):
    """Run the guest's shell wiring, as targets/vm.sh streams it in on every
    start. More than once to prove a second start changes nothing."""
    for _ in range(times):
        cp = subprocess.run(
            ["bash", str(SHELL_RC), str(REPO)],
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60,
        )
        assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


def _shell_vars(shell, home, args):
    """Every proxy variable a shell started with `args` ends up with. The rc
    files are read for real -- no `source` by hand -- because which of them a
    given invocation reads is the whole question."""
    printer = "; ".join(f'echo "{v}=${v}"' for v in VARS)
    cp = subprocess.run(
        [shell, *args, printer],
        cwd=str(REPO),
        env={"HOME": str(home), "TERM": "dumb",
             "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
    )
    out = {}
    for line in cp.stdout.splitlines():
        k, _, v = line.partition("=")
        if k in VARS:
            out[k] = v
    return out


class TestGuestProxyEnvironment(WkTest):
    """Which shells in a guest have egress. The pane is the case that was
    broken; the others must not regress while fixing it."""

    # What a person and what `wk` actually start, and how each is spelled.
    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh (the guest's own window)": ("zsh", ["-l", "-c"]),
        "bash -lc (every t_exec)": ("bash", ["-lc"]),
        "interactive bash": ("bash", ["-i", "-c"]),
    }

    def _home(self, egress=True):
        h = self.tmp / "home"
        h.mkdir(exist_ok=True)
        if egress:
            (h / ".wk-egress").write_text(EGRESS)
        _wire(h)
        return h

    def test_every_shell_that_reads_an_rc_has_the_proxy(self):
        home = self._home()
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = _shell_vars(shell, home, args)
                self.assertEqual(got.get("http_proxy"), "http://192.168.2.1:3128", got)
                self.assertEqual(got.get("HTTPS_PROXY"), "http://192.168.2.1:3128", got)
                self.assertEqual(got.get("NO_PROXY"), "localhost,127.0.0.1,::1", got)

    def test_a_guest_with_no_egress_file_gets_no_proxy(self):
        """An unfiltered guest, and the moment before the host has written it.
        Absence has to mean absence rather than a stale default."""
        home = self._home(egress=False)
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = _shell_vars(shell, home, args)
                for v in VARS:
                    self.assertEqual(got.get(v), "", f"{v} set from nothing: {got}")

    def test_the_wiring_is_idempotent(self):
        """It runs on every start; a guest started fifty times has one of each
        line, not fifty."""
        home = self.tmp / "home"
        home.mkdir()
        (home / ".wk-egress").write_text(EGRESS)
        _wire(home, times=3)
        for rc in (".zshrc", ".zprofile", ".bash_profile", ".bashrc"):
            text = (home / rc).read_text()
            with self.subTest(rc=rc):
                self.assertEqual(1, text.count('if [ -r "$HOME/.wk-egress" ]; then'), text)
                self.assertEqual(1, text.count("shell/bashrc"), text)


class TestGuestConvergence(WkTest):
    """A guest cloned from a base that baked the address into its profiles
    converges on the next start, rather than needing the base rebuilt."""

    def test_a_stale_baked_in_address_is_stripped_and_replaced(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".zprofile").write_text(LEGACY)
        (home / ".bash_profile").write_text(LEGACY + 'echo "mine stays"\n')
        (home / ".wk-egress").write_text(EGRESS)

        _wire(home, times=2)

        for rc in (".zprofile", ".bash_profile", ".zshrc", ".bashrc"):
            text = (home / rc).read_text()
            with self.subTest(rc=rc):
                self.assertNotIn("192.168.64.1", text, text)
                self.assertNotIn("egress goes through", text, text)
                self.assertIn(".wk-egress", text, text)
        # Only wk's own stanza goes; anything else in the file is the guest's.
        self.assertIn("mine stays", (home / ".bash_profile").read_text())

    @unittest.skipUnless(shutil.which("zsh"), "no zsh on this machine")
    def test_the_converged_guest_ends_up_with_the_live_address(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / ".zprofile").write_text(LEGACY)
        (home / ".wk-egress").write_text(EGRESS)
        _wire(home)
        got = _shell_vars("zsh", home, ["-l", "-c"])
        self.assertEqual(got.get("https_proxy"), "http://192.168.2.1:3128", got)


class TestNothingBakesTheAddressIn(unittest.TestCase):
    """One writer for one fact. The address is the host's own on the guest
    bridge and changes, so provisioning may not record it."""

    def test_provisioning_writes_no_proxy_exports(self):
        text = (REPO / "vm" / "provision-base.sh").read_text()
        self.assertNotIn("export http_proxy", text)
        self.assertNotIn("WK_VM_PROXY_ADDR", text)

    def test_the_start_path_writes_the_egress_file(self):
        """One `_converge_guest`, called from both t_start arms: a guest that
        was already running gets its egress written exactly like one this
        start booted."""
        self.assertIn(".wk-egress", (REPO / "targets" / "vm.sh").read_text())
        assert_guest_start_converges(self, '_set_guest_egress "$name" "$ip"')


if __name__ == "__main__":
    unittest.main()
