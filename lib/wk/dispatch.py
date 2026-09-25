"""The dispatcher: what every command gets before it runs.

Reads the command's declaration (decl.py), refuses what it does not declare,
resolves the workspace name and the machine holding it, and runs the command
there: here, forwarded into the podman VM on a macOS host, or handed to the
machine's own wk. The targets are asked through one Registry (wk.targets);
what a driver has not ported still reaches into the bash library through shell.py.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from wk import completion as C
from wk import decl as D
from wk import act, clock, record, shell, sshalias
from wk.machine import Local, is_macos
from wk.store import Store
from wk.targets import Registry

ROOT = Path(os.environ.get("WK_ROOT") or Path(__file__).resolve().parents[2])
MACHINE = Store().podman_machine()
_registry = None
TOMBSTONES = {
    "image": "'wk image' is renamed: wk sysimage",
    "mcp": "'wk mcp' is removed",
    "pick": "'wk pick' is removed",
    "skills": "'wk skills' is removed",
    "notify": "'wk notify' is removed: a program calls lib/wk/notify.py's send",
    "verify": "'wk verify' is merged into doctor: wk doctor <workspace>",
    "remotes": "'wk remotes' is merged into sync: wk sync [<workspace>] --fix",
    "sudo": "'wk sudo' is now 'wk key sudo'", "bridge": "'wk bridge' is now 'wk machine setup|status|tailnet|rm'",
    "backup": "'wk backup' is now 'wk key backup'", "vm": "'wk vm' is gone: a guest is a workspace (--target vm), its base 'wk sysimage build macos-guest-base'",
    "remote": "'wk remote' is now 'wk machine setup|rm <name>'", "find": "'wk find' is now 'wk machine probe [<name>]'", "pi": "'wk pi' is gone; each verb is now:\n    bench --ab A,B | --ab-systems A,B   wk bench run <ws> <plan> --system <board> --ab A,B | --ab-systems A,B\n    bench --slot <name>                 wk bench run <ws> <plan> --system <board> --slot <name>\n    bench --pgo                         wk bench run <ws> <plan> --system <board> --slot <name>-instr --collect\n    deploy                              wk bench deploy <ws> <board> --slot <name>\n    boot-order                          wk boot <board> --boot-order <usb-first|sd-first|local>\n    setup, helper                       wk machine setup <board>\n    flash                               wk sysimage write --from <path> --disk <board>:<device>",
    "ab": "'wk ab' is now 'wk bench ab' (wk bench ab <pr-spec> --devices <a,b>; wk bench ab <task> --kill)",
}
GLOBALS = {"--force": "WK_FORCE", "--quiet": "WK_QUIET", "--dry-run": "WK_DRY_RUN",
           "-n": "WK_DRY_RUN", "--yes": "WK_YES", "-y": "WK_YES"}


class Exit(Exception):
    def __init__(self, status):
        self.status = status


def _tty(fd):
    try:
        return os.isatty(fd)
    except OSError:
        return False


def _colour(code):
    return code if _tty(2) else ""


def log(msg):
    if not os.environ.get("WK_QUIET"):
        sys.stderr.write(msg + "\n")


def info(msg):
    if not os.environ.get("WK_QUIET"):
        sys.stderr.write("%s==>%s %s\n" % (_colour("\033[32m"), _colour("\033[0m"), msg))


def warn(msg):
    sys.stderr.write("%swarning:%s %s\n" % (_colour("\033[33m"), _colour("\033[0m"), msg))


def die(msg, status=1):
    sys.stderr.write("%serror:%s %s\n" % (_colour("\033[31m"), _colour("\033[0m"), msg))
    raise Exit(status)


# -- markers: the file that says this machine is a workspace, or a build machine

def in_workspace():
    return registry().in_workspace()


def wk_self():
    return registry().workspace_name()


def _logical_cwd():
    """The path the shell shows ($PWD), when it is this directory: a symlinked
    root is named the way the marker names it."""
    pwd = os.environ.get("PWD", "")
    try:
        if pwd and os.path.samefile(pwd, os.getcwd()):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def cwd_workspace():
    if not registry().in_remote_host():
        return ""
    root = registry().remote_marker_field("root")
    if not root:
        return ""
    cwd = _logical_cwd() + "/"
    prefix = root + "/ws/"
    if not cwd.startswith(prefix) or cwd == prefix:
        return ""
    return cwd[len(prefix):].split("/")[0]


# -- argv arithmetic

def positionals(args):
    return [a for a in args if not a.startswith("-")]


def positional(n, args):
    p = positionals(args)
    return p[n - 1] if len(p) >= n else None


def without_positional(n, args):
    out, i = [], 0
    for a in args:
        if not a.startswith("-"):
            i += 1
            if i == n:
                continue
        out.append(a)
    return out


def argv_name(slot, takes, args):
    t = 0 if takes == "*" else int(takes)
    if len(positionals(args)) < slot + t:
        return None
    return positional(slot, args)


def args_before_name(slot, args):
    out, i = [], 0
    for a in args:
        if a.startswith("-"):
            continue
        i += 1
        if i >= slot:
            break
        out.append(a)
    return "".join(" " + a for a in out)


def argv_split(opts, args):
    """Every declared `--x=v` back to `--x v` for the command."""
    out = []
    for i, a in enumerate(args):
        if a == "--":
            out.extend(args[i:])
            break
        if a.startswith("--") and "=" in a and D.in_list(a.split("=")[0] + "=", opts):
            k, _, v = a.partition("=")
            out.extend([k, v])
        else:
            out.append(a)
    return out


class Invocation:
    def __init__(self, cmd, decl, args):
        self.cmd = cmd
        self.decl = decl
        self.args = list(args)
        self.globals_text = ""

    def usage_die(self, why=None):
        if why:
            warn(why)
        log("usage: wk %s" % self.decl.synopsis_line())
        log("       wk %s -h for the rest" % self.cmd)
        raise Exit(2)

    def argv_check(self):
        """The one place an argument is refused. What passes is one word per
        option (`--x=v`), so the name arithmetic never takes a value for a
        positional."""
        d, args = self.decl, self.args
        sub = args[0] if args else ""
        opts = d.opts_for(args)
        passthrough = d.passthrough_for(args)
        name_decl = d.name_for(args)
        takes = d.takes_for(args)
        slot = D.name_slot(name_decl)
        if slot > 0 and in_workspace():
            slot -= 1
        maximum = slot + (0 if takes == "*" else int(takes))
        out, n, i = [], 0, 0
        while i < len(args):
            a = args[i]
            i += 1
            if a == "--":
                if not passthrough:
                    self.usage_die("'--' means nothing to wk %s" % self.cmd)
                out.append(a)
                out.extend(args[i:])
                return out
            if a.startswith("-") and len(a) > 1:
                key = a.split("=")[0]
                if D.in_list(key + "=", opts):
                    if "=" in a:
                        out.append(a)
                    else:
                        if i >= len(args):
                            self.usage_die("%s needs a value" % key)
                        out.append(a + "=" + args[i])
                        i += 1
                elif D.in_list(key, opts):
                    if "=" in a:
                        self.usage_die("%s takes no value" % key)
                    out.append(a)
                else:
                    self.usage_die("unknown option: %s" % a)
                continue
            out.append(a)
            if n == 0 and in_workspace() and a == wk_self():
                continue   # the name in here: refused further down, by name
            n += 1
            if takes == "*":
                continue
            if n > maximum:
                if passthrough == "tail":
                    out.extend(args[i:])
                    return out
                if in_workspace() and name_decl.split("@")[0] != "none":
                    self.usage_die("unexpected argument: %s (this is workspace '%s', so there is no "
                                   "workspace argument in here)" % (a, wk_self()))
                self.usage_die("unexpected argument: %s" % a)
            if passthrough == "tail" and n == maximum:
                out.extend(args[i:])
                return out
        return out

    # -- questions the command answers for itself

    def _impl(self, *args):
        cp = subprocess.run([str(self.decl.path), *args], stdout=subprocess.PIPE, text=True)
        return cp.returncode, cp.stdout.strip()

    def where(self):
        w = self.decl.where_for(self.args)
        if w == "dynamic":
            rc, w = self._impl("--where", *self.args)
            if rc != 0:
                die("'wk %s --where' did not answer" % self.cmd)
        return w

    def derived_name(self):
        return self._impl("--wsname", *self.args)[1]

    def named_target(self):
        return self._impl("--wstarget", *self.args)[1]

    # -- checks

    def check_needs(self):
        needs = self.decl.needs_for(self.args)
        if not needs:
            return
        missing = []
        for n in needs.split(","):
            if n == "gh-auth":
                ok = shell.gh_authenticated(str(ROOT), env=_quiet_env())
                if not ok:
                    missing.append("gh-auth    gh cannot reach the GitHub API (not logged in, or the token expired): gh auth login")
            elif n == "tailnet":
                ok = subprocess.call(["tailscale", "status"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL) == 0 if _which("tailscale") else False
                if not ok:
                    missing.append("tailnet    this machine is not on the tailnet: tailscale up")
            elif n == "quiesce-helper":
                helper = "/usr/local/libexec/wk-quiesce-priv"
                ok = os.access(helper, os.X_OK) and subprocess.call(
                    ["sudo", "-n", helper, "status"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL) == 0
                if not ok:
                    missing.append("quiesce-helper    the privileged quiesce/session helper is not set up: ./setup --stage quiesce")
            elif not _which(n):
                missing.append("%s    not installed here" % n)
        if missing:
            die("'wk %s' cannot start here:%s\n    wk doctor says how to get each of these."
                % (self.cmd, "".join("\n    " + m for m in missing)))


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _quiet_env():
    return dict(os.environ)


# -- help

def usage():
    err = sys.stderr
    err.write("usage: wk <command> [args]\n")
    decls = list(D.all_commands(ROOT))
    for group, title in (("workspaces", "workspaces"), ("sources", "sources and disk"),
                         ("hosts", "hosts and machines"), ("other", "other")):
        rows = [d for d in decls if d.group == group]
        if rows:
            err.write("\n%s\n" % title)
            for d in rows:
                err.write("  %-32s %s\n" % (d.synopsis_line(), d.summary()))
    err.write("""
  Inside a workspace the name is implicit: `wk build <config>`, `wk run -- <args>`
  act on this machine.

  wk <command> -h          what it does, what it acts on, whether it changes anything
  wk <command> --force     cross a refusal that exists because of a rule; the
                           warning repeats when the command ends. It does not
                           force past a missing workspace or a failed build
  wk <command> --quiet     drop the info/log narration; a result, a warning
                           and an error still print. Every command accepts it
  wk <command> --dry-run   print every command it would run, run none of them;
                           -n for short. Every command accepts it
  wk <command> --yes       answer yes to the question a destructive command
                           asks first; -y for short. Nothing else is asked
  An option a command does not name in its -h, or an argument past what it
  takes, is refused with its usage line.
  wk <command> --all       every one of what the command acts on -- every
                           machine ('wk push'/'wk key sudo'), every workspace on
                           every target ('wk sync', 'wk rm'), every line of
                           the log ('wk logs'); a command's own -h says what
                           its --all covers
  WK_DEBUG=1               verbose output
