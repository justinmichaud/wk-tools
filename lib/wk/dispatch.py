"""The dispatcher: parses an invocation against its command's table (decl.py), refuses what the table does not declare,
resolves the workspace and the machine holding it, and runs the command's function there: here, forwarded into the
podman VM on a macOS host, or handed to that machine's own wk."""

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from wk import act, clock, completion, decl, guest, images, places, presets, project, repos, setupstate
from wk.act import info, log, warn
from wk.lock import Lock
from wk.machine import Local, Ssh, is_macos
from wk.reach import Reach
from wk.store import Store, build_preset, dispatch_place, in_vm, no_such_workspace, ws_name

ROOT = Path(images.root())
MACHINE = Store().podman_machine()
_registry = None
DISPATCH_VARS = ("WK_NAME", "WK_PLACE", "WK_DRIVER", "WK_ROOT", "WK_FORCE", "WK_QUIET", "WK_DRY_RUN", "WK_DESTRUCTIVE",
                 "WK_CONFIRMED", "WK_ROW_LABEL", "WK_HOST_SELF", "WK_IN_VM", "WK_PRESET", "WK_EXPORTS_READ")
SETUP_SECONDS = 3600
RELAYED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
Exit = act.Refused
die = act.die


def registry():
    """This invocation's one Registry: every place is loaded, and every machine probed, at most once."""
    global _registry
    if _registry is None:
        _registry = places.Registry(ROOT)
    return _registry


def in_workspace():
    return registry().in_workspace()


def wk_self():
    return registry().workspace_name()


def cwd_workspace():
    """On a build box, the workspace whose directory this is ($PWD, so a symlinked root is named as the marker names it)."""
    if not registry().in_remote_host():
        return ""
    pwd, cwd = os.environ.get("PWD", ""), os.getcwd()
    try:
        cwd = pwd if pwd and os.path.samefile(pwd, cwd) else cwd
    except OSError:
        pass
    prefix = registry().far_root() + "/ws/"
    return (cwd + "/")[len(prefix):].split("/")[0] if (cwd + "/").startswith(prefix) else ""


def tty():
    return os.isatty(0) and os.isatty(1)


class Invocation:
    """One command line: its module, the table its verbs and options make, its Args, and the words a far wk is handed."""

    def __init__(self, cmd, words):
        self.cmd, self.words, self.place = cmd, list(words), ""
        self.module = decl.load(ROOT, cmd)
        self.table = self.module.COMMAND
        rest = [w for w in self.words if w not in decl.HELP]
        if not callable(self.table.where) and rest == self.words:   # before the arguments: a host command's are not this place's
            refuse_where(cmd, self.words, self.table, self.table.where)
        try:
            self.command, self.args, self.argv = decl.parse(cmd, self.table, self.words, in_workspace(), bool(cwd_workspace()))
        except decl.Usage as e:
            if len(rest) == len(self.words):
                self.usage_die(str(e), e.line)
            if rest:
                self.refused(str(e), e.line)
            explain(cmd, self.module, "  this command line is refused, as above\n" if rest else "")
        if self.args.help:
            explain(cmd, self.module, self.preview() if rest else "")

    def refused(self, why, line=""):
        if why.startswith("unexpected argument") and in_workspace():
            why += " (this is workspace '%s', so there is no workspace argument in here)" % wk_self()
        if why:
            warn(why)
        log("usage: %s\n       wk %s -h for the rest" % ("\n       ".join([line] if line else decl.leaves(self.cmd, self.table)), self.cmd))

    def usage_die(self, why="", line=""):
        self.refused(why, line)
        raise Exit(2)

    def where(self):
        w = self.command.where
        return w(self.args, registry()) if callable(w) else w

    def preview(self):
        a, where = self.args, self.where()
        return "  this command line runs on: %s\n%s%s" % (where_prose(self.command, where),
                                                         "    workspace: %s\n" % a.name if a.name else "",
                                                         "    preset: %s\n" % a.preset if a.preset else "")

    def vm_side(self):
        """Whether the workspace is a container a macOS host's podman VM holds."""
        return is_macos() and not in_vm() and not in_workspace() and self.place == "container"

    def forward(self, argv):
        """`wk <cmd> <argv>` in the podman VM, waited for: its exit status."""
        return relayed(subprocess.Popen(vm_argv(self, argv)))

    def run(self):
        raise Exit(self.module.main(self.args, self) or 0)


