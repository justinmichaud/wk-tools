"""The credentials: where this machine keeps them, the agent and injector files `wk push` switches, and
/secrets, what every workspace here reads."""

import os
import sys

from wk import act, shell
from wk.act import die, warn
from wk.machine import Local
from wk.store import Store

AGENT_SOCK = "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wk/ssh-agent.sock"
CONTAINER_SOCK = "/run/wk/ssh-agent.sock"
PUBLIC = ("ssh_config", "github-user", "bugzilla-user")
PUBLISHED = ("ssh_config", "github-user", "view/container/ssh_config")
FORKS = (("fork", "justinmichaud/WebKit", "github-webkit"),
         ("forkwpe", "justinmichaud/WPEWebKit", "github-wpe"))
AGENT_SECRETS = (("claude", "claude-token", ".wk-agent-token", "CLAUDE_CODE_OAUTH_TOKEN", "value", "remote"),
                 ("litellm", "litellm-key", ".wk-litellm-key", "LITELLM_API_KEY", "value", "container,vm,remote"),
                 ("claude-login", ".credentials.json", ".claude/.credentials.json", "-", "file", "container,vm"))
CONFIG_HEADER = """# wk: written by 'wk push on|off' (lib/wk/secrets.py). One alias per fork, because GitHub takes one deploy
# key per repository and both forks live on github.com. The identity is a public half; the private one is
# in an ssh-agent outside this workspace, and whether it is loaded there is what 'wk push' switches.
"""


def forks():
    return [list(r) for r in FORKS]


def agent_secrets():
    return [list(r) for r in AGENT_SECRETS]


def first_line(text):
    return (text or "").split("\n", 1)[0].rstrip("\r")


def alias_blocks(forks, d, prefix="build_key_", sock="", proxy=""):
    """IdentityFile carries no `.pub`: named with it, OpenSSH 10 loads that path as the private key (10.2p1)."""
    out = []
    for fork, _repo, alias in forks:
        out.append("\nHost %s\n    HostName github.com\n    User git\n    StrictHostKeyChecking accept-new\n" % alias)
        if d:
            out.append("    IdentityFile %s/%s%s\n    IdentitiesOnly yes\n" % (d, prefix, fork))
        if sock:
            out.append("    IdentityAgent %s\n" % sock)
        if proxy:
            out.append("    ProxyCommand %s\n" % proxy)
    return "".join(out)


