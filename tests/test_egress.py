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
INJECT = REPO / "container" / "proxy" / "github-inject.py"
RC = REPO / "shell" / "bashrc"
SHELL_RC = REPO / "vm" / "shell-rc.sh"

# The six a proxied machine has to export. curl and git read the lowercase
# pair, some tools only the uppercase, and no_proxy keeps a workspace's own
# loopback services off the proxy.
VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "no_proxy", "NO_PROXY")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _policy():
    return _load(PROXY, "wkproxy").Policy(tempfile.mkdtemp(prefix="wk-test-store-"))


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

    def test_each_widening_is_declared_for_the_audit(self):
        """docs/HANDOFF-sandboxing.md audits the allowlist; every widening
        says so in the file, where the next person reading it will look."""
        text = PROXY.read_text()
        self.assertIn("SANDBOX AUDIT", text[:text.index('"registry.npmjs.org"')][-2000:])
        self.assertIn("BLOCKED_NETS", text)


class TestTheApiGoesToTheInjector(unittest.TestCase):
    """api.github.com is the one host whose TLS is not tunnelled: its CONNECT
    goes to the credential injector, which puts the real token in the
    Authorization header so that no workspace has to hold it."""

    def test_the_api_is_allowed_on_443_and_named_as_the_injectors(self):
        ok, why = _policy().host_allowed("api.github.com", 443)
        self.assertTrue(ok, why)
        self.assertIn("injector", why)

    def test_and_on_no_other_port(self):
        """A tunnel on 22 or 80 would be a way past the injector, and the
        generic `github.com` suffix would grant both if this were not an exact
        match checked before it."""
        p = _policy()
        for port in (22, 80, 9418):
            with self.subTest(port=port):
                ok, why = p.host_allowed("api.github.com", port)
                self.assertFalse(ok, why)

    def test_the_upload_api_is_still_refused(self):
        ok, why = _policy().host_allowed("uploads.github.com", 443)
        self.assertFalse(ok)
        self.assertIn("refused", why)

    def test_a_lookalike_is_neither_injected_nor_allowed(self):
        p = _policy()
        for host in ("evilapi.github.com.attacker.net",
                     "api.github.com.attacker.net",
                     "notapi.github.com.evil.example"):
            with self.subTest(host=host):
                ok, why = p.host_allowed(host, 443)
                self.assertFalse(ok, f"{host}: {why}")

    def test_github_itself_and_codeload_are_untouched(self):
        p = _policy()
        for host, port in (("github.com", 443), ("github.com", 22),
                           ("codeload.github.com", 443),
                           ("raw.githubusercontent.com", 443)):
            with self.subTest(host=host, port=port):
                ok, why = p.host_allowed(host, port)
                self.assertTrue(ok, f"{host}:{port} {why}")
                self.assertNotIn("injector", why)

    def test_the_injected_host_is_routed_to_a_socket_under_the_store(self):
        """Never under $XDG_RUNTIME_DIR/wk: that is the directory every
        container bind-mounts, and a workspace must reach the injector through
        this policy rather than around it."""
        m = _load(PROXY, "wkproxy")
        self.assertNotIn("/wk/", m.INJECT_SOCKET.replace("/var/lib/wk/", ""))
        self.assertTrue(m.INJECT_SOCKET.endswith("github-inject.sock"))
        text = PROXY.read_text()
        self.assertIn("asyncio.open_unix_connection(INJECT_SOCKET)", text)

    def test_the_widening_is_declared_for_the_audit(self):
        self.assertIn("SANDBOX AUDIT", PROXY.read_text())