def where_prose(t, where):
    if callable(where):
        return "whichever the command itself answers for the rest of the arguments"
    if where == "workspace" and not t.here and t.lifecycle:
        return "the machine holding the workspace, which keeps its record: a build box or a peer workstation is handed it"
    return {"host": "this host's own hardware; refused inside a workspace and on a build machine",
            "store": "the machine holding the store -- the podman VM on macOS, this machine otherwise; refused inside a workspace",
            "local": "this machine, never forwarded"}.get(where, "the machine you type it on" if t.here else
                                                          "the workspace's place, on the machine holding it (the podman VM for a "
                                                          "container workspace on macOS; that machine's own wk when it has one)")


def facts(t):
    return ["changes things: %s" % ("no -- starts nothing, writes nothing, repairs nothing" if t.readonly else
                                    "yes -- --dry-run prints what it would run and runs none of it" if t.dryrun else
                                    "yes -- and is exempt from --dry-run (the help says why)"),
            "destructive: %s" % ("yes -- asks before it acts; --yes answers" if t.destructive else "no -- asks nothing"),
            "runs on: %s" % where_prose(t, t.where)]


def explain(cmd, module, preview=""):
    """The usage line and summary, what the table says for the command and wherever a verb or an option changes it,
    each option's help, the module's docstring, and the values a closed set takes."""
    table = module.COMMAND
    out = ["  " + f for f in facts(table)] + (["  acts on: a workspace holding %s" % " or ".join(table.repos)] if table.repos else [])
    for label, t, opts in decl.variants(cmd, table):
        own = [f for f in facts(t) if f not in facts(table)]
        if label != decl.usage(cmd, table) or opts:
            out += [label] + ["    " + f for f in own]
        out += ["    %-24s %s" % (" ".join(filter(None, (o.flag, ("[=%s]" if o.optional else "%s") % o.value if o.value else ""))),
                                  o.help) for o in opts]
    out += ["", "what it does (from cmd/%s):" % cmd] + [("  " + line).rstrip() for line in decl.body(module).splitlines()]
    if table.preset:
        out += ["", "valid values (%s):" % ("--preset" if table.preset == "--preset" else "<preset>")]
        out += [("  " + line).rstrip() for line in presets.LIST_TEXT.splitlines()]
    if any(o.flag == "--repo" for o in table.opts):
        out += ["", "valid values (--repo):"] + ["  %s%s" % (r, " (the default)" if r == project.get("REPO") else "") for r in repos.names()]
    if table.values:
        r = Local().run([str(ROOT / "wk"), cmd, table.values])
        out += ["", "valid values (wk %s %s):" % (cmd, table.values)] + ["  " + line for line in (r.out + r.err).splitlines()]
    sys.stdout.write("usage: %s\n  %s\n\n%s%s\n" % (decl.usage(cmd, table), decl.summary(module), preview, "\n".join(out)))
    raise Exit(0)


def usage():
    err = sys.stderr
    err.write("usage: wk <command> [args]\n")
    tables = [(name, m) for name, m in decl.commands(ROOT)]
    for group, title in (("workspaces", "workspaces"), ("sources", "sources and disk"), ("hosts", "hosts and machines"),
                         ("other", "other")):
        rows = [(name, m) for name, m in tables if m.COMMAND.group == group]
        if rows:
            err.write("\n%s\n" % title)
            for name, m in rows:
                err.write("  %-32s %s\n" % (decl.usage(name, m.COMMAND)[3:], decl.summary(m)))
    err.write("""
  Inside a workspace the name is implicit: `wk build <preset>`, `wk run -- <args>`
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
                           machine ('wk key sudo'), every workspace on
                           every place ('wk sync', 'wk rm'), every line of
                           the log ('wk status <ws> --log --all'); a command's own -h says what
                           its --all covers
  WK_DEBUG=1               verbose output

  wk help                  README.md: architecture, setup, every workflow by example
  wk completion bash|zsh   shell completion for wk; shell/bashrc evals it already
""")
    raise Exit(2)


