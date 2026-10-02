"""The dispatcher: what every command gets before it runs.

Reads the command's declaration (decl.py), refuses what it does not declare,
resolves the workspace name and the machine holding it, and runs the command
there: here, forwarded into the podman VM on a macOS host, or handed to the
machine's own wk. The targets are asked through one Registry (wk.targets).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from wk import completion as C
from wk import decl as D
from wk import act, buildconf, clock, images, record, sshalias, targets
from wk.act import info, log, warn
from wk.machine import Local, is_macos
from wk.store import Store, build_config, dispatch_target, in_vm, no_such_workspace, ws_name

ROOT = Path(images.root())
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
DISPATCH_VARS = ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_ROOT", "WK_FORCE", "WK_QUIET", "WK_DRY_RUN", "WK_DESTRUCTIVE",
                 "WK_CONFIRMED", "WK_ROW_LABEL", "WK_HOST_SELF", "WK_IN_VM", "WK_CONFIG")
GLOBALS = {"--force": "WK_FORCE", "--quiet": "WK_QUIET", "--dry-run": "WK_DRY_RUN",
           "-n": "WK_DRY_RUN", "--yes": "WK_YES", "-y": "WK_YES"}


class Exit(Exception):
    def __init__(self, status):
        self.status = status


def die(msg, status=1):
    act.err(msg)
    raise Exit(status)


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


def name_in_argv(base, slot, takes, args):
    a = argv_name(slot, takes, args)
    if a is None and base == "required" and not in_workspace() and not cwd_workspace():
        a = positional(slot, args)
    return a


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


def first_positional(d, args):
    """The index of the first word that is neither an option nor a declared option's value, or None."""
    valued, i = d.valued_opts(), 0
    while i < len(args) and args[i] != "--":
        if args[i].startswith("-") and len(args[i]) > 1:
            i += 2 if args[i] in valued else 1
            continue
        return i
    return None


def tail_from(d, args, inside):
    """Where a `passthrough=all` invocation's tail starts in argv as typed: from there on every
    word, `-h` and `--force` included, is the other program's."""
    valued, words, i = d.valued_opts(), [], 0
    while i < len(args) and args[i] != "--":
        a = args[i]
        i += 1
        if a.startswith("-") and len(a) > 1:
            i += 1 if a in valued else 0
            continue
        words.append(a)
        probe = words if D.in_list(words[0], d.verbs) or not d.default else [d.default] + words
        takes = d.takes_for(probe)
        if d.passthrough_for(probe) != "all" or takes == "*":
            return len(args)
        slot = D.name_slot(d.name_for(probe))
        if len(probe) >= slot - (1 if inside and slot else 0) + int(takes):
            return i
    return len(args)