""")
    err.write("\n  wk help                  README.md: architecture, setup, every workflow by example\n")
    err.write("  wk completion bash|zsh   shell completion for wk; shell/bashrc evals it already\n")
    raise Exit(2)


def dump_declarations():
    for d in D.all_commands(ROOT):
        print("\t".join([d.name, d.where, d.name_decl, d.group, d.synopsis_line(), d.takes,
                         d.opts or "-", d.destructive or "-", d.dryrun or "no",
                         d.passthrough or "-", d.readonly or "-"]))
    raise Exit(0)


def completion_cmd(args):
    """A builtin: it reads every command's declaration, so it has no `cmd/` file of its own."""
    sub = args[0] if args else None
    if sub in ("-h", "--help", "--explain"):
        sys.stdout.write(
            "wk completion bash|zsh -- print a shell completion script for wk\n\n"
            "  changes things: no -- prints a script for your shell's rc to eval\n"
            "  runs on: this machine, never forwarded\n\n"
            "what it does:\n"
            "  shell/bashrc evals it for both shells when wk is on PATH. Commands,\n"
            "  subverbs and flags come from each command's declaration. Workspace names\n"
            "  are read from this machine's own stores at each TAB press, never by\n"
            "  asking a machine; a command's values= list is asked of the command when\n"
            "  that list is answered on this machine.\n")
        raise Exit(0)
    if sub not in C.SHELLS or len(args) != 1:
        die("usage: wk completion bash|zsh -- print a shell completion script for wk; see wk completion -h", 2)
    sys.stdout.write(C.generate(ROOT, sub, TOMBSTONES))
    raise Exit(0)