def completion_cmd(args):
    if args[:1] in (["-h"], ["--help"], ["--explain"]):
        sys.stdout.write("wk completion bash|zsh -- print a shell completion script for wk\n\n"
                         "  changes things: no -- prints a script for your shell's rc to eval\n"
                         "  runs on: this machine, never forwarded\n\n"
                         "  Commands, verbs and flags come from each command's table; workspace names are read\n"
                         "  from this machine's own stores at each TAB press, never by asking a machine.\n")
        raise Exit(0)
    if len(args) != 1 or args[0] not in completion.SHELLS:
        die("usage: wk completion bash|zsh -- print a shell completion script for wk; see wk completion -h", 2)
    sys.stdout.write(completion.generate(ROOT, args[0]))
    raise Exit(0)


def help_doc(topic):
    text = (ROOT / "README.md").read_text()
    if not topic:
        from wk import fleet
        text = "%s\n\n%s" % (fleet.self_line(str(ROOT), os.environ), text)
        if os.isatty(1) and shutil.which("less"):
            raise Exit(subprocess.run(["less"], input=text, text=True).returncode)
        sys.stdout.write(text)
        raise Exit(0)
    marks = [(m.start(), m.group(0)) for m in re.finditer(r"^(## .+|\*\*[^*]+\*\*)$", text, re.M)]
    out = []
    for i, (start, head) in enumerate(marks):
        end = next((later for later, h in marks[i + 1:] if not head.startswith("##") or h.startswith("##")), len(text))
        if topic.lower() in head.strip("#* ").lower():
            out.append(text[start:end].rstrip() + "\n")
    if not out:
        sys.stderr.write("error: no help topic matches '%s'. Topics (wk help <word>):\n%s"
                         % (topic, "".join("    %s\n" % h.strip("#* ") for _, h in marks)))
        raise Exit(1)
    sys.stdout.write("\n".join(out))
    raise Exit(0)


def podman_vm():
    return places.podman_vm(Local(), MACHINE) if shutil.which("podman") else None


def start_podman_machine(machine, cmd, terminal):
    if not terminal and not act.dry_run():
        die("the podman machine '%s' is stopped, and 'wk %s' needs it.\n"
            "    Nothing here starts it without a terminal asking:  wk start" % (MACHINE, cmd))
    guest.podman_admit(places.Registry(ROOT, os.environ, machine).load("vm"))
    info("starting podman machine '%s'" % MACHINE)
    machine.act_run(["podman", "machine", "start", MACHINE], tty=True)


def vm_argv(inv, argv):
    """The ssh that runs `wk <cmd> <argv>` in the podman VM, starting a stopped one first unless the command only reads."""
    readonly = inv.command.readonly
    if not shutil.which("podman"):
        if readonly:
            warn("podman is not installed, so there are no container workspaces to read")
            log("  './setup' installs it; 'WK_PLACE=vm wk ls' lists the macOS guests, which do not need it")
            raise Exit(0)
        die("podman is required: install the official pkg from podman.io, then ./setup")
    rec = podman_vm()
    if rec is None:
        die("podman machine '%s' does not exist -- run ./setup first" % MACHINE)
    if rec.get("State") != "running":
        if readonly:
            warn("the podman machine '%s' is stopped, so its store cannot be read" % MACHINE)
            log("  'wk start' to bring it up -- 'wk %s' will not start it" % inv.cmd)
            raise Exit(0)
        start_podman_machine(Local(), inv.cmd, tty())
    line = registry().load("container").wk_cmd([inv.cmd, *argv], os.environ)
    if tty() or inv.command.session:   # a session's far end needs a pty: the client's death is then its SIGHUP
        opts, dest = places.podman_vm_route(rec)
        return ["ssh", "-tt" if inv.command.session else "-t", *opts, dest, line]
    return Ssh.podman(MACHINE).argv(line)