class TestTheInjectorsRule(unittest.TestCase):
    """The header rewrite, driven directly: no socket, no network, no GitHub."""

    def setUp(self):
        self.m = _load(INJECT, "wkinject")

    def head(self, extra=b""):
        return (b"GET /user HTTP/1.1\r\n"
                b"Host: api.github.com\r\n"
                b"Authorization: Basic d2s6d2staW5qZWN0cy10aGlz\r\n"
                b"Connection: keep-alive\r\n"
                b"User-Agent: python-requests/2.31.0" + extra)

    def test_the_real_token_replaces_whatever_the_workspace_sent(self):
        out = self.m.rewrite_head(self.head(), "ghp-not-a-real-token")
        self.assertIn(b"Authorization: Bearer ghp-not-a-real-token", out)
        self.assertNotIn(b"Basic", out)
        self.assertEqual(1, out.count(b"Authorization:"))

    def test_the_request_line_and_the_other_headers_survive(self):
        out = self.m.rewrite_head(self.head(), "ghp-x")
        self.assertTrue(out.startswith(b"GET /user HTTP/1.1\r\n"))
        self.assertIn(b"Host: api.github.com", out)
        self.assertIn(b"User-Agent: python-requests/2.31.0", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"))

    def test_with_no_token_the_placeholder_is_stripped_and_nothing_added(self):
        """GitHub then answers for itself -- 200 for a public endpoint, 401 for
        one that needs an account -- which is what `wk verify` measures either
        side of the switch. Forwarding the placeholder would only ever be a way
        to smuggle a credential out."""
        out = self.m.rewrite_head(self.head(), "")
        self.assertNotIn(b"Authorization", out)
        self.assertNotIn(b"Basic", out)

    def test_the_connection_is_closed_per_request(self):
        """One request per TLS connection, so GitHub closes and the client
        learns it from GitHub's own response: no response header is parsed
        here, and no keep-alive stream is left half-rewritten."""
        out = self.m.rewrite_head(self.head(), "ghp-x")
        self.assertIn(b"Connection: close", out)
        self.assertEqual(1, out.count(b"Connection:"))
        self.assertNotIn(b"keep-alive", out)

    def test_a_smuggled_proxy_credential_is_dropped_too(self):
        head = self.head(b"\r\nProxy-Authorization: Basic zzz")
        out = self.m.rewrite_head(head, "ghp-x")
        self.assertNotIn(b"Proxy-Authorization", out)

    def test_the_token_is_read_from_the_file_on_every_request(self):
        """`wk push off` removes it, and the next request must go
        unauthenticated rather than be served from something remembered."""
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "pat")
            self.assertEqual("", self.m.read_token(path))
            Path(path).write_text("ghp-not-a-real-token\n")
            self.assertEqual("ghp-not-a-real-token", self.m.read_token(path))
            Path(path).unlink()
            self.assertEqual("", self.m.read_token(path))

    def test_only_one_host_is_ever_terminated(self):
        self.assertEqual("api.github.com", self.m.INJECT_HOST)
        self.assertEqual(443, self.m.INJECT_PORT)

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_it_makes_its_own_ca_and_publishes_only_the_public_half(self):
        """`cryptography` is absent from the macOS host's python, so the
        material is made with the CLI that is on both machines."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            chain = self.m.ensure_certs(str(d / "certs"), str(d / "out" / "ca.pem"))
            published = (d / "out" / "ca.pem").read_text()
            self.assertIn("BEGIN CERTIFICATE", published)
            self.assertNotIn("PRIVATE KEY", published)
            self.assertEqual(0o644, (d / "out" / "ca.pem").stat().st_mode & 0o777)
            self.assertEqual(0o600, (d / "certs" / "ca.key").stat().st_mode & 0o777)
            self.assertEqual(0o600, (d / "certs" / "leaf.key").stat().st_mode & 0o777)
            self.assertEqual(2, Path(chain).read_text().count("BEGIN CERTIFICATE"))

            # A second run keeps it: the CA is what every workspace was told to
            # trust, so regenerating it would break every one of them.
            again = (d / "certs" / "ca.crt").read_text()
            self.m.ensure_certs(str(d / "certs"), str(d / "out" / "ca.pem"))
            self.assertEqual(again, (d / "certs" / "ca.crt").read_text())

    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI")
    def test_the_leaf_is_for_that_host_and_is_not_a_ca(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.m.ensure_certs(str(d / "certs"), str(d / "out" / "ca.pem"))
            out = subprocess.run(["openssl", "x509", "-noout", "-text",
                                  "-in", str(d / "certs" / "leaf.crt")],
                                 stdout=subprocess.PIPE, text=True, check=True).stdout
            self.assertIn("DNS:api.github.com", out)
            self.assertIn("CA:FALSE", out)


class TestTheWorkspaceHoldsThePlaceholder(unittest.TestCase):
    """Both targets set the same two variables and the same CA bundle, from the
    one wrapper each of them already goes through."""

    def test_a_container_gets_them_from_ensure_bridge(self):
        text = (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", text)
        self.assertIn("GITHUB_COM_USERNAME", text)
        for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            with self.subTest(var=var):
                self.assertIn(var, text)

    def test_the_bundle_is_the_systems_plus_the_ca(self):
        """Those variables replace the trust store outright, so a bundle
        holding one certificate would fail every other HTTPS request."""
        text = (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        self.assertIn("cat /etc/ssl/certs/ca-certificates.crt", text)

    def test_a_guest_gets_them_with_its_egress(self):
        text = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", text)
        for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            with self.subTest(var=var):
                self.assertIn(var, text)

    def test_no_real_token_is_anywhere_in_the_tree(self):
        """The placeholder is the only thing that may be written down: the
        token itself is in wk_push_held_dir on one machine."""
        for f in (REPO / "container" / "proxy" / "ensure-bridge.sh",
                  REPO / "targets" / "vm.sh",
                  REPO / "container" / "proxy" / "github-inject.py"):
            with self.subTest(f=f.name):
                self.assertNotIn("ghp_", f.read_text())


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
