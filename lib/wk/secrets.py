"""Where this machine keeps each credential, and how a workspace, an injector and a push service are given it."""

import argparse
import hashlib
import json
import os
import shlex
import sys

import credcheck
from wk import act, claudelogin, images, project, repos
from wk.act import debug, die, info, warn
from wk.machine import Local
from wk.store import Store, in_vm, remote_marker_path

PUBLIC = ("github-user", "bugzilla-user")
PUBLISHED = ("github-user", "view/container/github-user")
AGENT_SECRETS = (("claude", "claude-token", ".wk-agent-token", "CLAUDE_CODE_OAUTH_TOKEN", "remote"),
                 ("litellm", "litellm-key", ".wk-litellm-key", "LITELLM_API_KEY", "container,vm,remote"))
LOGIN_KINDS = ("container", "vm")
LOGIN_DIR = ".wk-claude"
PUSH_CLIENT = "container/push/wk-push-client.py"


def forks():
    return repos.default().push


def push_keys():
    return repos.push_keys()


def agent_secrets():
    return [list(r) for r in AGENT_SECRETS]


def first_line(text):
    return (text or "").split("\n", 1)[0].rstrip("\r")


LOGIN_WRITE = 'umask 077 && mkdir -p "$HOME/$1" && cat > "$HOME/$1/.credentials.json"'
# A container links its read-only /secrets, so a later `wk key set` reaches it: rc 1 is a link to nothing held yet.
# A write removes first: umask sets the mode of a file `cat >` creates, not of one already there.
DELIVERY = {"link": 'ln -sfn "/secrets/$2" "$HOME/$1" || exit 2; test -e "$HOME/$1"',
            "write": 'umask 077 && rm -f "$HOME/$1" && cat > "$HOME/$1"', "withdraw": 'rm -f "$HOME/$1"'}