def relayed(child):
    """Wait for `child`, handing it each TERM, INT and HUP this process gets: a far `wk ai` ends with this one."""
    saved = {s: signal.signal(s, lambda signum, frame: child.send_signal(signum)) for s in RELAYED}
    try:
        return child.wait()
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


def exec_flushed(argv):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(argv[0], argv)


def delegate_driver(place):
    """The place's driver when it runs commands itself, else None."""
    try:
        t = registry().load(place)
    except LookupError:
        return None
    return t if t.delegates() else None


def delegate_run(driver, cmd, args, readonly=False, session=False):
    exec_flushed(driver.hand_over(cmd, args, tty="force" if session else tty(), readonly=readonly))


def check_needs(cmd, t, machine=None):
    machine, missing = machine or Local(), []
    for n in t.needs:
        if n == "gh-auth":
            from wk.doctor import gh_authenticated
            if not gh_authenticated():
                missing.append("gh-auth    gh cannot reach the GitHub API (not logged in, or the token expired): gh auth login")
        elif n == "tailnet":
            if not Reach(machine).peers():
                missing.append("tailnet    this machine is not on the tailnet: tailscale up")
        elif n == "quiesce-helper":
            from wk.sudo import QUIESCE_PRIV
            if not (os.access(QUIESCE_PRIV, os.X_OK) and machine.run(["sudo", "-n", QUIESCE_PRIV, "status"]).ok):
                missing.append("quiesce-helper    the privileged quiesce/session helper is not set up: ./setup --stage quiesce")
        elif not shutil.which(n):
            missing.append("%s    not installed here" % n)
    if missing:
        die("'wk %s' cannot start here:%s\n    wk doctor says how to get each of these." % (cmd, "".join("\n    " + m for m in missing)))


def converge_setup(cmd, t, machine=None):
    """./setup where it is behind; a timeout gives it a session with no terminal, which sudo asks a password on."""
    if t.readonly or in_workspace() or in_vm() or registry().in_remote_host():
        return
    machine = machine or Local()
    why = setupstate.behind(str(ROOT), os.environ, machine)
    if not why:
        return
    if os.environ.get("WK_BROKERED"):
        info("this machine's setup is behind (%s); it runs with the next command typed on it, not under the broker" % why)
        return
    info("this machine's setup is behind (%s), so ./setup runs before 'wk %s'" % (why, cmd))
    if act.dry_run():
        machine.act_run([str(ROOT / "setup")])
        return
    with Lock(registry().store, machine, clock.Clock()).held("setup", timeout=SETUP_SECONDS):
        if setupstate.behind(str(ROOT), os.environ, machine):
            if not machine.act_run([str(ROOT / "setup")], input="", stream=True, timeout=SETUP_SECONDS).ok:
                warn("./setup did not finish (above); 'wk %s' goes on, and 'wk doctor' says what is left" % cmd)


def refuse_preset(inv, inherited):
    """A build preset reaches the command as WK_PRESET: one presets does not hold, or an exported one argv does not name, is refused."""
    t, given = inv.command, inv.args.preset
    if given is not None and given not in presets.names():
        inv.usage_die("unknown preset: %s -- 'wk build --list' names every one" % given)
    slot = (t.preset == "--preset" and any(o.flag == "--preset" for o in t.opts)) or (t.preset == "arg" and t.takes)
    if inherited and slot and given != inherited:
        remedy = "--preset %s" % inherited if t.preset == "--preset" else \
            "wk %s%s %s" % (inv.cmd, "" if in_workspace() or cwd_workspace() else " <workspace>", inherited)
        inv.usage_die("WK_PRESET=%s is set in this environment, and wk takes a build preset only from\n"
                      "    its arguments: unset it, or name the preset:  %s" % (inherited, remedy))
    if given:
        os.environ["WK_PRESET"] = given


def refuse_repo(inv, place, name):
    """`repos`: a workspace holding a repo the command does not serve is refused, naming what it holds."""
    if not inv.command.repos:
        return
    try:
        held = registry().load(place).repo(name).name
    except LookupError as e:
        die(str(e))
    if held not in inv.command.repos:
        die("'wk %s' acts on a %s workspace, and '%s' holds %s" % (inv.cmd, " or ".join(inv.command.repos), name, held))