def where_prose(d, where):
    if where == "host":
        return "this host's own hardware; refused inside a workspace and on a build machine"
    if where == "store":
        return "the machine holding the store -- the podman VM on macOS, this machine otherwise; refused inside a workspace"
    if where == "local":
        return "this machine, never forwarded"
    if where == "dynamic":
        return "whichever the command itself answers for the rest of the arguments"
    if d.here:
        return "the machine you type it on"
    if d.lifecycle:
        return "the workstation that keeps the workspace record"
    return ("the workspace's target, on the machine holding it (the podman VM for a container "
            "workspace on macOS; that machine's own wk when it has one)")


def destructive_prose(spec):
    if spec == "yes":
        return "yes -- asks before it acts; --yes answers"
    if spec:
        return "%s -- these ask before they act; --yes answers" % spec.replace(",", ", ")
    return "no -- asks nothing"


def explain(cmd, d):
    out = sys.stdout
    out.write("wk %s\n\n" % d.synopsis)
    if d.is_readonly():
        out.write("  changes things: no -- starts nothing, writes nothing, repairs nothing\n")
    elif d.dryrun == "yes":
        out.write("  changes things: yes -- wk %s ... --dry-run prints what it would run and runs none of it\n" % cmd)
    elif d.dryrun:
        out.write("  changes things: yes -- %s honour --dry-run; the rest have no dry run yet (docs/PLAN.md)\n"
                  % d.dryrun.replace(",", ", "))
    else:
        out.write("  changes things: yes -- and has no dry run yet (docs/PLAN.md)\n")
    if not d.is_readonly():
        if d.destructive:
            out.write("  destructive: %s\n" % destructive_prose(d.destructive))
        for verbs, spec in d.sub + d.flag:
            if "destructive" in spec:
                out.write("    %s: %s\n" % (verbs.replace(",", ", "), destructive_prose(spec["destructive"])))
    out.write("  runs on: %s\n" % where_prose(d, d.where))
    for verbs, spec in d.sub + d.flag:
        if "where" in spec:
            out.write("    %s: %s\n" % (verbs.replace(",", ", "), where_prose(d, spec["where"])))
    out.write("\nwhat it does (from %s):\n" % os.path.relpath(str(d.path), str(ROOT)))
    out.write(d.leading_comment() + "\n")
    if d.values:
        out.write("\nvalid values (wk %s %s):\n" % (cmd, d.values))
        out.flush()
        cp = subprocess.run([str(ROOT / "wk"), cmd, d.values], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
        out.write("".join("  " + l + "\n" for l in cp.stdout.splitlines()))
    raise Exit(0)


def help_doc(topic):
    path = ROOT / "README.md"
    text = path.read_text()
    if not topic:
        if _tty(1) and _which("less"):
            os.execvp("less", ["less", str(path)])
        sys.stdout.write(text)
        raise Exit(0)
    topic = topic.lower()
    heading = re.compile(r"^(## .+|\*\*[^*]+\*\*)$", re.M)
    marks = [(m.start(), m.group(0)) for m in heading.finditer(text)]
    out, names = [], []
    for i, (start, head) in enumerate(marks):
        level = 2 if head.startswith("##") else 3
        name = head.strip("#* ")
        names.append(name)
        end = len(text)
        for later, h in marks[i + 1:]:
            if level == 3 or h.startswith("##"):
                end = later
                break
        if topic in name.lower():
            out.append(text[start:end].rstrip() + "\n")
    if not out:
        sys.stderr.write("error: no help topic matches '%s'. Topics (wk help <word>):\n" % topic)
        for n in names:
            sys.stderr.write("    %s\n" % n)
        raise Exit(1)
    sys.stdout.write("\n".join(out))
    raise Exit(0)


# -- machines

def machine_running():
    if not _which("podman"):
        return False
    cp = subprocess.run(["podman", "machine", "inspect", MACHINE, "--format", "{{.State}}"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return cp.stdout.strip() == "running"


def forward_to_vm(inv, cmd, args):
    if not _which("podman"):
        die("podman is required; install the official pkg from podman.io")
    if subprocess.call(["podman", "machine", "inspect", MACHINE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        die("podman machine '%s' does not exist -- run ./setup first" % MACHINE)
    if not machine_running():
        if inv.decl.is_readonly(args[0] if args else ""):
            warn("the podman machine '%s' is stopped, so its store cannot be read" % MACHINE)
            log("  'wk start' to bring it up -- 'wk %s' will not start it" % cmd)
            raise Exit(0)
        if not (_tty(0) and _tty(1)):
            die("the podman machine '%s' is stopped, and 'wk %s' needs it.\n"
                "    Nothing here starts it without a terminal asking:  wk start" % (MACHINE, cmd))
        info("starting podman machine '%s'" % MACHINE)
        subprocess.call(["podman", "machine", "start", MACHINE], stdout=sys.stderr)
    line = registry().load("container").wk_cmd([cmd, *args], os.environ)
    sys.stdout.flush()
    sys.stderr.flush()
    if _tty(0) and _tty(1):
        fields = {}
        for key in ("Port", "IdentityPath", "RemoteUsername"):
            cp = subprocess.run(["podman", "machine", "inspect", MACHINE, "--format",
                                 "{{.SSHConfig.%s}}" % key], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
            fields[key] = cp.stdout.strip()
        if all(fields.values()):
            os.execvp("ssh", ["ssh", "-t", "-p", fields["Port"], "-i", fields["IdentityPath"],
                              "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                              "-o", "LogLevel=ERROR", "%s@127.0.0.1" % fields["RemoteUsername"], line])
    os.execvp("podman", ["podman", "machine", "ssh", MACHINE, "--", line])


def forward_status(inv, cmd, args, env=None):
    """forward_to_vm in a child, for a caller that goes on afterwards."""
    cp = subprocess.run([sys.executable, str(ROOT / "wk"), "--forward", cmd, *args],
                        env=env or os.environ)
    return cp.returncode


def registry():
    """This invocation's one Registry: every target is loaded, and every machine probed, at most once."""
    global _registry
    if _registry is None:
        _registry = Registry(ROOT)
    return _registry


def delegate_target(target):
    """The target's driver when it is a machine that runs commands itself, else None."""
    try:
        t = registry().load(target)
    except LookupError:
        return None
    return t if t.delegates() else None


def delegate_run(target, cmd, args):
    machine = target.name
    far = target.far_side()
    if far == "unreachable":
        die("'%s' acts on a workspace on %s, and %s did not answer.\n"
            "    Nothing here can reach into it: the workspace is that machine's own." % (cmd, machine, machine))
    if far != "answering":
        die("'%s' acts on a workspace on %s, which has no wk-tools of its own to\n"
            "    run it:  wk machine setup %s" % (cmd, machine, machine))
    os.environ["WK_ROW_LABEL"] = machine
    line = target.wk_cmd([cmd, *args], os.environ)
    tty = ["-t"] if (_tty(0) and _tty(1)) else []
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("ssh", ["ssh", *tty, *target.machine.opts, target.machine.dest, line])


def json_merge_list(key, paths):
    """{"<key>": [...]} merged from N files, each zero or more JSON documents concatenated with no
    delimiter -- the shape a per-target `--json` listing (or a missing/empty file) produces."""
    items = []
    dec = json.JSONDecoder()
    for path in paths:
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        i, n = 0, len(text)
        while i < n:
            while i < n and text[i] in " \t\r\n":
                i += 1
            if i >= n:
                break
            obj, i = dec.raw_decode(text, i)
            items.extend(obj.get(key, []))
    return {key: items}


def bare_report(inv, cmd, args):
    """A report with no subject, merged over every target here and the VM."""
    hosts = [t for t in registry().all() if t != "container"]
    worst = 0
    ls_json = cmd == "ls" and "--json" in args
    ls_local = ls_vm = None
    ls_empty = []
    import tempfile
    if hosts:
        env = dict(os.environ, WK_TARGET=" ".join(hosts))
        if ls_json:
            ls_local = tempfile.NamedTemporaryFile(delete=False)
            rc = subprocess.call([str(inv.decl.path), *args], stdout=ls_local, env=env)
            ls_local.close()
        elif cmd == "ls" and machine_running():
            cp = subprocess.run([str(inv.decl.path), "--more-follows", *args], stdout=subprocess.PIPE,
                                text=True, env=env)
            rc = cp.returncode
            sys.stdout.write(cp.stdout)
            sys.stdout.flush()
            if len(cp.stdout.splitlines()) <= 1:
                ls_empty = ["--empty-so-far"]
        else:
            rc = subprocess.call([str(inv.decl.path), *args], env=env)
        worst = max(worst, rc)
    if machine_running():
        env = dict(os.environ, WK_ROW_LABEL=record.machine_name())
        if cmd == "ls" and hosts:
            if ls_json:
                ls_vm = tempfile.NamedTemporaryFile(delete=False)
                cp = subprocess.run([sys.executable, str(ROOT / "wk"), "--forward", cmd, "--continued", *args],
                                    stdout=ls_vm, env=env)
                ls_vm.close()
                rc = cp.returncode
            else:
                rc = forward_status(inv, cmd, ["--continued", *ls_empty, *args], env=env)
        else:
            rc = forward_status(inv, cmd, args, env=env)
        worst = max(worst, rc)
    elif _which("podman"):
        warn("the podman machine '%s' is stopped, so container workspaces are not included" % MACHINE)
        log("  'wk start' to bring it up")
    if ls_json:
        files = [f.name for f in (ls_local, ls_vm) if f is not None]
        print(json.dumps(json_merge_list("workspaces", files)))
        for f in files:
            os.unlink(f)
    raise Exit(worst)


# -- main

def main(argv):
    if not argv:
        usage()
    cmd, args = argv[0], list(argv[1:])
    if cmd in ("help", "-h", "--help"):
        help_doc(args[0] if args else "")
    if cmd == "setup":
        os.execv(str(ROOT / "setup"), [str(ROOT / "setup"), *args])
    if cmd in TOMBSTONES:
        die(TOMBSTONES[cmd])
    if cmd == "claude":
        die("'wk claude' is renamed: wk ai claude%s\n    One command for every coding agent (wk ai -h)."
            % ((" " + args[0]) if args else ""))
    if cmd == "--declarations":
        dump_declarations()
    if cmd == "completion":
        completion_cmd(args)
    if cmd == "--forward":
        # this process's own forward, in a child that goes on afterwards (bare_report)
        d = D.Decl(ROOT / "cmd" / args[0])
        forward_to_vm(Invocation(args[0], d, args[1:]), args[0], args[1:])
    impl = ROOT / "cmd" / cmd
    if not (impl.is_file() and os.access(str(impl), os.X_OK)):
        warn("unknown command: %s" % cmd)
        usage()
    try:
        d = D.Decl(impl)
    except D.DeclError as e:
        die(str(e))

    rest, globals_text, seen_dashdash = [], "", False
    for a in args:
        if not seen_dashdash:
            if a in ("-h", "--help", "--explain"):
                explain(cmd, d)
            if a in GLOBALS:
                os.environ[GLOBALS[a]] = "1"
                globals_text += " " + a
                continue
            if a == "--":
                seen_dashdash = True
        rest.append(a)
    args = rest
    inv = Invocation(cmd, d, args)
    inv.globals_text = globals_text

    # WK_NAME is this invocation's answer, never an inherited one.
    os.environ.pop("WK_NAME", None)

    where = inv.where()
    sub = args[0] if args else ""

    if in_workspace() and (where in ("host", "store") or d.outside) and not D.in_list(sub, d.broker):
        if d.outside:
            die("'wk %s' acts on a host, and this is workspace '%s'.\n    From the host:  wk %s %s"
                % (cmd, wk_self(), cmd, wk_self()))
        die("'wk %s' acts on a host, and this is workspace '%s'.\n    From the host:  wk %s%s%s"
            % (cmd, wk_self(), cmd, "".join(" " + a for a in args), globals_text))
    if registry().in_remote_host() and where == "host":
        die("'wk %s' acts on a workstation's own store or hardware, and this is\n"
            "    the shared build machine for target '%s'.\n"
            "    Run it on the workstation instead. What works here: ls, status, build,\n"
            "    run, test, logs, enter." % (cmd, registry().remote_marker_field("target")))
    if registry().in_remote_host() and d.lifecycle:
        die("workspaces are created and destroyed from the workstation, and this is\n"
            "    the shared build machine for target '%s'.\n"
            "    Run 'wk %s' there: the workstation owns the workspace's store, and a\n"
            "    later 'wk build' finds its target from that store." % (registry().remote_marker_field("target"), cmd))

    args = inv.argv_check()
    inv.args = args
    sub = args[0] if args else ""
    if os.environ.get("WK_DRY_RUN") and not d.honours_dryrun(args) and not d.is_readonly(sub):
        inv.usage_die("'wk %s' has no dry run yet: not every change it makes goes through\n"
                      "    the one path --dry-run intercepts (owed, docs/PLAN.md)" % cmd)
    if d.is_destructive(args):
        os.environ["WK_DESTRUCTIVE"] = "1"

    name_decl, slot, takes, derived = "none", 0, "0", ""
    if where == "workspace":
        name_decl = d.name_for(args)
        slot = D.name_slot(name_decl)
        takes = d.takes_for(args)
        if name_decl.split("@")[0] == "derived":
            derived = inv.derived_name()

    resolved = ""
    if where == "workspace" and not in_workspace():
        resolved = resolve_target(inv, name_decl, slot, takes, derived)

    delegate = None
    if (where == "workspace" and name_decl.split("@")[0] != "none" and not in_workspace()
            and not os.environ.get("WK_IN_VM") and not d.here and not d.lifecycle):
        delegate = delegate_target(resolved)

    forwards = (where == "workspace" and is_macos() and not os.environ.get("WK_IN_VM")
                and not in_workspace() and d.forward and resolved == "container")
    if not forwards and delegate is None:
        inv.check_needs()

    if where == "store" and not os.environ.get("WK_IN_VM"):
        if not registry().store.is_local():
            forward_to_vm(inv, cmd, args)

    if where != "workspace":
        os.execv(str(impl), [str(impl), *argv_split(d.opts_for(args), args)])

    if delegate is not None:
        delegate_run(delegate, cmd, args)

    name = ""
    if not in_workspace() and name_decl.split("@")[0] == "required":
        if argv_name(slot, takes, args) is None and not cwd_workspace():
            inv.usage_die()
    if in_workspace():
        os.environ.setdefault("WK_TARGET", "local")
        resolved = os.environ["WK_TARGET"]
        name = wk_self()
        if name_decl.split("@")[0] != "none" and argv_name(slot, takes, args) == name:
            die("this is workspace '%s', and there is no workspace argument in here --\n"
                "    every command acts on this one. Drop the name: wk %s%s"
                % (name, cmd, args_before_name(slot, args)))
    elif forwards:
        if d.post == "zed" and "--zed" in args:
            rest = [a for a in args if a != "--zed"]
            name = positional(1, rest)
            if not name:
                die("'wk %s --zed' needs a workspace name" % cmd)
            if forward_status(inv, cmd, rest) != 0:
                die("'wk %s %s' failed, so nothing was opened.\n"
                    "    What it managed to say is above; a re-run destroys the rubble and retries." % (cmd, name))
            os.execv(str(ROOT / "cmd" / "zed"), [str(ROOT / "cmd" / "zed"), name])
        if d.post == "ssh-alias-remove":
            name = positional(1, args) or ""
            rc = forward_status(inv, cmd, args)
            if rc != 0:
                raise Exit(rc)
            if name:
                sshalias.alias_remove(Local(), os.environ, name)
            raise Exit(0)
        if d.bare == "merged" and not positionals(args):
            bare_report(inv, cmd, args)
        if d.is_readonly(sub) and not _which("podman"):
            warn("podman is not installed, so there are no container workspaces to read")
            log("  './setup' installs it; 'WK_TARGET=vm wk ls' lists the macOS guests, which do not need it")
            raise Exit(0)
        forward_to_vm(inv, cmd, args)

    base = name_decl.split("@")[0]
    if base == "derived":
        name = name or derived
    elif base in ("required", "optional") and not name:
        a = argv_name(slot, takes, args)
        if a is not None:
            name = a
            args = without_positional(slot, args)
        else:
            name = cwd_workspace()
    if name:
        os.environ["WK_NAME"] = name
        asks = not d.lifecycle and base != "derived" and not in_workspace()
        if not d.lifecycle:
            os.environ["WK_TARGET"] = resolved
        if asks or d.ready:
            ask_target(inv, resolved, name, asks, d.ready)
    os.execv(str(impl), [str(impl), *argv_split(d.opts_for(args), args)])


def ask_target(inv, resolved, name, exists, ready):
    """Whether `name` is on `resolved` (a machine that did not answer is no absence), then its readiness, from one load."""
    try:
        target = registry().load(resolved)
        if exists and not registry().exists_on(target, name):
            inv.usage_die("no such workspace: %s -- 'wk ls' lists them" % name)
        if ready:
            target.wait_ready(name, clock.Clock())
    except LookupError as e:
        die(str(e))
    except act.Refused as e:
        raise Exit(e.status)


def resolve_target(inv, name_decl, slot, takes, derived):
    if os.environ.get("WK_TARGET"):
        return os.environ["WK_TARGET"]
    args = inv.args
    named = D.Args(inv.decl, argv_split(inv.decl.opts_for(args), args)).value("--target")
    if named:
        return named
    name = ""
    if name_decl.split("@")[0] == "derived":
        t = inv.named_target()
        if t:
            return t
        name = derived
    elif slot > 0:
        name = argv_name(slot, takes, args) or cwd_workspace() or ""
    if name:
        try:
            return registry().ws_target(name)
        except LookupError as e:
            die(str(e))
    return "container"


def entry():
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `wk help | head` ends quietly, as a shell tool does
    try:
        main(sys.argv[1:])
    except Exit as e:
        sys.exit(e.status)
    except KeyboardInterrupt:
        sys.exit(130)
