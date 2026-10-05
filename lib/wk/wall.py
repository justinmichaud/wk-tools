"""The wall around a workspace, measured from inside it; every check runs at once."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import credcheck
from wk import claudelogin, project, reach, secrets
from wk.store import no_such_workspace
from wk.act import die
from wk.doctor import MISS, miss, note, ok
from wk.resources import arch_has_gpu

# Named places, never the checkout: it ships PEM fixtures.
KEY_SCAN = ("{ grep -rl 'PRIVATE KEY' $HOME/.ssh $HOME/.claude /secrets /run/wk;\n"
            "  grep -l  'PRIVATE KEY' $HOME/* ; } 2>/dev/null | head -5")
HOST_PATHS = ("/host/home", "/host/run", "/run/user/*/bus", "/run/dbus/system_bus_socket")
PUBLISHING = ("push-keys", "github-write", "bugzilla-write")
CSI = re.compile(r"\x1b\[[0-9;?<>=]*[A-Za-z]|\x1b[78]|\x1b\([A-Z]|\x0f")
COMMIT_WALL_PATHS = ("objects", "refs", "logs", "HEAD", "packed-refs", "index.lock", "ORIG_HEAD")

CURL = "curl -sS -m 40 --suppress-connect-headers -D -"
# Where a claude.ai token could be read in a workspace: the login, the CLI's state, the mounts.
CLAUDE_TOKEN_SCAN = ("grep -rlE 'sk-ant-o[ar]t[0-9]+-[A-Za-z0-9_-]{40}' \"$HOME/%s\" $HOME/.claude $HOME/.claude.json "
                     "/secrets /run/wk 2>/dev/null | head -5" % secrets.LOGIN_DIR)
LOGIN_REMEDY = ("a container is given the placeholder when it is made ('wk rm %s' and 'wk new'), a guest at every "
                "'wk start %s'")


def placeholders():
    """The PR tool sends two of these, gh the other."""
    return ("GITHUB_COM_TOKEN", "GH_TOKEN", project.get("BUGZILLA_ENV")[1])


def http(url, extra=""):
    return "%s -o /dev/null -w '%%{http_code}' %s%s 2>/dev/null" % (CURL, extra, url)


def fault_of(reply):
    """The reason in the injector's `X-Wk-Injector` header: it answers its own TLS or DNS failure toward the upstream with a 502 that carries one."""
    m = re.search(r"(?im)^x-wk-injector:[ \t]*(.+?)\s*$", reply)
    return m.group(1) if m else ""


def status_of(reply):
    return reply.rsplit("\n", 1)[-1].strip()


def upstream_gap(name, remedy, *replies):
    """The injector's 502/504 without the header is the far end down; with it, the injector failing toward the far end."""
    for r in replies:
        if reason := fault_of(r):
            return [miss("%s: the injector failed to verify or resolve the host (%s) -- a fault in the injector, not the upstream" % (name, reason), remedy)]
    bad = next((c for c in map(status_of, replies) if c in ("502", "504")), None)
    if bad is None:
        return []
    return [note("%s did not answer through the injector (HTTP %s) -- an upstream outage, not the sandbox" % (name, bad))]


def claude_status(text):
    """`claude auth status` read through whatever control sequences a TTY wrapped it in: absent | unreadable <bytes> | <loggedIn> <authMethod>."""
    if "wk-no-claude-cli" in text:
        return "absent"
    clean = CSI.sub("", text).replace("\r", "")
    try:
        doc, _ = json.JSONDecoder().raw_decode(clean[clean.index("{"):])
    except ValueError:
        return "unreadable %r" % text.encode("utf-8", "surrogateescape")[:80]
    return "%s %s" % (doc.get("loggedIn"), doc.get("authMethod"))


def commit_walled(driver):
    return driver.kind == "container" or (driver.kind == "local" and driver.os() != "macos")


def commit_wall_prefix(root, src):
    """bwrap's read-only binds resist unmount, remount, shadowing and nested user namespaces (tests/test_commit_wall.py)."""
    ro = []
    for p in COMMIT_WALL_PATHS:
        ro += ["--ro-bind-try", "%s/.git/%s" % (src, p), "%s/.git/%s" % (src, p)]
    return ["bwrap", "--dev-bind", "/", "/"] + ro + ["--"]


