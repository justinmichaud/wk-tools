"""The credentials: where this machine keeps them, the key and token files the push service and the injector read, and
/secrets, what every workspace here reads."""

import argparse
import os
import shlex
import sys

from wk import act, claudelogin, images, project, repos
from wk.act import debug, die, warn
from wk.machine import Local
from wk.store import Store, in_vm, remote_marker_path

PUBLIC = ("github-user", "bugzilla-user")
PUBLISHED = ("github-user", "view/container/github-user")
AGENT_SECRETS = (("claude", "claude-token", ".wk-agent-token", "CLAUDE_CODE_OAUTH_TOKEN", "remote"),
                 ("litellm", "litellm-key", ".wk-litellm-key", "LITELLM_API_KEY", "container,vm,remote"))
LOGIN_KINDS = ("container", "vm")
LOGIN_DIR = ".wk-claude"


def forks():
    return repos.default().push


def push_keys():
    return repos.push_keys()


def agent_secrets():
    return [list(r) for r in AGENT_SECRETS]


def first_line(text):
    return (text or "").split("\n", 1)[0].rstrip("\r")


BOX_PUSH_REFUSAL = "error: a build box holds no deploy key; push from the workstation:  wk pr open <workspace>"


def box_alias_blocks(forks):
    refuse = "sh -c %s" % shlex.quote('echo "%s" >&2; exit 1' % BOX_PUSH_REFUSAL)
    return "".join("\nHost %s\n    HostName github.com\n    User git\n    ProxyCommand %s\n" % (alias, refuse) for _, _, alias in forks)


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

    def owned_here(self):
        """Inside the podman VM the keyring is the macOS host's, mounted read-only."""
        return not in_vm(self.env)

    def push_key_path(self, fork):
        return os.path.join(self.store.keyring_push_dir(), "build_key_" + fork)

    def key_at_rest(self, fork):
        return self.machine.exists(self.push_key_path(fork))

    def pub_path(self, fork):
        return os.path.join(self.store.keyring_dir(), "build_key_%s.pub" % fork)

    def github_pat_path(self):
        return os.path.join(self.store.keyring_push_dir(), "github-pat")

    def bugzilla_key_path(self):
        return os.path.join(self.store.keyring_push_dir(), "bugzilla-api-key")

    def agent_secrets(self):
        return agent_secrets()

    def cred_path(self, name):
        home = self.env.get("HOME") or os.path.expanduser("~")
        fixed = {"github-pat": self.github_pat_path, "bugzilla-api-key": self.bugzilla_key_path,
                 "claude-login": self.store.keyring_claude_login, "ntfy": self.store.keyring_ntfy_topic,
                 "tailnet": lambda: self.env.get("WK_TS_AUTHKEY") or os.path.join(home, ".config", "wk", "tailscale-authkey"),
                 "tailnet-api": lambda: self.env.get("WK_TS_API_SECRET") or os.path.join(home, ".config", "wk", "tailscale-api-key")}
        if name in fixed:
            return fixed[name]()
        row = next((r for r in self.agent_secrets() if r[0] == name), None)
        if row is None:
            return None
        if "remote" in row[4].split(",") and os.path.isfile(remote_marker_path(self.env)):
            return os.path.join(home, row[2])
        return os.path.join(self.store.keyring_dir(), row[1])

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
        r = self.machine.run(["git", "-C", self.store.mirror_dir(), "cat-file", "-p", "main:metadata/contributors.json"], input="")
        if not r.ok:
            return None
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "contributors.py"), "bugzilla-login", self.github_user()],
                             input=r.out)
        if not r.ok:
            return None
        return r.out.strip() or None

    def _machine_file(self, var, name):
        return self.env.get(var) or os.path.join(self.store.store_dir(), name)

    def machine_pat(self):
        return self._machine_file("WK_PUSH_PAT_FILE", "push-github-pat")

    def machine_read_pat(self):
        return self._machine_file("WK_PUSH_READ_PAT_FILE", "read-github-pat")

    def machine_bugzilla_key(self):
        return self._machine_file("WK_PUSH_BUGZILLA_KEY_FILE", "push-bugzilla-api-key")

    def machine_argv(self, line):
        """A shell line on that machine: here, or in the podman VM when its store is not this process's to write."""
        if self.host_side or self.store.is_local():
            return ["sh", "-c", line]
        return ["podman", "machine", "ssh", self.store.podman_machine(), "--", line]

    def _ask(self, line):
        return self.machine.run(self.machine_argv(line), input="")

    def _act(self, line, input=""):
        return self.machine.act_run(self.machine_argv(line), input=input)

    def cred_write(self, path, name):
        """The first line of a held credential, down a pipe: an empty file would be a token the injector sends."""
        value = first_line(self.cred_read(name))
        if not value:
            return False
        return self._act("umask 077 && cat > %s" % shlex.quote(path), input=value + "\n").ok

    def cred_clear(self, path):
        return self._act("rm -f %s" % shlex.quote(path)).ok

    def cred_sync(self, path, name):
        if first_line(self.cred_read(name)):
            return self.cred_write(path, name)
        return self.cred_clear(path)

    def machine_creds(self):
        """(path, held credential) per file the injector on this machine reads: the token a read spends, the one a write spends, the Bugzilla key."""
        return ((self.machine_read_pat(), "github-pat"), (self.machine_pat(), "github-pat"),
                (self.machine_bugzilla_key(), "bugzilla-api-key"))

    def machine_push_key(self, fork):
        return os.path.join(self.store.store_dir(), "push-keys", "build_key_" + fork)

    def push_key_sync(self, fork):
        """The deploy key where the push service of this machine reads it; the podman machine mounts none, so it is a copy there."""
        src, dest = self.push_key_path(fork), self.machine_push_key(fork)
        if src == dest:
            return True
        key = self.read(src)
        if not (key or "").strip():
            return self._act("rm -f %s" % shlex.quote(dest)).ok
        return self._act("umask 077 && mkdir -p %s && cat > %s" % (shlex.quote(os.path.dirname(dest)), shlex.quote(dest)), input=key).ok

    def push_converge_machine(self):
        if not self.machine.isdir(self.store.keyring_push_dir()):
            debug("the held credentials are not on this machine (%s; the podman machine never mounts them), so they are "
                  "left to the host that has them" % self.store.keyring_push_dir())
            return True
        ok = all([self.cred_sync(path, name) for path, name in self.machine_creds()]
                 + [self.push_key_sync(k) for k, _, _ in push_keys()])
        if not ok:
            warn("this machine's injector and push service did not take every credential and deploy key; './setup' converges them")
        return ok

    def push_deliver(self):
        """Every injector and push service this machine runs: a credential delivered to one of two is a 401 from the other."""
        ok = self.push_converge_machine()
        if self.macos:
            from wk import guest
            ok = guest.credentials_converge(self.root, self.env, self.machine) and ok
        return ok

    def claude_login_migrate(self):
        """Once, by rename: agent-rw, where the login was, is still mounted by an older podman machine, container or guest."""
        old, new = os.path.join(self.store.keyring_agent_rw_dir(), ".credentials.json"), self.cred_path("claude-login")
        if not self.machine.exists(old):
            return "unchanged"
        if self.machine.exists(new):
            warn("%s is an older copy of the claude.ai login, readable where agent-rw is mounted; %s is the one the injector "
                 "holds:  rm %s" % (old, new, old))
            return "both"
        self.ensure_dir(os.path.dirname(new), "0700")
        return "moved" if self.machine.act_run(["mv", old, new]).ok else "failed"

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

    def publish_config(self, d):
        self.ensure_dir(d, "0700")
        self.converge_file(os.path.join(d, "github-user"), self.github_user() + "\n", "0644")
        bz = self.bugzilla_user()
        if bz:
            self.converge_file(os.path.join(d, "bugzilla-user"), bz + "\n", "0644")
        else:
            self.drop(os.path.join(d, "bugzilla-user"))
            warn("no Bugzilla login for %s: metadata/contributors.json in the\n    mirror (%s) has no entry for that account, or there "
                 "is no mirror\n    ('wk sync'). %s in a workspace asks for one instead" % (self.github_user(), self.store.mirror_dir(), project.get("PR_TOOL")))

    def publish(self):
        self.publish_config(self.store.keyring_dir())
        self.publish_view("container")

    def store_publish(self):
        if self.owned_here():
            self.publish()
        else:
            self.require_published()

    def require_published(self):
        d = self.store.keyring_dir()
        for f in PUBLISHED:
            if not self.machine.exists(os.path.join(d, f)):
                die("%s/%s is not there, and nothing in here publishes it: %s is the\n    host's ~/.config/wk/secrets, mounted "
                    "read-only. Publish it from the host:\n        ./setup --stage vmtools" % (d, f, d))

    def view_files(self, kind):
        want = {f: "0644" for f in PUBLIC}
        for row in self.agent_secrets():
            if kind in row[4].split(","):
                want[row[1]] = "0600"
        return want

    def publish_view(self, kind):
        if not self.owned_here():
            return True
        src, d = self.store.keyring_dir(), self.store.keyring_view_dir(kind)
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
        self.ensure_dir(self.store.keyring_push_dir(), "0700")
        self.ensure_dir(self.store.keyring_dir(), "0700")
        new = priv + ".new"
        if not self.machine.act_run(["sh", "-c", 'umask 077 && cat > "$0"', new], input=key.rstrip("\n") + "\n").ok:
            return False
        if not act.dry_run() and not self.machine.run(["ssh-keygen", "-y", "-f", new], input="").ok:
            self.machine.remove(new)
            return False
        self.machine.act_run(["mv", "-f", new, priv])
        if act.dry_run():
            return True
        return self.pub_publish(fork)

    def pub_publish(self, fork):
        """The `.pub` beside a private key is dropped (ssh refuses an identity whose `.pub` beside it disagrees), and the key goes where the push service reads it."""
        priv = self.push_key_path(fork)
        self.drop(priv + ".pub")
        r = self.machine.run(["ssh-keygen", "-y", "-f", priv], input="")
        if not r.ok:
            return False
        self.converge_file(self.pub_path(fork), r.out, "0644")
        if not self.push_key_sync(fork):
            warn("the deploy key '%s' did not reach this machine's push service; './setup' converges it" % fork)
        return True


def rows(table):
    return "".join("  ".join(r) + "\n" for r in table)


def main(argv):
    parser = argparse.ArgumentParser(prog="python3 -m wk.secrets")
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("push-keys", "agent-secrets", "push-converge", "claude-placeholder", "claude-login-migrate"):
        sub.add_parser(verb)
    sub.add_parser("box-alias-blocks")
    a = parser.parse_args(argv)
    if a.verb == "push-converge":
        return 0 if Secrets(images.root()).push_converge_machine() else 1
    if a.verb == "claude-login-migrate":
        said = Secrets(images.root()).claude_login_migrate()
        print(said)
        return 1 if said == "failed" else 0
    if a.verb == "claude-placeholder":
        sys.stdout.write(claudelogin.placeholder())
        return 0
    sys.stdout.write(box_alias_blocks(forks()) if a.verb == "box-alias-blocks" else
                     rows([k, alias] for k, _, alias in push_keys()) if a.verb == "push-keys" else rows(AGENT_SECRETS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