class Secrets:
    def __init__(self, root, env=None, machine=None, macos=None, host_side=False):
        self.root = str(root)
        self.host_side = host_side
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.store = Store(self.env)
        self.macos = os.uname().sysname == "Darwin" if macos is None else macos
        self.planned = {}
        self.planned_dirs = set()

    def secrets_dir(self):
        return self.store.secrets_dir()

    def owned_here(self):
        """Inside the podman VM the secrets directory is the macOS host's, mounted read-only."""
        return not self.env.get("WK_IN_VM")

    def held_dir(self):
        return self.store.push_held_dir()

    def push_key_path(self, fork):
        return os.path.join(self.held_dir(), "build_key_" + fork)

    def pub_path(self, fork):
        return os.path.join(self.secrets_dir(), "build_key_%s.pub" % fork)

    def github_pat_path(self):
        return os.path.join(self.held_dir(), "github-pat")

    def bugzilla_key_path(self):
        return os.path.join(self.held_dir(), "bugzilla-api-key")

    def agent_secrets(self):
        return agent_secrets()

    def cred_path(self, name):
        home = self.env.get("HOME") or os.path.expanduser("~")
        fixed = {"github-pat": self.github_pat_path, "bugzilla-api-key": self.bugzilla_key_path,
                 "ntfy": self.store.ntfy_topic_path,
                 "tailnet": lambda: self.env.get("WK_TS_AUTHKEY") or os.path.join(home, ".config", "wk", "tailscale-authkey"),
                 "tailnet-api": lambda: self.env.get("WK_TS_API_SECRET") or os.path.join(home, ".config", "wk", "tailscale-api-key")}
        if name in fixed:
            return fixed[name]()
        row = next((r for r in self.agent_secrets() if r[0] == name), None)
        if row is None:
            return None
        return os.path.join(self.store.agent_rw_dir() if row[4] == "file" else self.secrets_dir(), row[1])

    def read(self, path):
        """Every byte, "" when absent, None when lib/secretfile.py refused it (a link or a shared inode)."""
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "secretfile.py"), "read", path], input="")
        sys.stderr.write(r.err)
        return r.out if r.ok else None

    def cred_read(self, name):
        path = self.cred_path(name)
        return self.read(path) if path else ""

    def cred_stored(self, name):
        """True or False, None where lib/secretfile.py refused the file."""
        path = self.cred_path(name)
        if not path:
            return False
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "secretfile.py"), "present", path], input="")
        sys.stderr.write(r.err)
        return True if r.rc == 0 else False if r.rc == 1 else None

    def check_value(self, name, value, *extra):
        """lib/credcheck.py's verdict line on `value` as <name>; a Bugzilla key is judged with its login as evidence."""
        args = ["check", name, "--repos", "".join(r[1] + " " for r in self.forks())] + list(extra)
        if name == "bugzilla-api-key":
            args += ["--evidence", "login=" + (self.bugzilla_user() or "")]
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "credcheck.py")] + args, input=value)
        sys.stderr.write(r.err)
        return r.out.rstrip("\n")

    def cred_verdict(self, name):
        path = self.cred_path(name)
        value = self.read(path)
        if value is None:
            return "bad\tthe file at %s could not be read; the refusal above says why" % path
        return self.check_value(name, value, "--path", path)

    def agent_secret_remedy(self, name):
        """What this machine's store owes a workspace missing <name>."""
        if not self.cred_stored(name):
            return "this machine's store holds no %s: 'wk key set %s' puts one there" % (name, name)
        verdict, _, detail = self.cred_verdict(name).partition("\t")
        if verdict == "bad":
            return ("this machine's store holds a %s no workspace can use (%s): 'wk key set %s --replace' makes a new one"
                    % (name, detail.splitlines()[0] if detail else "", name))
        return ("this machine's store holds a usable %s that this workspace was made without: "
                "'wk rm' and 'wk new' remake it with one" % name)

    def forks(self):
        return forks()

    def github_user(self):
        return self.forks()[0][1].split("/")[0]

    def bugzilla_user(self):
        r = self.machine.run(["git", "-C", self.store.mirror(), "cat-file", "-p", "main:metadata/contributors.json"], input="")
        if not r.ok:
            return None
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "contributors.py"), "bugzilla-login", self.github_user()],
                             input=r.out)
        if not r.ok:
            return None
        return r.out.strip() or None

    def machine_sock(self):
        return self.env.get("WK_PUSH_AGENT_SOCK") or AGENT_SOCK

    def _machine_file(self, var, name):
        return self.env.get(var) or os.path.join(self.store.root(), name)

    def machine_pat(self):
        return self._machine_file("WK_PUSH_PAT_FILE", "push-github-pat")

    def machine_read_pat(self):
        return self._machine_file("WK_PUSH_READ_PAT_FILE", "read-github-pat")

    def machine_bugzilla_key(self):
        return self._machine_file("WK_PUSH_BUGZILLA_KEY_FILE", "push-bugzilla-api-key")

    def agent_argv(self, line):
        """A shell line on that machine: here, or in the podman VM when its store is not this process's to write."""
        if self.host_side or self.store.is_local():
            return ["sh", "-c", line]
        return ["podman", "machine", "ssh", self.store.podman_machine(), "--", line]

    def _ask(self, line):
        return self.machine.run(self.agent_argv(line), input="")

    def _act(self, line, input=""):
        return self.machine.act_run(self.agent_argv(line), input=input)

    def agent_answers(self, sock):
        """`ssh-add -l` exits 1 for an empty agent and 2 for none."""
        rc = "".join(c for c in self._ask("SSH_AUTH_SOCK=%s ssh-add -l >/dev/null 2>&1; echo $?" % sock).out if c.isdigit())
        return rc in ("0", "1")

    def agent_list(self, sock):
        out = self._ask("SSH_AUTH_SOCK=%s ssh-add -l 2>/dev/null" % sock).out.replace("\r", "")
        return [line for line in out.splitlines() if line.strip() and "has no identities" not in line]

    def agent_load(self, sock):
        """(fork, loaded | no-key | FAILED) per fork; the key goes in on stdin, never an argument."""
        rows = []
        for fork in [f[0] for f in self.forks()]:
            key = (self.read(self.push_key_path(fork)) or "").rstrip("\n")
            if not key:
                rows.append((fork, "no-key"))
                continue
            ok = self._act("SSH_AUTH_SOCK=%s ssh-add - >/dev/null 2>&1" % sock, input=key + "\n").ok
            rows.append((fork, "loaded" if ok else "FAILED"))
        return rows

    def agent_clear(self, sock):
        return self._act("SSH_AUTH_SOCK=%s ssh-add -D >/dev/null 2>&1" % sock).ok

    def cred_write(self, path, name):
        """The first line of a held credential, down a pipe: an empty file would be a token the injector sends."""
        value = first_line(self.cred_read(name))
        if not value:
            return False
        return self._act("umask 077 && cat > %s" % shell.sh_quote(path), input=value + "\n").ok

    def cred_clear(self, path):
        return self._act("rm -f %s" % shell.sh_quote(path)).ok

    def cred_present(self, path):
        return self._ask("test -s %s && echo yes" % shell.sh_quote(path)).out.strip() == "yes"

    def cred_sync(self, path, name):
        if first_line(self.cred_read(name)):
            return self.cred_write(path, name)
        return self.cred_clear(path)

    def switch_cred_converge(self, sock, path, name):
        """Only while the agent holds a key: writing one into a machine whose agent is empty would be turning push on."""
        if not self.agent_list(sock):
            return True
        return self.cred_sync(path, name)

    def pat_converge_machine(self):
        if not self.cred_sync(self.machine_read_pat(), "github-pat"):
            warn("the injector in the podman machine did not take the read token; './setup' converges it")

    def pat_deliver(self):
        """Every injector this machine runs: a token delivered to one of the two is a 401 from the other."""
        ok = self.cred_sync(self.machine_read_pat(), "github-pat")
        if self.macos:
            from wk import guest
            ok = guest.pat_converge(self.root, self.env, self.machine) and ok
        return ok

    def ensure_dir(self, path, mode):
        if not self.made_dir(path):
            self.machine.mkdir(path)
            self.machine.act_run(["chmod", mode, path])
            if act.dry_run():
                self.planned_dirs.add(path)

    def made_dir(self, path):
        return path in self.planned_dirs or self.machine.isdir(path)

    def text(self, path):
        """What a file holds, None when absent; under --dry-run, what this run would have left there."""
        if path in self.planned:
            return self.planned[path]
        try:
            return self.machine.read(path)
        except OSError:
            return None

    def converge_file(self, path, text, mode):
        if self.text(path) == text:
            return
        self.machine.write(path, text)
        self.machine.act_run(["chmod", mode, path])
        if act.dry_run():
            self.planned[path] = text

    def drop(self, path):
        if self.text(path) is not None:
            self.machine.remove(path)
            if act.dry_run():
                self.planned[path] = None

    def publish_config(self, d, sock):
        self.ensure_dir(d, "0700")
        blocks = alias_blocks(self.forks(), "/secrets", "build_key_", sock)
        self.converge_file(os.path.join(d, "ssh_config"), CONFIG_HEADER + blocks, "0644")
        self.converge_file(os.path.join(d, "github-user"), self.github_user() + "\n", "0644")
        bz = self.bugzilla_user()
        if bz:
            self.converge_file(os.path.join(d, "bugzilla-user"), bz + "\n", "0644")
        else:
            self.drop(os.path.join(d, "bugzilla-user"))
            warn("no Bugzilla login for %s: metadata/contributors.json in the\n    mirror (%s) has no entry for that account, or there "
                 "is no mirror\n    ('wk sync'). git-webkit in a workspace asks for one instead" % (self.github_user(), self.store.mirror()))

    def publish(self):
        self.publish_config(self.secrets_dir(), CONTAINER_SOCK)
        self.publish_view("container")

    def store_publish(self):
        if self.owned_here():
            self.publish()
        else:
            self.require_published()

    def require_published(self):
        d = self.secrets_dir()
        for f in PUBLISHED:
            if not self.machine.exists(os.path.join(d, f)):
                die("%s/%s is not there, and nothing in here publishes it: %s is the\n    host's ~/.config/wk/secrets, mounted "
                    "read-only. Publish it from the host:\n        ./setup --stage vmtools" % (d, f, d))

    def view_dir(self, kind):
        return self.store.secrets_view_dir(kind)

    def view_files(self, kind):
        want = {f: "0644" for f in PUBLIC}
        try:
            names = self.machine.listdir(self.secrets_dir())
        except OSError:
            names = []
        want.update({f: "0644" for f in names if f.startswith("build_key_") and f.endswith(".pub")})
        for row in self.agent_secrets():
            if row[4] == "value" and kind in row[5].split(","):
                want[row[1]] = "0600"
        return want

    def publish_view(self, kind):
        if not self.owned_here():
            return True
        src, d = self.secrets_dir(), self.view_dir(kind)
        self.ensure_dir(d, "0700")
        want = self.view_files(kind)
        for f, mode in sorted(want.items()):
            text = self.text(os.path.join(src, f))
            if text is None:
                self.drop(os.path.join(d, f))
            else:
                self.converge_file(os.path.join(d, f), text, mode)
        for f in self.machine.listdir(d) if self.machine.isdir(d) else []:
            if f not in want:
                self.machine.remove(os.path.join(d, f))
        return True

    def push_key_adopt(self, fork, key):
        priv = self.push_key_path(fork)
        self.ensure_dir(self.held_dir(), "0700")
        self.ensure_dir(self.secrets_dir(), "0700")
        new = priv + ".new"
        if not self.machine.act_run(["sh", "-c", 'umask 077 && cat > "$0"', new], input=key.rstrip("\n") + "\n").ok:
            return False
        if not act.dry_run() and not self.machine.run(["ssh-keygen", "-y", "-f", new], input="").ok:
            self.machine.remove(new)
            return False
        self.machine.act_run(["mv", "-f", new, priv])
        return self.pub_publish(fork)

    def pub_publish(self, fork):
        """Kept only where workspaces read it: ssh refuses an identity whose `.pub` beside it disagrees."""
        priv = self.push_key_path(fork)
        self.drop(priv + ".pub")
        r = self.machine.run(["ssh-keygen", "-y", "-f", priv], input="")
        if not r.ok:
            return False
        self.converge_file(self.pub_path(fork), r.out, "0644")
        return self.publish_view("container")


def rows(table):
    return "".join("  ".join(r) + "\n" for r in table)


def main(argv):
    if argv in (["forks"], ["agent-secrets"]):
        sys.stdout.write(rows(FORKS if argv[0] == "forks" else AGENT_SECRETS))
        return 0
    if len(argv) == 2 and argv[0] == "cred-read":
        value = Secrets(os.environ["WK_ROOT"]).cred_read(argv[1])
        sys.stdout.write(value or "")
        return 1 if value is None else 0
    if not argv or argv[0] != "alias-blocks" or not 2 <= len(argv) <= 5:
        sys.stderr.write("usage: python3 -m wk.secrets forks|agent-secrets|cred-read <name>|alias-blocks <dir> [<prefix> [<sock> [<proxy>]]]\n")
        return 2
    a = argv[1:] + [""] * (4 - len(argv[1:]))
    sys.stdout.write(alias_blocks(FORKS, a[0], a[1] or "build_key_", a[2], a[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