def run_at_once(checks):
    """(name, rows) in the order given; a check that raises is reported as unmeasured, never as passed."""
    with ThreadPoolExecutor(max_workers=max(1, len(checks))) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in checks]
    out = []
    for name, fut in futures:
        try:
            rows = list(fut.result())
        except Exception as e:
            rows = [miss("the '%s' probe died before it reported anything (%s), so what it measures is unmeasured" % (name, e),
                         "run it again to ask a second time")]
        out.append((name, rows))
    return out


class Wall:
    def __init__(self, root, driver, ws, machine, push_on=0, want_gpu=False):
        self.root = root
        self.driver = driver
        self.ws = ws
        self.machine = machine
        self.push_on = push_on
        self.want_gpu = want_gpu

    def inside(self, cmd):
        """The container exec path appends \\r, which every numeric probe would then test as "2\\r"."""
        return self.driver.exec(self.ws, ["bash", "-lc", cmd]).out.replace("\r", "").rstrip("\n")

    def github(self):
        code = status_of(self.inside(http("https://github.com/")))
        if code == "200":
            return [ok("github reachable through the proxy (HTTP %s)" % code)]
        return [miss("github unreachable through the proxy (got '%s')" % (code or "nothing"), self.driver.daemon_remedy(self.ws, "proxy"))]

    def allowlist(self):
        denied = self.inside("curl -sS -m 15 -o /dev/null https://example.com/ 2>&1")
        if "403" in denied:
            return [ok("a host outside the allowlist is refused")]
        return [miss("example.com was NOT refused", "the allowlist is not being enforced")]

    def off_allowlist(self):
        rows = []
        lan = self.inside("curl -sS -m 10 -o /dev/null -w '%{http_code}' http://192.168.1.1/ 2>&1 | tail -1")
        if "403" in lan or "000" in lan:
            rows.append(ok("the local network is refused"))
        else:
            rows.append(miss("reaching the LAN gateway returned '%s'" % lan, "the local network must be refused"))
        direct = self.inside("curl -sS --noproxy '*' -m 8 -o /dev/null -w '%{http_code}' https://1.1.1.1/ 2>&1 | tail -1")
        if not direct or any(w in direct for w in ("000", "Could not resolve", "Failed to connect", "Network is unreachable")):
            rows.append(ok("no direct egress with the proxy bypassed"))
        else:
            rows.append(miss("direct egress succeeded, bypassing the proxy: '%s'" % direct, "everything must go through the proxy"))
        return rows

    def softwareupdate(self):
        scan = self.inside("curl -sS -m 12 -o /dev/null https://swscan.apple.com/ 2>&1; "
                           "curl -sS -m 12 -o /dev/null https://gdmf.apple.com/v2/pmv 2>&1")
        if len([l for l in scan.splitlines() if "403" in l]) == 2:
            return [ok("the softwareupdate scan path is refused, so nothing offers this guest an upgrade")]
        return [miss("swscan/gdmf are reachable from in here: '%s'" % " ".join(scan.splitlines()),
                     "softwareupdated finds an upgrade and Setup Assistant puts its pane in front of the window; "
                     "no guest can turn the check off (vm/desktop.sh)")]

    def isolation(self):
        rows = []
        # iproute2 is not in the SDK image.
        ifaces = self.inside("awk -F: 'NR>2 {gsub(/ /,\"\",$1); printf \"%s \", $1}' /proc/net/dev")
        if ifaces.replace(" ", "") == "lo":
            rows.append(ok("no network interface but loopback (%s)" % ifaces))
        elif not ifaces.strip():
            rows.append(miss("could not enumerate interfaces inside the workspace", "/proc/net/dev did not answer"))
        else:
            rows.append(miss("workspace has network interfaces: %s" % ifaces, "a workspace has loopback only"))
        found = [f for f in (self.inside("ls -d %s 2>/dev/null | head -1" % p) for p in HOST_PATHS) if f]
        rows += [miss("host path visible inside the workspace: %s" % f, "nothing of the host is mounted in") for f in found]
        if not found:
            rows.append(ok("no host home, runtime directory or D-Bus socket"))
        return rows

    def commit_wall(self):
        if "WKBWRAP" not in self.inside("command -v bwrap >/dev/null 2>&1 && echo WKBWRAP"):
            return [miss("no bwrap in the workspace", "'wk ai claude' cannot wall off commits and refuses to start")]
        probe = self.inside('''set -e
            D=$(mktemp -d /tmp/wk-wall.XXXXXX); cd "$D"
            git init -q; git config user.email a@b; git config user.name a
            echo hi > f; git add f; git commit -qm one >/dev/null
            W="%s"
            echo two >> f
            $W sh -c "git add f && git commit -qm two" >/dev/null 2>&1 && echo COMMITTED || echo BLOCKED
            $W sh -c "echo three >> f" >/dev/null 2>&1 && echo WROTE || echo NOWRITE
            rm -rf "$D"''' % " ".join(commit_wall_prefix(self.root, "$D")))
        if "BLOCKED" not in probe:
            return [miss("commit wall did NOT block a commit under bwrap: '%s'" % " ".join(probe.splitlines()), "lib/wk/wall.py, commit_wall_prefix")]
        if "WROTE" not in probe:
            return [miss("commit wall blocks an ordinary write too", "too much: lib/wk/wall.py, commit_wall_prefix")]
        return [ok("commit wall blocks a commit; an ordinary write still works (bwrap)")]

    def no_credentials_inside(self):
        rows = []
        material = self.inside(KEY_SCAN)
        if material:
            rows.append(miss("private key material inside the workspace: %s" % " ".join(material.splitlines()), "remove it"))
        else:
            rows.append(ok("no private key material in the workspace (~/.ssh, ~/.claude, the top of home, /secrets, /run/wk)"))
        for var in placeholders():
            # Whether it is set, never its value.
            tok = self.inside('printf %%s "${%s:-}"' % var)
            if tok == "wk-injects-this":
                rows.append(ok("%s in the workspace is the placeholder" % var))
            elif not tok:
                rows.append(miss("%s is unset in here, so nothing that reads it sends a credential for the injector to replace" % var,
                                 "container/proxy/ensure-bridge.sh exports all three, from /secrets/github-user, "
                                 "/secrets/bugzilla-user and the injector's CA; 'wk key push on' rewrites /secrets"))
            else:
                rows.append(miss("%s in the workspace is not the placeholder: something put a real credential in here" % var,
                                 "find it and remove it"))
        ghcred = self.inside("test -e ~/.config/gh/hosts.yml && echo ~/.config/gh/hosts.yml; "
                             "env | grep -E '^(GITHUB_TOKEN|GH_ENTERPRISE_TOKEN)=' | cut -d= -f1")
        if ghcred:
            rows.append(miss("GitHub credential inside the workspace: %s" % " ".join(ghcred.splitlines()),
                             "rm ~/.config/gh/hosts.yml; unset the token, before an agent runs"))
        else:
            rows.append(ok("no stored GitHub credential inside: gh has only the placeholder"))
        return rows

    def secrets_view(self):
        rows, kept = [], []
        for row in secrets.agent_secrets():
            if self.driver.kind in row[4].split(","):
                continue
            path = "/secrets/" + row[1]
            if self.inside("test -r %s && echo yes" % path) == "yes":
                rows.append(miss("%s is readable in '%s' and the delivery table gives %s to no %s place" % (path, self.ws, row[0], self.driver.kind),
                                 "what a workspace mounts holds only what it is given (Secrets.publish_view, lib/wk/secrets.py)"))
            else:
                kept.append(row[0])
        if kept:
            rows.append(ok("no credential this kind is not given is readable in here (%s)" % " ".join(kept)))
        return rows

    def agent_identities(self):
        sock = self.driver.agent_sock()
        if not sock:
            return [miss("the '%s' place names no ssh-agent socket, so a push from in here would use a credential wk does not control"
                         % self.driver.name, "lib/wk/places.py, %s.agent_sock" % type(self.driver).__name__)]
        # An empty agent prints "The agent has no identities." on stdout.
        ident = self.inside("command -v ssh-add >/dev/null 2>&1 "
                            "&& (SSH_AUTH_SOCK=%s ssh-add -l 2>/dev/null | grep -v 'has no identities' | grep -c . || true) "
                            "|| echo MISSING" % sock)
        if ident == "MISSING":
            return [miss("no ssh-add in the workspace, so what the agent holds cannot be measured from in here",
                         "a push from in here would not work either")]
        n = int(ident) if ident.isdigit() else 0
        if self.push_on == 1:
            if n > 0:
                return [ok("%d deploy key(s) reach this workspace through %s, and push is on" % (n, sock))]
            return [miss("push is ON but no identity reaches %s in here" % sock, "wk key push on")]
        if n == 0:
            return [ok("no identity reaches this workspace (%s is empty): a push is refused" % sock)]
        return [miss("%d identity/identities reach this workspace, and the host does not say push is on" % n, "wk key push off")]

    def push_here(self):
        alias = next((r[2] for r in secrets.forks()), "")
        sock = reach.ssh_g(self.machine, alias).get("identityagent", "")
        ident = 0
        if sock:
            r = self.machine.run(["env", "SSH_AUTH_SOCK=" + sock, "ssh-add", "-l"])
            ident = len([l for l in r.out.splitlines() if l]) if r.ok else 0
        if ident == 0:
            return [ok("the agent holds nothing, so a push from in here has no identity to offer")]
        return [miss("%d deploy key(s) reach this workspace through %s, so an agent in here could push" % (ident, sock),
                     "the switch is the host's:  wk key push off")]

    def github_read(self):
        """GET / answers 200 unauthenticated and 401 only for a token GitHub refuses, which is the injector's own standing one."""
        rroot, ruser = self.inside(http("https://api.github.com/")), self.inside(http("https://api.github.com/user"))
        if gap := self.gap("GitHub", rroot, ruser):
            return gap
        root, user = status_of(rroot), status_of(ruser)
        if root == "200" and user == "200":
            return [ok("a read is authenticated (HTTP 200) from a token this workspace never holds")]
        if root == "200" and user == "401":
            return [note("reads are unauthenticated (HTTP 401): no standing read token where the injector reads one, so a workspace "
                         "sees public GitHub at 60 requests an hour ('wk key set github-pat', then './setup')")]
        if root == "401":
            return [note("GitHub refused the standing read token the injector holds (HTTP 401), so every API read in here is refused "
                         "with it -- 'gh', and '%s pr', which reports it as a token of its own being out of date. A token this "
                         "machine holds and GitHub still accepts ('wk key check github-pat') means the injector was handed an older one: "
                         "'wk start %s' converges a guest's copy and './setup' the podman machine's. One refused there too is "
                         "'wk key set github-pat --replace'" % (project.get("PR_TOOL"), self.ws))]
        if root == "200":
            return [miss("a read answered '%s' rather than 200 or 401" % (user or "nothing"),
                         "the injector is in the path but not answering for api.github.com/user")]
        return [miss("api.github.com answered '%s' -- the injector is not in the path" % (root or "nothing"),
                     self.driver.daemon_remedy(self.ws, "inject"))]

    def github_write(self):
        """An empty body names no branch, so 422 is the authenticated answer and no pull request is created."""
        fork = next((r[1] for r in secrets.forks()), "")
        pulls = "https://api.github.com/repos/%s/pulls" % fork
        reply = self.inside(http(pulls, "-X POST -d '{}' "))
        if gap := self.gap("GitHub", reply):
            return gap
        code = status_of(reply)
        if self.push_on != 1:
            if code == "412":
                return [ok("a write is refused by the injector (HTTP 412), which names 'wk key push on'")]
            return [miss("POST /repos/%s/pulls answered '%s' where the host does not say push is on -- expected 412, the injector's own refusal"
                         % (fork, code or "nothing"),
                         "a 401 is an injector still running older code, which 'wk status' reports and './setup' on that machine "
                         "restarts; anything else is a write token still on the machine:  wk key push off")]
        if code == "422":
            return [ok("a write is authenticated (HTTP 422, nothing created), and push is on")]
        if code == "403":
            return [miss("push is ON and the token reached GitHub, which refused it: the stored PAT has no 'Pull requests: write' on %s" % fork,
                         "reissue it with that permission and 'wk key set github-pat --replace'")]
        if code == "401":
            return [miss("push is ON but POST /repos/%s/pulls answered 401: the injector has no write token, or one GitHub refuses" % fork,
                         "'wk key push on' again for the first; 'wk key set github-pat --replace', then './setup' for a spent, revoked or "
                         "expired PAT. 'wk key push status' says which of the two the machine is in")]
        return [miss("push is ON but POST /repos/%s/pulls answered '%s' rather than 422" % (fork, code or "nothing"),
                     "the injector has no write token ('wk key push on' again)")]

    def agent_credential(self):
        """`claude auth status` is local (measured 2026-09-10: loggedIn for a token Anthropic has never seen)."""
        rows = []
        login = self.driver.kind in secrets.LOGIN_KINDS
        secret, want = ("placeholder claude.ai login", "claude.ai") if login else ("claude", "oauth_token")
        remedy = LOGIN_REMEDY % (self.ws, self.ws) if login else self.driver.agent_secret_remedy(self.ws, "claude")
        token = self.inside('printf %s "${CLAUDE_CODE_OAUTH_TOKEN:+set}"')
        if login and token == "set":
            rows.append(miss("$CLAUDE_CODE_OAUTH_TOKEN is set in this workspace as well as the placeholder claude.ai login, and the token wins: "
                             "every session authenticates as an inference-only credential and remote control refuses to start",
                             "nothing should put it here (%s/shell/bashrc exports it only where the delivery column sends it); "
                             "'wk rm %s' and 'wk new' remake the workspace without it" % (self.driver.tools(self.ws), self.ws)))
        elif not login and token != "set":
            rows.append(miss("no $CLAUDE_CODE_OAUTH_TOKEN in this workspace, which is the one Claude credential a %s place is given"
                             % self.driver.kind, remedy))
        verdict = claude_status(self.inside("if command -v claude >/dev/null 2>&1; then claude auth status 2>/dev/null; "
                                            "else echo wk-no-claude-cli; fi"))
        if verdict == "True " + want:
            rows.append(ok("the agent in '%s' is authenticated (%s), from the %s credential this machine delivered -- no /login, no browser"
                           % (self.ws, want, secret)))
        elif verdict.startswith("True "):
            rows.append(miss("the agent in '%s' authenticates as %s, where a %s place is given %s and must report %s"
                             % (self.ws, verdict[5:], self.driver.kind, secret, want), "two credentials reach it, or the wrong one does"))
        elif verdict == "absent":
            rows.append(miss("no 'claude' on $PATH in '%s', so no session there can run at all" % self.ws,
                             "'wk enter %s' then 'claude --version'. Whether an agent there can authenticate is unmeasured" % self.ws))
        elif verdict.startswith("unreadable"):
            rows.append(miss("'claude auth status' in '%s' answered something that is not a report, so whether an agent there can "
                             "authenticate is unmeasured. It answered (first 80 bytes): %s" % (self.ws, verdict[len("unreadable "):]),
                             "the CLI is too old to have the subcommand (it self-updates; 'wk enter %s' then 'claude --version'), "
                             "or what it wrote is not the JSON this reads" % self.ws))
        else:
            rows.append(miss("'claude auth status' in '%s' says it is not logged in, so every session there stops at /login" % self.ws,
                             remedy))
        return rows

    def claude_login(self):
        """The workspace holds the placeholder and no token; the placeholder, sent, comes back authenticated by the injector."""
        rows = []
        try:
            oauth = json.loads(self.inside('cat "$HOME/%s/.credentials.json" 2>/dev/null' % secrets.LOGIN_DIR))["claudeAiOauth"]
            held = {oauth.get("accessToken"), oauth.get("refreshToken")} == {claudelogin.PLACEHOLDER}
        except (ValueError, KeyError, TypeError, AttributeError):
            held = False
        if held:
            rows.append(ok("the claude.ai login in here is the placeholder the injector swaps"))
        else:
            rows.append(miss("~/%s/.credentials.json in '%s' is not the placeholder login" % (secrets.LOGIN_DIR, self.ws),
                             LOGIN_REMEDY % (self.ws, self.ws)))
        found = self.inside(CLAUDE_TOKEN_SCAN)
        if found:
            rows.append(miss("a claude.ai token is readable in '%s': %s" % (self.ws, " ".join(found.splitlines())),
                             "the injector is its one holder: remove it, and 'wk key check claude-login' outside says "
                             "whether the login still works"))
        else:
            rows.append(ok("no claude.ai token is readable in here (the login, ~/.claude, /secrets, /run/wk)"))
        reply = self.inside("%s -w '\n%%{http_code}' -H 'Authorization: Bearer %s' -H 'anthropic-version: 2023-06-01' "
                            "'https://api.anthropic.com/v1/models?limit=1' 2>/dev/null" % (CURL, claudelogin.PLACEHOLDER))
        if gap := self.gap("Anthropic", reply):
            return rows + gap
        code = status_of(reply)
        said = next((l for l in reply.splitlines() if "wk credential injector" in l), "")
        if code == "200":
            rows.append(ok("the placeholder login is authenticated through the injector (HTTP 200), from a login no workspace holds"))
        elif code == "401" and said:
            rows += [note("no claude.ai session here can authenticate: " + said.strip()),
                     note("'wk key check claude-login' says what this machine holds")]
        elif code == "401":
            rows.append(miss("Anthropic refused the claude.ai login the injector put on a request (HTTP 401)",
                             "'wk key check claude-login'; 'wk key set claude-login --replace' stores a new one"))
        else:
            rows.append(miss("api.anthropic.com answered '%s' -- the injector is not in the path for it" % (code or "nothing"),
                             self.driver.daemon_remedy(self.ws, "inject")))
        return rows

    def pr_tool_check(self):
        return [("pr-tool-setup", self.pr_tool_setup)] if self.driver.repo(self.ws).pr_tool else []

    def pr_tool_setup(self):
        tool, key = project.get("PR_TOOL"), project.get("PR_TOOL_SETUP")
        if self.inside("git -C %s config --get %s" % (self.driver.src(self.ws), key)) == "true":
            return [ok("%s is set up in '%s' (hooks, fork remote verified)" % (tool, self.ws))]
        return [miss("'%s setup' has not completed in '%s' (%s is not true): `%s pr` prompts or refuses" % (tool, self.ws, key, tool),
                     "'wk sync %s --fix' re-asserts the remotes and runs it" % self.ws)]

    def gap(self, name, *replies):
        return upstream_gap(name, self.driver.daemon_remedy(self.ws, "inject"), *replies)

    def bugzilla_read(self):
        reply = self.inside(http(project.get("BUGZILLA") + "/rest/version"))
        if gap := self.gap("Bugzilla", reply):
            return gap
        code = status_of(reply)
        if code == "200":
            return [ok("%s reachable through the injector (HTTP %s)" % (credcheck.bugzilla_host(), code))]
        return [miss("%s answered '%s' -- the injector is not in the path for it" % (credcheck.bugzilla_host(), code or "nothing"),
                     self.driver.daemon_remedy(self.ws, "inject"))]

    def bugzilla_write(self):
        """Bugzilla names its own refusal in the body: 410 "log in first", 306 an unknown key, else an empty bug refused."""
        post = "-X POST -H 'Content-Type: application/json' -d '{}' "
        if self.push_on != 1:
            # Nothing reaches Bugzilla here, so the status is the injector's own.
            reply = self.inside(http(project.get("BUGZILLA") + "/rest/bug", post))
            if gap := self.gap("Bugzilla", reply):
                return gap
            code = status_of(reply)
            if code == "412":
                return [ok("a Bugzilla write is refused by the injector (HTTP 412), which names 'wk key push on'")]
            return [miss("POST /rest/bug answered '%s' where the host does not say push is on -- expected 412, the injector's own refusal"
                         % (code or "nothing"),
                         "Bugzilla's own 'log in first' is an injector still running older code, which 'wk status' reports and "
                         "'./setup' on that machine restarts; anything else is a Bugzilla key still on the machine:  wk key push off")]
        reply = self.inside("%s %s%s/rest/bug 2>/dev/null" % (CURL, post, project.get("BUGZILLA")))
        headers, _, body = reply.partition("\n\n") if reply.startswith("HTTP/") else ("", "", reply)
        if "wk credential injector" in body or fault_of(headers):
            return self.gap("Bugzilla", headers + "\n" + ("504" if "did not answer" in body else "502"))
        try:
            code = str(json.loads(body).get("code", ""))
        except (ValueError, AttributeError):
            code = ""
        if code == "410":
            return [miss("push is ON but POST /rest/bug answered 'log in first' (410): the injector has no Bugzilla API key",
                         "'wk key set bugzilla-api-key', then 'wk key push on' again")]
        if code == "306":
            return [miss("push is ON and the key reached Bugzilla, which does not know it (306)",
                         "'wk key set bugzilla-api-key --replace', then 'wk key push on' again")]
        if not code:
            return [miss("POST /rest/bug answered nothing Bugzilla-shaped", "the injector is in the path but not answering for " + credcheck.bugzilla_host())]
        return [ok("a Bugzilla write is authenticated (error %s: an empty bug, nothing filed), and push is on" % code)]

    def gpu(self):
        """gpu-probe.sh exits 0 hardware, 1 software only, 2 no EGL, 3 build failed."""
        arch = self.driver.arch(self.ws)
        if not arch_has_gpu(arch):
            rows = [note("no GPU: an %s workspace gets none (the NVIDIA userspace is aarch64-only)" % arch)]
            if self.want_gpu:
                rows.append(miss("--gpu on an %s workspace, which cannot have one" % arch, "a native workspace"))
            return rows
        if self.driver.os() == "macos":
            return [note("a guest's GPU is Virtualization.framework's, reached through Metal, and this probe is EGL: what a benchmark "
                         "in there gets is measured by the benchmark, and its desktop rows above say whether its window is covered")]
        r = self.driver.exec(self.ws, ["bash", "-lc", os.path.join(self.driver.tools(self.ws), "container", "gpu", "gpu-probe.sh")])
        rows = [note(l) for l in (r.out + r.err).replace("\r", "").splitlines()]
        if r.rc == 0:
            return rows + [ok("GPU acceleration available")]
        what = "only software rendering inside the workspace" if r.rc == 1 else "no usable EGL inside the workspace (probe exit %d)" % r.rc
        if self.want_gpu:
            return rows + [miss(what, "a benchmark on it would measure the software renderer")]
        return rows + [note(what + (" -- benchmarks would measure llvmpipe" if r.rc == 1 else ""))]

    def rootless_proxy(self):
        rows = []
        if self.driver.rootless() == "true":
            rows.append(ok("podman is rootless"))
        else:
            rows.append(miss("podman is NOT rootless", "an escape from this container is an escape as root"))
        if self.machine.run(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"]).ok:
            rows.append(ok("egress proxy running"))
        else:
            rows.append(miss("egress proxy is not running", self.driver.daemon_remedy(self.ws, "proxy")))
        written = self.inside("touch /opt/wk-tools/.wk-write-probe 2>&1")
        if "read-only" in written.lower() or "permission denied" in written.lower():
            rows.append(ok("/opt/wk-tools is read-only"))
        else:
            self.inside("rm -f /opt/wk-tools/.wk-write-probe")
            rows.append(miss("/opt/wk-tools is writable from inside the workspace", "it is mounted read-only (lib/wk/places.py's Container)"))
        return rows

    def from_host(self):
        checks = []
        if self.driver.egress_filtered(self.ws):
            checks += [("egress-github", self.github), ("egress-allowlist", self.allowlist), ("egress-off-allowlist", self.off_allowlist)]
            if self.driver.os() == "macos":
                checks.append(("egress-softwareupdate", self.softwareupdate))
        if self.driver.kind == "container":
            checks.append(("isolation", self.isolation))
        if commit_walled(self.driver):
            checks.append(("commit-wall", self.commit_wall))
        if self.driver.kind in secrets.LOGIN_KINDS:
            checks.append(("claude-login", self.claude_login))
        return checks + [("no-credentials-inside", self.no_credentials_inside), ("secrets-view", self.secrets_view),
                         ("agent-identities", self.agent_identities), ("github-read", self.github_read),
                         ("github-write", self.github_write), ("bugzilla-read", self.bugzilla_read),
                         ("bugzilla-write", self.bugzilla_write), *self.pr_tool_check(),
                         ("agent-credential", self.agent_credential), ("gpu", self.gpu)]

    def from_inside(self):
        checks = [("push-keys", self.push_here), ("github-read", self.github_read), ("github-write", self.github_write),
                  ("bugzilla-read", self.bugzilla_read), ("bugzilla-write", self.bugzilla_write), ("egress-github", self.github),
                  ("egress-allowlist", self.allowlist), ("egress-off-allowlist", self.off_allowlist),
                  ("no-credentials", self.no_credentials_inside), *self.pr_tool_check()]
        if commit_walled(self.driver):
            checks.append(("commit-wall", self.commit_wall))
        return checks


def verdict(rep, publishing=False):
    return "publishing" if publishing else "broken" if rep.missing else "intact"


def push_verdict(rc):
    """push_on (1, 0 or None, unmeasured) from `wk key push status`'s exit code."""
    return {0: 1, 1: 0, 4: 0}.get(rc)


def push_switch(root, machine):
    rc = machine.run([os.path.join(root, "wk"), "key", "push", "status"]).rc
    push_on = push_verdict(rc)
    if push_on == 1:
        return 1, "the host says push is ON"
    if push_on == 0:
        return 0, "the host says push is OFF"
    return None, ("the host could not measure the switch ('wk key push status' exited %d), so what reaches this workspace "
                  "is measured below and compared to nothing" % rc)


def from_host(root, driver, ws, machine, rep, want_gpu=False):
    """Out of the parallel pass: the push switch, which two checks read, and the write probe, which cleans up after itself."""
    if driver.kind == "remote":
        die("'wk doctor %s' proves a sandbox holds, and a remote place has none:\n"
            "    a plain checkout on a shared machine, no container, no firewall, no\n"
            "    disposable layer. There is nothing here to measure and nothing it\n"
            "    promised. 'wk ai claude' puts a barrier in front of this place rather\n"
            "    than a measurement, for the same reason." % ws)
    state = driver.info(ws)
    if state == "absent":
        die(no_such_workspace(ws))
    rows = [ok("workspace running") if state == "running" else miss("workspace state: %s" % state, "wk start %s" % ws)]
    push_on, said = push_switch(root, machine)
    rows.append(note(said))
    if driver.kind == "vm" and not driver.egress_filtered(ws):
        rows.append(miss("this guest was booted with WK_VM_UNFILTERED, so it has the open network",
                         "wk stop %s && wk start %s   (without that variable)" % (ws, ws)))
    if driver.kind == "vm":
        rows += driver.check_rows(ws)
    rep.rows(rows)
    wall = Wall(root, driver, ws, machine, push_on, want_gpu)
    for _, found in run_at_once(wall.from_host()):
        rep.rows(found)
    if driver.kind == "container":
        rep.rows(wall.rootless_proxy())


def from_inside(root, driver, ws, machine, rep):
    """`wk doctor` in a workspace: every row into `rep`, and whether an agent in here could publish."""
    results = run_at_once(Wall(root, driver, ws, machine, 0, False).from_inside())
    for _, found in results:
        rep.rows(found)
    return any(r[0] == MISS for name, found in results if name in PUBLISHING for r in found)