class Invocation:
    def __init__(self, cmd, decl, args):
        self.cmd = cmd
        self.decl = decl
        self.args = list(args)
        self.typed = list(args)
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
            if n == (1 if d.verbs and D.in_list(args[0], d.verbs) else 0) and in_workspace() and a == wk_self():
                continue   # the name in here: refused further down, by name
            n += 1
            if takes == "*":
                continue
            if n > maximum:
                if passthrough in ("tail", "all"):
                    out.extend(args[i:])
                    return out
                if in_workspace() and name_decl.split("@")[0] != "none":
                    self.usage_die("unexpected argument: %s (this is workspace '%s', so there is no "
                                   "workspace argument in here)" % (a, wk_self()))
                self.usage_die("unexpected argument: %s" % a)
            if passthrough in ("tail", "all") and n == maximum:
                out.extend(args[i:])
                return out
        return out

    def gone_check(self):
        """A retired flag anywhere, or a retired verb where the verb goes, is refused naming its replacement."""
        d, args = self.decl, self.args
        pos = first_positional(d, args)
        for i, a in enumerate(args):
            if a == "--":
                break
            if a in d.gone and (a.startswith("-") or i == pos):
                die("'wk %s %s' is gone: wk %s %s" % (self.cmd, a, self.cmd, d.gone[a]))

    def default_takes_a_word(self):
        d = self.decl
        takes = d.takes_for([d.default])
        return takes == "*" or D.name_slot(d.name_for([d.default])) + int(takes) > 1

    def verb_first(self):
        """A declared verb is argv[0], with no option before it. No word, or one that is no verb where the
        default verb takes an argument, is the default verb's; any other word is refused. No word and no
        default is verb_given's to refuse."""
        d, args = self.decl, self.args
        if not d.verbs:
            return args
        pos = first_positional(d, args)
        if pos is not None and D.in_list(args[pos], d.verbs):
            if pos:
                self.usage_die("%s comes before the verb: a verb's options follow it (wk %s %s ...)"
                               % (args[0], self.cmd, args[pos]))
            return args
        if d.default and (pos is None or self.default_takes_a_word()):
            return [d.default] + args
        if pos is None:
            return args
        self.usage_die("unknown verb: %s (one of %s)" % (args[pos], d.verbs.replace(",", ", ")))

    def refuse_inherited_config(self, inherited):
        """An exported WK_CONFIG is never an invocation's answer: one that argv does not name too is refused."""
        d, args = self.decl, self.args
        if not inherited or not d.config:
            return
        pos = [a for a in args[:args.index("--") if "--" in args else len(args)] if not a.startswith("-")]
        if d.config == "--config":
            if not D.in_list("--config=", d.opts_for(args)):
                return
            given = ([a[len("--config="):] for a in args if a.startswith("--config=")] or [None])[-1]
            remedy = "--config %s" % inherited
        else:
            if d.takes_for(args) == "0":
                return
            k = 0 if in_workspace() or (len(pos) == 1 and cwd_workspace()) else D.name_slot(d.name_for(args))
            given = pos[k] if len(pos) > k else None
            remedy = "wk %s%s %s" % (self.cmd, "" if k == 0 else " <workspace>", inherited)
        if given != inherited:
            self.usage_die("WK_CONFIG=%s is set in this environment, and wk takes a build config only from\n"
                           "    its arguments: unset it, or name the config:  %s" % (inherited, remedy))

    def verb_given(self):
        """After the options are checked: a verb."""
        d, args = self.decl, self.args
        if d.verbs and not (args and D.in_list(args[0], d.verbs)):
            self.usage_die("'wk %s' needs one of: %s" % (self.cmd, d.verbs.replace(",", ", ")))

    def take_config(self, args, env=None):
        """The declared build config lifted out of argv into WK_CONFIG; one buildconf does not name is refused."""
        how, out, given = self.decl.config, [], None
        positional = how == "arg" and self.decl.takes_for(args) != "0"
        for i, a in enumerate(args):
            if a == "--":
                out.extend(args[i:])
                break
            if how == "--config" and a.startswith("--config="):
                given = a[len("--config="):]
            elif positional and given is None and not a.startswith("-"):
                given = a
            else:
                out.append(a)
        if given is not None:
            if given not in buildconf.names():
                self.usage_die("unknown config: %s -- 'wk build --list' names every one" % given)
            (os.environ if env is None else env)["WK_CONFIG"] = given
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
                from wk.doctor import gh_authenticated
                ok = gh_authenticated()
                if not ok:
                    missing.append("gh-auth    gh cannot reach the GitHub API (not logged in, or the token expired): gh auth login")
            elif n == "tailnet":
                ok = subprocess.call(["tailscale", "status"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL) == 0 if shutil.which("tailscale") else False
                if not ok:
                    missing.append("tailnet    this machine is not on the tailnet: tailscale up")
            elif n == "quiesce-helper":
                from wk.sudo import QUIESCE_PRIV
                ok = os.access(QUIESCE_PRIV, os.X_OK) and subprocess.call(
                    ["sudo", "-n", QUIESCE_PRIV, "status"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL) == 0
                if not ok:
                    missing.append("quiesce-helper    the privileged quiesce/session helper is not set up: ./setup --stage quiesce")
            elif not shutil.which(n):
                missing.append("%s    not installed here" % n)
        if missing:
            die("'wk %s' cannot start here:%s\n    wk doctor says how to get each of these."
                % (self.cmd, "".join("\n    " + m for m in missing)))


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
        out.write("  changes things: yes -- wk %s %s ... --dry-run prints what it would run and runs none of it;\n"
                  "    the rest have no dry run yet (docs/PLAN.md)\n" % (cmd, d.dryrun.replace(",", "|")))
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
    if d.verbs:
        out.write("  verbs: %s%s\n" % (d.verbs.replace(",", ", "), "; with none, or another first word: %s" % d.default
                                       if d.default else ""))
    out.write("\nwhat it does (from %s):\n" % os.path.relpath(str(d.path), str(ROOT)))
    out.write(d.leading_comment() + "\n")
    if d.config:
        out.write("\nvalid values (%s):\n" % ("--config" if d.config == "--config" else "<config>"))
        out.write("".join(("  " + l).rstrip() + "\n" for l in buildconf.LIST_TEXT.splitlines()))
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
        if os.isatty(1) and shutil.which("less"):
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

def podman_vm():
    return targets.podman_vm(Local(), MACHINE) if shutil.which("podman") else None


def machine_running():
    rec = podman_vm()
    return bool(rec) and rec.get("State") == "running"


def forward_to_vm(inv, cmd, args):
    if not shutil.which("podman"):
        die("podman is required: install the official pkg from podman.io, then ./setup")
    rec = podman_vm()
    if rec is None:
        die("podman machine '%s' does not exist -- run ./setup first" % MACHINE)
    if rec.get("State") != "running":
        if inv.decl.is_readonly(args[0] if args else ""):
            warn("the podman machine '%s' is stopped, so its store cannot be read" % MACHINE)
            log("  'wk start' to bring it up -- 'wk %s' will not start it" % cmd)
            raise Exit(0)
        if not (os.isatty(0) and os.isatty(1)):
            die("the podman machine '%s' is stopped, and 'wk %s' needs it.\n"
                "    Nothing here starts it without a terminal asking:  wk start" % (MACHINE, cmd))
        info("starting podman machine '%s'" % MACHINE)
        subprocess.call(["podman", "machine", "start", MACHINE], stdout=sys.stderr)
    line = registry().load("container").wk_cmd([cmd, *args], os.environ)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.isatty(0) and os.isatty(1):
        opts, dest = targets.podman_vm_route(rec)
        os.execvp("ssh", ["ssh", "-t", *opts, dest, line])
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
        _registry = targets.Registry(ROOT)
    return _registry


def delegate_target(target, ws=""):
    """The target's driver when it runs commands itself and this machine drives no live task on `ws` there, else None."""
    try:
        t = registry().load(target)
    except LookupError:
        return None
    return t if t.delegates() and not (ws and t.records().driving(ws)) else None


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
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("ssh", target.machine.argv(line, tty=os.isatty(0) and os.isatty(1)))


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
    elif shutil.which("podman"):
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
    tail = tail_from(d, args, in_workspace()) if d.passthrough == "all" else len(args)
    for i, a in enumerate(args):
        if i >= tail:
            rest.extend(args[i:])
            break
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
    inv = Invocation(cmd, d, rest)
    inv.globals_text = globals_text
    inv.gone_check()
    args = inv.args = inv.verb_first()

    # WK_NAME and WK_CONFIG are this invocation's answers, never inherited ones.
    ws_name(take=True)
    inherited_config = build_config(take=True)

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
    inv.verb_given()
    inv.refuse_inherited_config(inherited_config)
    sub = args[0] if args else ""
    if act.dry_run() and not d.honours_dryrun(args) and not d.is_readonly(sub):
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
            and not in_vm() and not d.here and not d.lifecycle):
        delegate = delegate_target(resolved, decl_name(inv, name_decl, slot, takes, derived))

    forwards = (where == "workspace" and is_macos() and not in_vm()
                and not in_workspace() and d.forward and resolved == "container")
    if not forwards and delegate is None:
        inv.check_needs()

    if where == "store" and not in_vm():
        if not registry().store.is_local():
            forward_to_vm(inv, cmd, args)

    if where != "workspace":
        args = inv.take_config(args)
        os.execv(str(impl), [str(impl), *argv_split(d.opts_for(args), args)])

    if delegate is not None:
        delegate_run(delegate, cmd, inv.typed)

    name = ""
    if not in_workspace() and name_decl.split("@")[0] == "required":
        if name_in_argv("required", slot, takes, args) is None and not cwd_workspace():
            inv.usage_die()
    if in_workspace():
        resolved = dispatch_target(default="local")
        os.environ.update({"WK_TARGET": resolved})
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
            from wk import workspace
            try:
                workspace.refuse_unsaved_before_forward(registry(), positionals(args))
            except act.Refused as e:
                raise Exit(e.status)
            rc = forward_status(inv, cmd, args)
            if rc != 0:
                raise Exit(rc)
            if name:
                sshalias.alias_remove(Local(), os.environ, name)
            raise Exit(0)
        if d.bare == "merged" and not positionals(args):
            bare_report(inv, cmd, args)
        if d.is_readonly(sub) and not shutil.which("podman"):
            warn("podman is not installed, so there are no container workspaces to read")
            log("  './setup' installs it; 'WK_TARGET=vm wk ls' lists the macOS guests, which do not need it")
            raise Exit(0)
        forward_to_vm(inv, cmd, args)

    base = name_decl.split("@")[0]
    if base == "derived":
        name = name or derived
    elif base in ("required", "optional") and not name:
        a = name_in_argv(base, slot, takes, args)
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
    args = inv.take_config(args)
    os.execv(str(impl), [str(impl), *argv_split(d.opts_for(args), args)])


def ask_target(inv, resolved, name, exists, ready):
    """Whether `name` is on `resolved` (a machine that did not answer is no absence), then its readiness, from one load."""
    try:
        target = registry().load(resolved)
        if exists and not registry().exists_on(target, name):
            inv.usage_die(no_such_workspace(name))
        if ready:
            target.wait_ready(name, clock.Clock())
    except LookupError as e:
        die(str(e))
    except act.Refused as e:
        raise Exit(e.status)


def decl_name(inv, name_decl, slot, takes, derived):
    if name_decl.split("@")[0] == "derived":
        return derived
    if slot > 0:
        return name_in_argv(name_decl.split("@")[0], slot, takes, inv.args) or cwd_workspace() or ""
    return ""


def resolve_target(inv, name_decl, slot, takes, derived):
    inherited = dispatch_target()
    if inherited:
        return inherited
    args = inv.args
    named = D.Args(inv.decl, argv_split(inv.decl.opts_for(args), args)).value("--target")
    if named:
        return named
    if name_decl.split("@")[0] == "derived":
        t = inv.named_target()
        if t:
            return t
    name = decl_name(inv, name_decl, slot, takes, derived)
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
    except (Exit, act.Refused) as e:
        sys.exit(e.status)
    except KeyboardInterrupt:
        sys.exit(130)