def deliver(m, kind, value_of=None, where="", tools=""):
    """What a <kind> workspace holds of its credentials, by `m` running in it. Where the injector and the push service
    serve it: the placeholder login, and git's ssh transport the push client under `tools`. Each row given to <kind> at
    ~/<home> (`value_of(name)`'s first line on stdin, or a container's link); every other row's file taken away.
    `value_of` answers None for a stored file it refused to read."""
    where = where or kind
    ok = kind not in LOGIN_KINDS or all(m.act_run(argv, input=text).ok for argv, text in (
        (["sh", "-c", LOGIN_WRITE, "sh", LOGIN_DIR], claudelogin.placeholder()),
        (["git", "config", "--global", "core.sshCommand", tools + "/" + PUSH_CLIENT], ""),
        (["git", "config", "--global", "ssh.variant", "ssh"], "")))   # git would otherwise run the client with -G
    for name, file, home, var, kinds in AGENT_SECRETS:
        given = kind in kinds.split(",")
        value = value_of(name) if given and kind != "container" else ""
        if value is None:
            warn("the stored %s was refused (above), so none was delivered to %s" % (name, where))
            ok = False
            continue
        how = "link" if given and kind == "container" else "write" if first_line(value) else "withdraw"
        r = m.act_run(["sh", "-c", DELIVERY[how], "sh", home, file], input=first_line(value) + "\n" if how == "write" else "")
        if how == "link" and r.rc == 1 or given and how == "withdraw" and r.ok:
            info("no %s credential is held, so $%s is unset in %s: 'wk key set %s' on the host stores one" % (name, var, where, name))
        elif not r.ok:
            warn("could not deliver ~/%s in %s" % (home, where))
            ok = False
    return ok


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
        path = self.cred_path(name)
        if not path:
            return False
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "secretfile.py"), "present", path], input="")
        sys.stderr.write(r.err)
        return True if r.rc == 0 else False if r.rc == 1 else None

    def check_value(self, name, value, path="", repos=None, evidence=None):
        if name == "bugzilla-api-key":
            evidence = dict(evidence or {}, login=self.bugzilla_user() or "")
        return credcheck.verdict(name, value, [r[1] for r in self.forks()] if repos is None else repos, path, evidence)

    def cred_verdict(self, name):
        path = self.cred_path(name)
        value = self.read(path)
        if value is None:
            return "bad\tthe file at %s could not be read; the refusal above says why" % path
        return self.check_value(name, value, path)

    def agent_secret_remedy(self, name):
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
        """The first email of this account's entry in the mirror's contributors.json."""
        r = self.machine.run(["git", "-C", self.store.mirror_dir(), "cat-file", "-p", "main:metadata/contributors.json"], input="")
        try:
            entries = json.loads(r.out) if r.ok else []
        except ValueError:
            entries = []
        return next((e["emails"][0] for e in entries if e.get("github") == self.github_user() and e.get("emails")), None)

    def _machine_file(self, var, name):
        return self.env.get(var) or os.path.join(self.store.store_dir(), name)

    def machine_pat(self):
        return self._machine_file("WK_PUSH_PAT_FILE", "push-github-pat")

    def machine_read_pat(self):
        return self._machine_file("WK_PUSH_READ_PAT_FILE", "read-github-pat")

    def machine_bugzilla_key(self):
        return self._machine_file("WK_PUSH_BUGZILLA_KEY_FILE", "push-bugzilla-api-key")

    def machine_argv(self, line):
        if self.host_side or self.store.is_local():
            return ["sh", "-c", line]
        return ["podman", "machine", "ssh", self.store.podman_machine(), "--", line]

    def _ask(self, line):
        return self.machine.run(self.machine_argv(line), input="")

    def _act(self, line, input=""):
        return self.machine.act_run(self.machine_argv(line), input=input)

    def _holds(self, path, value):
        """A read of the file's digest, so nothing secret comes back; None is a value that withdraws the file."""
        if value is None:
            return not self.machine.run(self.machine_argv("test -e {0} || test -L {0}".format(shlex.quote(path)))).ok
        r = self.machine.run(self.machine_argv("sha256sum < %s" % shlex.quote(path)))
        return r.ok and r.out.split()[:1] == [hashlib.sha256(value.encode()).hexdigest()]

    def cred_write(self, path, name):
        """The first line of a held credential, down a pipe: an empty file would be a token the injector sends."""
        value = first_line(self.cred_read(name))
        if not value:
            return False
        if self._holds(path, value + "\n"):
            return True
        return self._act("umask 077 && cat > %s" % shlex.quote(path), input=value + "\n").ok

    def cred_clear(self, path):
        return self._holds(path, None) or self._act("rm -f %s" % shlex.quote(path)).ok

    def cred_sync(self, path, name):
        if first_line(self.cred_read(name)):
            return self.cred_write(path, name)
        return self.cred_clear(path)

    def machine_creds(self):
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
            return self._holds(dest, None) or self._act("rm -f %s" % shlex.quote(dest)).ok
        if self._holds(dest, key):
            return True
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

    def ensure_dir(self, path, mode):
        if not self.made_dir(path):
            self.machine.mkdir(path)
            self.machine.act_run(["chmod", mode, path])
            if act.dry_run():
                self.planned_dirs.add(path)

    def made_dir(self, path):
        return path in self.planned_dirs or self.machine.isdir(path)

    def text(self, path):
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
    for verb in ("push-keys", "push-converge", "box-alias-blocks"):
        sub.add_parser(verb)
    d = sub.add_parser("deliver")
    d.add_argument("kind", choices=("container",))
    d.add_argument("tools")
    a = parser.parse_args(argv)
    if a.verb == "push-converge":
        return 0 if Secrets(images.root()).push_converge_machine() else 1
    if a.verb == "deliver":
        return 0 if deliver(Local(), a.kind, tools=a.tools) else 1
    sys.stdout.write(box_alias_blocks(forks()) if a.verb == "box-alias-blocks" else rows([k, alias] for k, _, alias in push_keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