def ask_place(inv, resolved, name, exists, ready):
    """Whether `name` is on `resolved` (a machine that did not answer is no absence), then its readiness, from one load."""
    try:
        driver = registry().load(resolved)
        if exists and not registry().exists_on(driver, name):
            inv.usage_die(no_such_workspace(name))
        if ready:
            driver.wait_ready(name, clock.Clock())
    except LookupError as e:
        die(str(e))


def resolve_place(args, name, derived_place=""):
    named = dispatch_place() or args.value("--on") or derived_place
    if named:
        return named
    if name:
        try:
            return registry().ws_place(name)
        except LookupError as e:
            die(str(e))
    return "container"


def refuse_where(cmd, words, t, where):
    if in_workspace() and (where in ("host", "store") or t.outside) and not t.broker:
        die("'wk %s' acts on a host, and this is workspace '%s'.\n    From the host:  wk %s %s"
            % (cmd, wk_self(), cmd, shlex.join(words) if not t.outside else wk_self()))
    if registry().in_remote_host() and where == "host":
        die("'wk %s' acts on a workstation's own store or hardware, and this is\n"
            "    the shared build machine for place '%s'.\n"
            "    Run it on the workstation instead. What works here: ls, status, build,\n"
            "    run, test, enter, new, rm." % (cmd, registry().self_place()))


BUILTINS = {"help": lambda args: help_doc(args[0] if args else ""),
            "setup": lambda args: os.execv(str(ROOT / "setup"), [str(ROOT / "setup"), *args]),
            "completion": completion_cmd}
BUILTINS["-h"] = BUILTINS["--help"] = BUILTINS["help"]


def main(argv):
    if not argv:
        usage()
    cmd = argv[0]
    if cmd in BUILTINS:
        BUILTINS[cmd](argv[1:])
    if not (ROOT / "cmd" / cmd).is_file():
        warn("unknown command: %s" % cmd)
        usage()
    try:
        registry().self_place()
    except LookupError as e:
        die(str(e))
    inv = Invocation(cmd, argv[1:])
    t, a = inv.command, inv.args
    for flag in a.globals:
        os.environ[decl.GLOBALS[flag]] = "1"
    ws_name(take=True)
    inherited = build_preset(take=True)
    where = inv.where()
    refuse_where(cmd, inv.words, t, where)
    refuse_preset(inv, inherited)
    if act.dry_run() and not t.dryrun:
        inv.usage_die("'wk %s' is exempt from --dry-run (wk %s -h says why)" % (cmd, cmd))
    converge_setup(cmd, t)
    if t.destructive:
        os.environ["WK_DESTRUCTIVE"] = "1"

    if where != "workspace":
        if where == "store" and not in_vm() and not registry().store.is_local():
            exec_flushed(vm_argv(inv, inv.argv))
        check_needs(cmd, t)
        inv.run()
    name, derived = a.name, callable(t.name)
    if derived:
        name, place = t.name(a, registry())
    if in_workspace():
        inv.place, name = dispatch_place(default="local"), wk_self()
    else:
        name = name or (cwd_workspace() if t.named() else "")
        inv.place = resolve_place(a, name, place if derived else "")
        far = t.name != "none" and not in_vm() and not t.here and not t.lifecycle and delegate_driver(inv.place)
        if far:
            delegate_run(far, cmd, inv.argv, readonly=t.readonly, session=t.session)
        if inv.vm_side() and t.forward and not t.here:
            exec_flushed(vm_argv(inv, inv.argv))
    check_needs(cmd, t)
    if name:
        os.environ["WK_NAME"] = name
        if not t.lifecycle:
            os.environ["WK_PLACE"] = inv.place
        asks = not t.lifecycle and not derived and not in_workspace()
        if asks or t.ready:
            ask_place(inv, inv.place, name, asks, t.ready)
        refuse_repo(inv, inv.place, name)
    inv.run()


def entry():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # `wk help | head` ends quietly, as a shell tool does
    try:
        main(sys.argv[1:])
    except Exit as e:
        sys.exit(e.status)
    except KeyboardInterrupt:
        sys.exit(130)
