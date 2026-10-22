"""The lint pass: one row per rule (`lint.<rule>`), each a function over the tree returning what breaks it.
`python3 tests/run.py --lint -k <rule>` runs one."""
TIER = "lint"
import ast
import collections
import functools
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import unittest
from pathlib import Path

from tests.support import FLEET_ENV, INTERNAL_VARS, REAL_MACHINES, REPO, bash, clean_env, owed, repo_files, shell_files

sys.path.insert(0, str(REPO / "lib"))
from wk import boot, decl, doctor, fleet, gc, images, places  # noqa: E402
from wk.boot import cli as boot_cli  # noqa: E402
from wk.places import CONF_ENV  # noqa: E402
from wk.store import Store  # noqa: E402

SELF = "tests/test_lint.py"
README_MARKER = "*** Claude edit below here ***"
RULES = {}


def rule(owed_because=None):
    def add(fn):
        RULES[fn.__name__] = (fn, owed_because)
        return fn
    return add


@functools.cache
def tracked():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    return sorted(rel for rel in set(out) if (REPO / rel).is_file() and not (REPO / rel).is_symlink())


def text_of(rel):
    try:
        return (REPO / rel).read_text(errors="replace")
    except OSError:
        return ""


def users_text(rel):
    """A file's text, README.md's spec half (above the marker, the user's) blanked to keep line numbers."""
    text = text_of(rel)
    if rel == "README.md":
        head, _, rest = text.partition(README_MARKER)
        text = "\n" * head.count("\n") + rest
    return text


def code(line):
    return not line.lstrip().startswith("#")


def python_sources():
    yield from sorted((REPO / "lib" / "wk").rglob("*.py"))
    yield from sorted(p for p in (REPO / "cmd").iterdir() if p.is_file())


def lines_hits(paths, broken):
    hits = []
    for p in paths:
        hits += ["%s:%d: %s" % (p.relative_to(REPO), n, l.strip()[:90])
                 for n, l in enumerate(p.read_text(errors="replace").splitlines(), 1) if broken(p, l)]
    return hits


# lint.command_names: every `wk <command> [<verb>]` a shipped file tells someone to run names a command that exists.
COMMAND_REF = re.compile(r"""(?:['`]|:\s+|\w=\S*\s+|/\s)wk ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?""")


@rule()
def command_names():
    decls = {name: m.COMMAND for name, m in decl.commands(REPO)}
    out = []
    for rel in tracked():
        if rel.startswith(("docs/", "tests/", "CLAUDE.md")):
            continue
        text = users_text(rel)
        for m in COMMAND_REF.finditer(text):
            cmd, verb = m.groups()
            d = decls.get(cmd)
            if cmd in ("help", "completion") or d and not (d.subs and not d.default and verb and verb not in d.subs):
                continue
            out.append("%s:%d: wk %s" % (rel, text.count("\n", 0, m.start()) + 1, " ".join(filter(None, m.groups()))))
    return out


# lint.effects_through_machine: a process, a file write, a removal or an exec goes through lib/wk/machine.py, or is named.
EFFECT = re.compile(
    r"\bsubprocess\.(run|Popen|call|check_call|check_output)\("
    r"|\bos\.(remove|unlink|rename|replace|exec\w*|system|rmdir|makedirs|mkdir|symlink|chmod|kill|truncate)\("
    r"|\bshutil\.(rmtree|copy\w*|move)\("
    r"|\bopen\([^)]*,\s*[\"'][wax]b?\+?[\"']"
    r"|\.write_(text|bytes)\(|\.(unlink|rmdir|symlink_to|touch)\(")
NOT_STATE = {
    "lib/wk/dispatch.py": "the dispatcher execs into the command it resolved, and probes for it",
    "lib/wk/job.py": "the job's own log, opened only past its dry-run check",
    "lib/wk/places.py": "the ProxyCommand exec that is the route",
    "lib/wk/tools.py": "removes its own temporary bundle",
    "lib/wk/bench/mac.py": "removes its own temporary tar",
    "lib/wk/slot.py": "`python3 -m wk.slot`: a step's tool writing the output its caller named",
    "lib/wk/mac.py": "`python3 -`: the program a Machine runs on the Mac (autorun's `sudo wkmac display-mode --declare`); the effect is that act_run",
    "lib/wk/pgo.py": "`python3 -m wk.pgo`: a step's tool writing the output its caller named",
    "lib/wk/bench/cli.py": "copies into its own temporary directory",
    "lib/wk/publish.py": "a host daemon's socket forward into the podman machine, held for the daemon's life",
}
PROCESS = re.compile(r"\bsubprocess\.(run|Popen|call|check_call|check_output)\b|\bos\.(exec\w*|system|spawn\w*|posix_spawn\w*|popen)\(")
FORCED = {
    "lib/wk/dispatch.py": "the dispatcher starts the command it resolved under --dry-run too, since that command is what honours it",
    "lib/wk/places.py": "the ProxyCommand exec is the transport a dry run's reads also travel",
    "lib/wk/mac.py": "a program sent whole to a Mac's `python3 -`, where no wk package is importable",
    "lib/wk/publish.py": "a daemon's (the broker's, the injector's) forward, outside any wk command and its dry run",
}


def files_matching(pattern, top):
    files = list((REPO / top).rglob("*.py")) + [p for p in (REPO / "cmd").iterdir() if p.is_file()]
    return {str(p.relative_to(REPO)) for p in files if str(p.relative_to(REPO)) != "lib/wk/machine.py"
            and any(pattern.search(l) and code(l) for l in p.read_text(errors="replace").splitlines())}


def named_exactly(found, named, what):
    return (["%s %s outside lib/wk/machine.py: route it through the Machine, or name why" % (f, what) for f in sorted(found - set(named))]
            + ["%s is named but no longer %s directly: remove it" % (f, what) for f in sorted(set(named) - found)])


@rule()
def effects_through_machine():
    return named_exactly(files_matching(EFFECT, "lib/wk"), NOT_STATE, "acts")


@rule()
def processes_through_machine():
    return named_exactly(files_matching(PROCESS, "lib"), FORCED, "starts a process")


# lint.layering: README's layers -- a lab module names no WebKit and imports no wk or field module; nothing imports field.
LAYERS = {
    "lab": ("lib/common.sh", "lib/credcheck.py", "lib/secretfile.py", "lib/treehash.py", "lib/wkdata.py", "lib/wk/"),
    "wk": ("lib/wk/build.py", "lib/wk/pgo.py", "lib/wk/pr.py", "lib/wk/presets.py",
           "lib/wk/profile.py", "lib/wk/slot.py", "lib/wk/bench/ab.py", "lib/wk/bench/autorun.py",
           "lib/wk/bench/board_driver.py", "lib/wk/bench/change.py", "lib/wk/bench/mac_ab.py", "lib/wk/bench/mac_pgo.py",
           "lib/wk/bench/plans.py", "lib/wk/bench/scores.py", "lib/wk/bench/seed.py",
           # The image recipes: WebKit's own Tools/yocto and cross-toolchain-helper, and buildroot's wpewebkit package.
           "lib/wk/sysimage/buildroot.py", "lib/wk/sysimage/buildroot_ws.py", "lib/wk/sysimage/yocto.py", "lib/wk/sysimage/yocto_ws.py",
           "lib/wk/sync.py"),
    "field": ("lib/wk/bench/report.py",),
    "cli": ("cmd/", "lib/wk/dispatch.py", "lib/wk/completion.py", "lib/wk/disk.py", "lib/wk/gc.py", "lib/wk/bench/cli.py",
            "lib/wk/sysimage/cli.py"),
}
WEBKIT = re.compile(r"webkit|javascriptcore|minibrowser|tools/scripts|tools/(gtk|wpe)\b|\bjsc\b|run-benchmark|cross-toolchain-helper|wkdev"
                    r"|motionmark|speedometer|jetstream|octane|kraken|sunspider|ares6|jsbench|\b(mac|ios-sim|gtk|wpe|jsc)-(debug|release|cross)", re.I)


def layer_of(rel):
    best = max((p for ps in LAYERS.values() for p in ps if rel == p or (p.endswith("/") and rel.startswith(p))), key=len, default=None)
    return next((layer for layer, ps in LAYERS.items() if best in ps), None)


def module_path(dotted):
    base = REPO / "lib" / dotted.replace(".", "/")
    return next((p.relative_to(REPO).as_posix() for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None)


def imported(rel, path):
    pkg = ".".join(rel.split("/")[1:-1]) if rel.startswith("lib/") else ""
    found = set()
    for n in ast.walk(ast.parse(path.read_text(), str(path))):
        names = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")[:len(pkg.split(".")) - (n.level - 1)]
                base = ".".join(parts + ([n.module] if n.module else []))
            names = [base] + ["%s.%s" % (base, a.name) for a in n.names]
        found.update(m for m in map(module_path, names) if m)
    return found - {rel}


@rule()
def layering():
    out = []
    sources = [p for p in sorted((REPO / "lib").rglob("*")) if p.suffix in (".py", ".sh")]
    for path in sources + sorted(p for p in (REPO / "cmd").iterdir() if p.is_file()):
        rel = path.relative_to(REPO).as_posix()
        layer = layer_of(rel)
        if layer is None:
            out.append("%s has no layer" % rel)
            continue
        if layer == "lab":
            words = sorted({m.group(0).lower() for m in WEBKIT.finditer(path.read_text(errors="replace"))})
            if words:
                out.append("%s names %s: move it into lib/wk/project.json" % (rel, ", ".join(words)))
        if path.suffix != ".sh":
            below = {"lab": ("wk", "field", "cli"), "wk": ("field", "cli"), "field": ("cli",), "cli": ()}[layer]
            bad = sorted(m for m in imported(rel, path) if layer_of(m) in below and layer_of(m) != layer)
            if bad:
                out.append("%s imports %s (README.md, Layers)" % (rel, ", ".join(bad)))
    return out


# lint.live_runs_on_the_container_target: no test is gated on the podman VM; the gate is requires_container_place.
VM_GATES = re.compile(r"\b(requires_podman_vm|_needs_podman_vm|podman_vm_running|podman_vm_ssh)\b")


@rule()
def live_runs_on_the_container_target():
    out = []
    for p in sorted((REPO / "tests").glob("*.py")):
        if p.name == Path(SELF).name:
            continue
        for n, line in enumerate(p.read_text().splitlines(), 1):
            m = VM_GATES.search(line)
            if m and not (p.name == "support.py" and m.group(1) == "podman_vm_running"):
                out.append("%s:%d: %s" % (p.name, n, line.strip()))
    return out


# lint.one_machine_name_reader: this host's name is read by record.host_name alone.
class _NameReads(ast.NodeVisitor):
    def __init__(self):
        self.where, self.found = ["<module>"], set()

    def visit_FunctionDef(self, node):
        self.where.append(node.name)
        self.generic_visit(node)
        self.where.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node):
        if node.attr == "nodename":
            self.found.add(self.where[-1])
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and "%s.%s" % (f.value.id, f.attr) in ("socket.gethostname", "platform.node"):
            self.found.add(self.where[-1])
        self.generic_visit(node)

    def visit_List(self, node):
        if node.elts and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "hostname":
            self.found.add(self.where[-1])
        self.generic_visit(node)

    visit_Tuple = visit_List


@rule()
def one_machine_name_reader():
    found = []
    for path in python_sources():
        r = _NameReads()
        r.visit(ast.parse(path.read_text(errors="replace"), str(path)))
        found += ["%s %s" % (path.relative_to(REPO), fn) for fn in sorted(r.found)]
    return [f + ": call record.host_name instead" for f in found if f != "lib/wk/record.py host_name"]


RUNS_HOSTNAME = re.compile(r"(^|[;&|({`$]|\bthen|\bdo)\s*hostname(\s+-s)?\s*(2>|\||\)|;|$)")


@rule()
def one_machine_name_reader_in_bash():
    """lib/common.sh's wk_machine_name is the bash spelling of this machine's name."""
    files = [p for p in shell_files() if p.relative_to(REPO).parts[0] in ("lib", "cmd", "host", "setup")]
    return lines_hits(files, lambda p, l: code(l) and RUNS_HOSTNAME.search(l))


# lint.no_addresses: a node is reached by its tailnet name, and how to reach it is not written down.
EXCEPTED_HOSTS = {"buildbox4", "devbox-arm64-2", "devbox-armhf-2"}
MAC_ADDR = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
LOCAL_NAME = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*\.local\b(?![/\w])")
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def address_hits(text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        hits += [(n, "MAC " + m) for m in MAC_ADDR.findall(line)
                 if not (set(m.replace(":", "")) <= {"0"} or m.lower().startswith("00:00:00:00:00:"))]
        hits += [(n, "name " + m.group(0)) for m in LOCAL_NAME.finditer(line)]
    return hits


def ssh_config_hits(text):
    hits, hosts = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        key, args = parts[0].lower(), parts[1:]
        if key == "host":
            hosts = args
        elif not set(hosts) <= EXCEPTED_HOSTS:
            if key in ("proxyjump", "hostkeyalias"):
                hits.append((n, "%s in Host %s" % (parts[0], " ".join(hosts))))
            elif key == "hostname" and args and IPV4.match(args[0]):
                hits.append((n, "address %s in Host %s" % (args[0], " ".join(hosts))))
    return hits


@rule(owed_because="five board confs still carry a MAC: closed once the boards join the tailnet by image")
def no_addresses():
    conf = REPO / "dotfiles" / "ssh" / "config"
    hits = ["%s:%d: %s" % (conf.relative_to(REPO), n, w) for n, w in ssh_config_hits(conf.read_text()) + address_hits(conf.read_text())]
    for p in sorted((REPO / "machines").glob("*.conf")) + [p for p in sorted((REPO / "lib").rglob("*")) if p.is_file() and "__pycache__" not in p.parts]:
        hits += ["%s:%d: %s" % (p.relative_to(REPO), n, w) for n, w in address_hits(p.read_text(errors="replace"))]
    return hits


# lint.referenced_paths_exist: a path named under /opt/wk-tools/ or container/bin/ is tracked, so a push (commits
# alone) cannot leave a workspace's entry broken.
REFERENCE = re.compile(r"(?:/opt/wk-tools/|(?<![\w/])container/bin/)([A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])")
RUNTIME = {".wk-write-probe"}   # lib/wk/wall.py touches and removes it to measure the mount, so no file holds it


@rule()
def referenced_paths_exist():
    committed = set(subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached"], capture_output=True,
                                   text=True, check=True).stdout.split("\0"))
    missing = set()
    for path in repo_files():
        if path.suffix in (".png", ".jpg", ".gz", ".xz", ".zip") or ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in REFERENCE.finditer(text):
            rel = ("container/bin/" + m.group(1)) if m.group(0).startswith("container/bin/") else m.group(1)
            if any(c in rel for c in "<*%$") or rel.split("/")[-1] in RUNTIME:
                continue
            if rel not in committed and not any(t.startswith(rel + "/") for t in committed):
                missing.add("%s: %s" % (path.relative_to(REPO), rel))
    return sorted(missing)


# lint.vocabulary: one spelling per concept, in no file outside docs/ and CLAUDE.md.
class Unless:
    """`bad`, once every `ok` span is blanked: a retired word inside a phrase that is another program's stays legal."""

    def __init__(self, bad, ok):
        self.bad, self.ok = re.compile(bad), re.compile(ok)

    def search(self, line):
        return self.bad.search(self.ok.sub(lambda m: " " * len(m.group(0)), line))


TARGET_OK = (r"(?i)\b(?:build|cmake|make|ninja|cross(?:-build)?|systemd)[- ]targets?\b"
             r"|\btargets? (?:triple|tuple|device)\b|\btargets\.conf\b|\bcross-target\b|\bport-target\b|\btarget-finalize\b"
             r"|\b(?:multi-user|default|local-fs|network(?:-online|-pre)?|sysinit|basic|timers|swap|sleep|suspend|hibernate|hybrid-sleep)\.target\b"
             r"|WantedBy=\S*|settings set target\.\S*|\btarget\.process\b|invalid target|\b(?:assign|n)\.targets\b"
             r"|\bThread\(target=|(?:^|[(,])\s*target=\w+(?:\.\w+)+|\b(?:pip install|rustc|cargo|findmnt|yocto_ws\.py)\b.*--target\b")
# Where "target" is another domain's own word for something that is neither a place nor a driver.
TARGET_DOMAINS = {
    "a symlink's target": ("wk", "bin/wk", "lib/wk/machine.py", "lib/wk/lock.py", "tests/test_wk_machine.py", "tests/test_wk_lock.py"),
    "an HTTP request-target": ("container/proxy/", "tests/test_egress.py"),
    "lldb's target": ("container/lldb/",),
    "a mount point or disk": ("lib/wk/mac.py", "lib/wk/sysimage/macvolume.py", "host/macos/machine.sh", "host/macos/vmtools.sh",
                              "tests/test_machine_mounts.py", "admin/wk-card-priv"),
    "the cross target an image is built for": ("lib/wk/sysimage/yocto_ws.py", "lib/wk/sysimage/buildroot_ws.py", "image/",
                                               "container/yocto/", "container/buildroot/", "tests/test_yocto_ws.py",
                                               "tests/test_yocto_stage.py", "tests/test_board_local_conf.py",
                                               "tests/test_buildroot_ws.py"),
    "a systemd target": ("boot/firstboot/", "lib/wk/daemons.py", "host/linux/rpi5/"),
    "where a copy or a credential lands": ("tests/test_boot_priv.py", "lib/wk/key/creds.py"),
}
RETIRED = {
    "say 'image workspace' (image_ws), or the precise word for the meaning": re.compile(r"(?i)\blanes?\b"),
    "say 'bench task'": re.compile(r"(?i)\bbenchmark tasks?\b|\bbench jobs?\b"),
    "say 'snapshot'": re.compile(r"(?i)\bbase[ -]snapshots?\b"),
    "say 'keyring'": re.compile(r"(?i)\bsecrets?[ _]dir(?:ector(?:y|ies))?\b|\bcredential stores?\b"),
    "say the part's name from README's Overrides (lib/wk/store.py)": re.compile(
        r"\b(?:record_dir|artifact_dir|base_dir|base_path|base_sha_file|ws_base_id|"
        r"secrets_view_dir|agent_rw_dir|push_held_dir|held_dir|broker_socket|provisioned_root|machine_store|"
        r"named_root|vm_root|container_mirror)\b"),
    "say 'place' (where a workspace lives, `--on`, WK_PLACE) or 'driver' (what makes one, WK_DRIVER)":
        Unless(r"(?i)\btargets?\b|\bWK_TARGET\w*", TARGET_OK),
    "say 'build preset' (`--preset`, WK_PRESET, `preset=`)":
        Unless(r"(?i)\bbuild[ -]configs?\b|--config\b|\bWK_CONFIG\b|\bconfig=(?:--config|arg)\b", r"rpi-eeprom-config\W.*--config\b"),
    "say 'bench machine'; `bench-device` is only the role value machines/*.conf declares":
        re.compile(r"(?i)\bbench devices?\b|(?<![\w\"'=-])bench-devices?(?![\w\"'-])"),
    "say 'A/B task'": Unless(r"(?i)\bexperiments?\b", r"Apple's experiment|\bexperiments(?==|\s+com\.apple)"),
    "say 'bench system' (the OS image a bench machine boots and measures)": re.compile(r"(?i)\bsystem images?\b"),
    "say 'warmup profile' (the recording the warmup round captures)":
        re.compile(r"(?i)\bwarm-?up (?:captures?|recordings?|traces?)\b|\btime[ -]profiles?\b"),
    "say 'image preset' (image/presets, `wk sysimage presets`)":
        re.compile(r"(?i)\bimage[ -](?:profiles?|configs?|configurations?)\b|\bsysimage configs\b|\bimage/configs\b"),
}


def target_domain(rel):
    return any(rel == f or (f.endswith("/") and rel.startswith(f)) for files in TARGET_DOMAINS.values() for f in files)


@rule()
def vocabulary():
    out = []
    for rel in tracked():
        if rel in ("CLAUDE.md", SELF) or rel.startswith(("docs/", "claude/skills/", ".claude/")):
            continue
        for n, line in enumerate(users_text(rel).splitlines(), 1):
            for why, pattern in RETIRED.items():
                if pattern.search(line) and not (why.startswith("say 'place'") and target_domain(rel)):
                    out.append("%s:%d: %s (%s)" % (rel, n, line.strip(), why))
    return out


# lint.no_wall_clock_assertions: no unit test asserts a wall-clock bound of its own; the runner's budget is the bound.
CLOCKS = {"time", "monotonic", "perf_counter", "monotonic_ns", "time_ns", "perf_counter_ns"}


def reads_clock(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in CLOCKS and isinstance(f.value, ast.Name) and f.value.id == "time":
                return True
            if isinstance(f, ast.Name) and f.id in ("monotonic", "perf_counter"):
                return True
    return False


def is_duration(node):
    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub) and (reads_clock(n.left) or reads_clock(n.right))
               for n in ast.walk(node))


def timers(tree):
    out = {}
    for f in ast.walk(tree):
        if isinstance(f, ast.FunctionDef):
            for r in ast.walk(f):
                if isinstance(r, ast.Return) and r.value is not None and is_duration(r.value):
                    elts = r.value.elts if isinstance(r.value, ast.Tuple) else None
                    out[f.name] = None if elts is None else {i for i, e in enumerate(elts) if is_duration(e)}
    return out


def durations_bound(assign, timed):
    if is_duration(assign.value):
        return {n.id for t in assign.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    call = assign.value if isinstance(assign.value, ast.Call) else None
    name = call and getattr(call.func, "attr", getattr(call.func, "id", ""))
    if not name or name not in timed:
        return set()
    out = set()
    for t in assign.targets:
        if isinstance(t, ast.Name) and timed[name] is None:
            out.add(t.id)
        elif isinstance(t, ast.Tuple) and timed[name] is not None:
            out |= {e.id for i, e in enumerate(t.elts) if i in timed[name] and isinstance(e, ast.Name)}
    return out


def fails(stmts):
    for st in stmts:
        for n in ast.walk(st):
            if isinstance(n, ast.Raise) and "AssertionError" in ast.unparse(n.exc or n):
                return True
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "fail":
                return True
    return False


def deadlines(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and reads_clock(n.test) and fails(n.body):
            yield n
        for body in (getattr(n, f, None) for f in ("body", "orelse", "finalbody")):
            if isinstance(body, list):
                for st, after in zip(body, body[1:] + [None]):
                    if isinstance(st, ast.While) and reads_clock(st.test) and (fails(st.orelse) or (after is not None and fails([after]))):
                        yield st


def start(fn):
    return min([fn.lineno] + [d.lineno for d in fn.decorator_list])


def referenced(fn):
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}


def clock_offences(source, path, units):
    tree = ast.parse(source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    checked = [f for f in funcs if start(f) in units]
    names = set().union(*(referenced(f) for f in checked)) if checked else set()
    while True:
        more = [f for f in funcs if f.name in names and f not in checked and not f.name.startswith("test")]
        if not more:
            break
        checked += more
        names |= set().union(*(referenced(f) for f in more))
    timed = timers(tree)
    out = []
    for fn in checked:
        took = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                took |= durations_bound(n, timed)
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr.startswith("assert")
                    and any(is_duration(a) or {x.id for x in ast.walk(a) if isinstance(x, ast.Name)} & took for a in n.args)):
                out.append((n.lineno, ast.unparse(n)[:120]))
        out += [(n.lineno, ast.unparse(n).splitlines()[0][:120]) for n in deadlines(fn)]
    return ["%s:%d: %s" % (path, line, text) for line, text in sorted(set(out))]


@rule()
def no_wall_clock_assertions():
    from tests.run import flatten, test_tier
    units = {}
    for t in flatten(unittest.TestLoader().discover(str(REPO / "tests"), top_level_dir=str(REPO))):
        fn = getattr(type(t), getattr(t, "_testMethodName", ""), None)
        c = getattr(inspect.unwrap(fn), "__code__", None) if fn is not None else None
        if c is not None and test_tier(t) == "unit":
            units.setdefault(os.path.realpath(c.co_filename), set()).add(c.co_firstlineno)
    out = []
    for p in sorted((REPO / "tests").glob("*.py")):
        out += clock_offences(p.read_text(), str(p.relative_to(REPO)), units.get(os.path.realpath(p), set()))
    return out


# lint.wk_overrides: every WK_* the Python core reads is read in one function, documented where the user meets it
# (README.md or a command's -h) and named in a test; every bash `${WK_X:-...}` is documented or removed.
WK_NAME = re.compile(r"WK_[A-Z0-9_]+")


def _is_wk(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and WK_NAME.fullmatch(node.value) and not node.value.endswith("_")


class _EnvReads(ast.NodeVisitor):
    def __init__(self):
        self.where, self.found = ["<module>"], {}

    def _mark(self, node):
        self.found.setdefault(node.value, set()).add(self.where[-1])

    def visit_FunctionDef(self, node):
        self.where.append(node.name)
        self.generic_visit(node)
        self.where.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        for arg in node.args[:2]:
            if _is_wk(arg):
                self._mark(arg)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        if isinstance(node.ctx, ast.Load) and _is_wk(node.slice):
            self._mark(node.slice)
        self.generic_visit(node)

    def visit_Compare(self, node):
        if _is_wk(node.left) and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            self._mark(node.left)
        self.generic_visit(node)


def read_sites():
    out = {}
    for p in list(python_sources()) + [REPO / "wk"]:
        r = _EnvReads()
        r.visit(ast.parse(p.read_text(errors="replace"), str(p)))
        for var, fns in r.found.items():
            out.setdefault(var, set()).update((str(p.relative_to(REPO)), fn) for fn in fns)
    return out


def bash_defaults():
    found = {}
    for p in repo_files():
        if p.relative_to(REPO).parts[0] in ("wk", "lib", "cmd", "build"):
            for m in re.finditer(r"\$\{(WK_[A-Z_]+):-", p.read_text(errors="replace")):
                found.setdefault(m.group(1), p.relative_to(REPO).as_posix())
    return found


def explained_in_a_comment(var):
    word = re.compile(r"\b%s\b" % re.escape(var))
    return any(any(word.search(l) for l in p.read_text(errors="replace").splitlines() if not code(l))
               for p in repo_files() if p.relative_to(REPO).parts[0] not in ("tests", "cmd") and p.name != "README.md")


@rule()
def wk_overrides():
    sites = read_sites()
    users = set(sites) - set(INTERNAL_VARS) - set(CONF_ENV.values())
    doc = "\n".join([text_of("README.md")] + [ast.get_docstring(ast.parse(p.read_text(errors="replace"))) or ""
                                               for p in sorted((REPO / "cmd").iterdir()) if p.is_file()])
    tests = "\n".join(p.read_text(errors="replace") for p in sorted((REPO / "tests").glob("*.py")) if str(p.relative_to(REPO)) != SELF)
    out = ["%s is read in %d functions" % (v, len(s)) for v, s in sorted(sites.items()) if len(s) > 1]
    out += ["%s is documented nowhere the user meets it" % v for v in sorted(users - set(WK_NAME.findall(doc)))]
    out += ["%s is named in no test" % v for v in sorted(users - set(WK_NAME.findall(tests)))]
    out += ["%s (read with a default in %s) is explained nowhere" % (v, where) for v, where in sorted(bash_defaults().items())
            if v not in CONF_ENV.values() and not re.search(r"\b%s\b" % v, doc) and not explained_in_a_comment(v)]
    return out


# The line rules: (the files each reads, the line that breaks it).
def under(*roots):
    return lambda: [p for p in repo_files() if p.relative_to(REPO).parts[0] in roots]


def sources_common(path):
    return "lib/common.sh" in path.read_text(errors="replace")


JSON_PARSERS = (re.compile(r"""(?<![\w-])jq(?=\s+[-'"])|\|\s*jq\b"""), re.compile(r"python3\s+-c\s+.*import\s+json"),
                re.compile(r"(?<![\w-])sed\b[^\n]*[{}][^\n]*\bjson\b", re.I))
RSYNC_REMOTE = re.compile(r'rsync\s[^\n]*(-e\s+"ssh|\$\w+:|@\$)')
VAR = r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
VARIABLE_PATTERN = re.compile(r"^\s*(?:;;\s*)?%s\)|\bcase\b.*\bin\s+%s\)|\S\|%s\)|\[\[[^]]*(?:==|!=)\s*%s" % (VAR, VAR, VAR, VAR))
LINE_RULES = {
    "one_lock_mechanism": (under("wk", "cmd", "lib", "boot", "image"),
                           lambda p, l: re.search(r"^[^#]*\bflock\b", l) and p.name != "selftest"),
    "exit_trap_through_common": (under("cmd", "lib", "boot", "image"),
                                 lambda p, l: re.search(r"^[^#]*trap .*EXIT", l) and p.name != "common.sh" and sources_common(p)),
    "no_json_in_bash": (shell_files, lambda p, l: any(r.search(l) for r in JSON_PARSERS)),
    "no_auth_key_in_argv": (under("cmd", "bench", "boot", "image", "lib"),
                            lambda p, l: re.search(r"--auth-?key", l) and code(l) and "file:" not in l),
    "rsync_sets_chmod": (shell_files, lambda p, l: code(l) and RSYNC_REMOTE.search(l) and "--chmod=" not in l),
    "no_variable_case_pattern": (shell_files, lambda p, l: VARIABLE_PATTERN.search(l)),
}
def _line_rule(files, broken):
    return lambda: lines_hits(files(), broken)


for _name, (_files, _broken) in LINE_RULES.items():
    RULES[_name] = (_line_rule(_files, _broken), None)


@rule()
def shell_parses():
    """Under bash 5 and the bash 3.2 macOS ships."""
    interpreters = (["/bin/bash"] if Path("/bin/bash").exists() else []) + ["bash"]
    return ["%s: %s" % (i, f.relative_to(REPO)) for f in shell_files() for i in interpreters
            if subprocess.run([i, "-n", str(f)], capture_output=True).returncode]


@rule()
def no_duplicate_shell_functions():
    out = []
    for f in shell_files():
        seen = collections.Counter(re.findall(r"(?m)^([a-zA-Z_][a-zA-Z0-9_]*)\(\)", f.read_text(errors="replace")))
        out += ["%s: %s defined twice" % (f.relative_to(REPO), n) for n, c in seen.items() if c > 1]
    return out


@rule()
def bench_confs_stand_alone():
    env = {"XDG_CONFIG_HOME": "/nonexistent-wk-test-config"}
    names = fleet.Fleet(REPO, env).names(fleet.BENCH_KINDS)
    bad = [] if names else ["no bench machines at all"]
    for n in names:
        conf = boot_cli.load_conf(REPO, n, env)
        if conf is None:
            bad.append("%s: no driver or note" % n)
            continue
        if conf["driver"] not in boot.drivers():
            bad.append("%s: driver %s missing" % (n, conf["driver"]))
        if conf["role"] not in ("workstation", "bench-device") or conf["os"] not in ("any", "macos", "linux"):
            bad.append("%s: bad role or os" % n)
        if not conf.get("image_preset"):
            bad.append("%s: no image_preset" % n)
        elif conf["os"] != "macos" and images.quiet_load(conf["image_preset"]) is None:
            bad.append("%s: image_preset '%s' does not resolve" % (n, conf["image_preset"]))
    listed = sorted(line.split()[0] for line in boot_cli.listing(REPO, env).splitlines())
    return bad + ([] if listed == sorted(names) else ["wk boot's listing %s and machines/ %s disagree" % (listed, sorted(names))])


@rule()
def gc_searches_every_build_location():
    declared = set(gc.build_outputs(Store({"WK_STORE": "/s"}), {"WK_STORE": "/s"}))
    return sorted(b for b in {images.load(n)["IMG_BUILDER"] for n in images.names()} if b and b not in declared)


@rule()
def buildroot_external_exists():
    d = REPO / "image" / "buildroot" / "external"
    if not any(re.search(r"(?m)^BR_EXTERNAL=1", f.read_text(errors="replace")) for f in (REPO / "image" / "presets").glob("*.conf")):
        return []
    out = ["%s does not exist and a preset sets BR_EXTERNAL=1" % (d / f) for f in ("external.desc", "external.mk", "Config.in") if not (d / f).exists()]
    return out or ([] if re.search(r"(?m)^name: ", (d / "external.desc").read_text()) else ["external.desc names nothing"])


@rule()
def card_helper_gate():
    """Every verb of admin/wk-card-priv calls its gate; the gate takes usb/mmc disks the machine is not running from."""
    text = text_of("admin/wk-card-priv")
    bad = []
    for v in ("v_check", "v_write", "v_fleet", "v_joins", "v_tailnet", "v_identity", "v_grow", "v_eject"):
        m = re.search(rf"(?ms)^{v}\(\) \{{.*?^\}}", text)
        if not m or "gate " not in m.group(0):
            bad.append("%s does not call gate" % v)
    gate = (re.search(r"(?ms)^gate\(\).*?^\}", text) or re.match("", "")).group(0)
    bad += [why for need, why in (("usb|mmc", "gate no longer restricts the transport to usb and mmc"),
                                  ("booted_disks", "gate no longer checks whether the machine runs from the disk")) if need not in gate]
    case = re.search(r'(?ms)^case "\$verb" in.*?^esac', text)
    if not case or "*) fail" not in case.group(0):
        bad.append("the verb dispatcher has a default that is not a refusal")
    if 'install -o root -m 0755 "$src" "$tgt"' not in text_of("admin/install.sh"):
        bad.append("admin/install.sh does not install the card helper root-owned")
    return bad


@rule()
def ssh_jump_hosts_bounded():
    """Each ProxyJump hop accepts new keys, times out, and never asks for a password inside a probe."""
    ssh = "/usr/bin/ssh"
    if not Path(ssh).exists():
        raise unittest.SkipTest("no %s on this machine" % ssh)
    conf = REPO / "dotfiles" / "ssh" / "config"
    jumps, interactive, hosts = set(), set(), []
    for line in conf.read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        key = parts[0].lower()
        if key == "host":
            hosts = parts[1:]
        elif key == "batchmode" and parts[1:2] == ["no"]:
            interactive.update(hosts)
        elif key == "proxyjump":
            jumps |= {re.sub(r":\d*$", "", re.sub(r".*@", "", j)) for tok in parts[1:] for j in tok.split(",") if j}
    bad = []
    for j in sorted(jumps):
        out = subprocess.run([ssh, "-G", "-F", str(conf), j], capture_output=True, text=True).stdout
        opts = dict((line.split() + [""])[:2] for line in out.splitlines() if line.strip())
        mode, timeout, batch = (opts.get(k, "") for k in ("stricthostkeychecking", "connecttimeout", "batchmode"))
        if mode not in ("accept-new", "no", "false", "off"):
            bad.append("%s: StrictHostKeyChecking is '%s'" % (j, mode or "unset"))
        if timeout in ("", "none", "0"):
            bad.append("%s: no ConnectTimeout" % j)
        if batch != "yes" and j not in interactive:
            bad.append("%s: BatchMode is '%s'" % (j, batch or "unset"))
    return bad


# Shell shapes `bash -n` cannot see.
GREP_ASSIGN = re.compile(r'^(local\s+|export\s+|declare\s+)?[A-Za-z_][A-Za-z0-9_]*=\$\(')
ABSENCE_CMD = re.compile(r'(?:\$\(|\||;|^)\s*(grep|ls)\s')
HEREDOC_OP = re.compile(r'<<(?!<)(-)?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\2')
FUNC = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{\s*(#.*)?$')


def _without_strings(s):
    out, quote, i = [], None, 0
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            out.append(" ")
        elif c in "'\"":
            quote = c
            out.append(" ")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _open_quote(text, quote=None):
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        i += 1
    return quote


def _statements(body):
    out, buf, heredoc = [], "", None
    for raw in body:
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        s = raw.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        buf = f"{buf} {s}" if buf else s
        if buf.endswith("\\"):
            buf = buf[:-1]
            continue
        if _open_quote(buf) is not None:
            continue
        out.append(buf)
        m = HEREDOC_OP.search(buf)
        if m:
            heredoc = m.group(3)
        buf = ""
    return out + ([buf] if buf else [])


@rule()
def grep_assignments_absorbed():
    """Under `set -e`, `x=$(grep ...)` or `x=$(ls ...)` dies when nothing matches; write `|| x=""`."""
    found = []
    for path in shell_files():
        text = path.read_text(errors="replace")
        if "set -e" in text:
            for stmt in _statements(text.splitlines()):
                bare = _without_strings(stmt)
                if GREP_ASSIGN.match(stmt) and "||" not in bare and "&&" not in bare and ABSENCE_CMD.search(bare):
                    found.append("%s: %s" % (path.relative_to(REPO), stmt))
    return found


def _functions(path):
    lines, out, i = path.read_text(errors="replace").splitlines(), [], 0
    while i < len(lines):
        m = FUNC.match(lines[i])
        if not m:
            i += 1
            continue
        depth, j, body = 1, i + 1, []
        while j < len(lines) and depth > 0:
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0:
                break
            body.append(lines[j])
            j += 1
        out.append((m.group(1), body))
        i = j + 1
    return out


def _without_data(s):
    out, depth, i = list(s), 0, 0
    while i < len(s):
        if s.startswith("$(", i):
            out[i] = out[i + 1] = " "
            depth += 1
            i += 2
            continue
        if depth:
            depth += {")": -1, "(": 1}.get(s[i], 0)
            out[i] = " "
        i += 1
    quote = None
    for i, c in enumerate(out):
        if quote:
            out[i] = " "
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            out[i] = " "
    return "".join(out)


BODY_TOKEN = re.compile(r'(?<![\w$])\{(?=\s)|(?<=[\s;])\}|\bdo\b|\bdone\b|\bthen\b|\bfi\b')
TAIL_CLOSER = re.compile(r'(\}|\bdone\b|\bfi\b)\s*;?\s*$')
BODY_SPLIT = re.compile(BODY_TOKEN.pattern + r'|;')
OPENS, CLOSES = ("{", "do", "then"), ("}", "done", "fi")


def _last_statement(body):
    stmts, depth = _statements(body), 0
    for i in range(len(stmts) - 1, -1, -1):
        tokens = [m.group(0) for m in BODY_TOKEN.finditer(_without_data(stmts[i]))]
        depth += sum(t in CLOSES for t in tokens) - sum(t in OPENS for t in tokens)
        if depth <= 0:
            return "; ".join(stmts[i:])
    return stmts[-1] if stmts else ""


def _last_in_body(body, masked):
    depth, cut = 0, 0
    for m in BODY_SPLIT.finditer(masked):
        tok = m.group(0)
        if tok in OPENS:
            depth += 1
        elif tok in CLOSES:
            depth -= 1
        elif depth == 0 and masked[m.end():].strip():
            cut = m.end()
    return body[cut:].strip().rstrip(";").strip()


def _without_bodies(masked):
    out, stack = list(masked), []
    for m in BODY_TOKEN.finditer(masked):
        if m.group(0) in OPENS:
            stack.append(m.end())
        elif stack:
            for i in range(stack.pop(), m.start()):
                out[i] = " "
    return "".join(out)


def _decisive(stmt):
    while True:
        masked = _without_data(stmt)
        if not TAIL_CLOSER.search(masked):
            return stmt
        span, stack = None, []
        for m in BODY_TOKEN.finditer(masked):
            if m.group(0) in OPENS:
                stack.append(m.end())
            elif stack:
                span = (stack.pop(), m.start())
        if span is None:
            return stmt
        stmt = _last_in_body(stmt[span[0]:span[1]], masked[span[0]:span[1]])


def deciding_and_chain(body):
    last = _last_statement(body)
    if not last or last.endswith("\\"):
        return ""
    stmt = _decisive(last)
    bare = _without_bodies(_without_data(stmt))
    return "" if "&&" not in bare or "||" in bare or stmt.startswith(("return", "exit")) else stmt


DELIBERATE_PREDICATES = {("admin/wk-card-priv", "_slot_present"), ("build/mem-watchdog.sh", "_alive"), ("container/agents.sh", "node_ok")}


@rule()
def trailing_and_chains_are_deliberate():
    """A function whose status is a trailing `a && b` returns 1 when `a` is false; it ends in `return 0` or is named here."""
    found = {(str(f.relative_to(REPO)), name) for f in shell_files() for name, body in _functions(f) if deciding_and_chain(body)}
    return ["new: %s %s" % x for x in sorted(found - DELIBERATE_PREDICATES)] + ["gone: %s %s" % x for x in sorted(DELIBERATE_PREDICATES - found)]


SCRIPT_ROOTS = tuple(r + "/" for r in ("cmd", "lib", "vm", "boot", "build", "host", "bench", "container/bin", "container/proxy", "admin"))
EUO_PIPEFAIL = re.compile(r'(?m)^\s*set\s+-euo\s+pipefail\s*$')
WITHOUT_EUO = {
    "bench/mac-quiet-hosts.sh": "sourced, not run",
    "build/mem-watchdog.sh": "a watchdog loop: under -e one transient ps/awk reading would kill the safety net",
}


@rule()
def scripts_set_euo_pipefail():
    out = []
    for p in shell_files():
        rel = p.relative_to(REPO).as_posix()
        with p.open(errors="replace") as f:
            script = re.match(r'^#!.*\b(bash|sh|dash|ksh)\b', f.readline())
        if script and rel.startswith(SCRIPT_ROOTS) and rel not in WITHOUT_EUO and not EUO_PIPEFAIL.search(p.read_text(errors="replace")):
            out.append("%s lacks `set -euo pipefail`" % rel)
    for rel in WITHOUT_EUO:
        if not (REPO / rel).is_file() or EUO_PIPEFAIL.search(text_of(rel)):
            out.append("%s is excepted but is gone or sets it: drop it from WITHOUT_EUO" % rel)
    return out


# Comment density: a source file's body is at most 5% prose.
MAX_BODY_PROSE = 0.05
DIRECTIVE = re.compile(r"^#\s*(wk:|shellcheck\b|noqa\b|type:\s|pylint|pragma|-\*-|!)")
HEREDOC = re.compile(r"""<<-?\s*(["']?)([A-Za-z_][A-Za-z0-9_]*)\1""")
GRANT_STATEMENTS = {"admin/wk-card-priv", "admin/wk-quiesce-priv", "admin/wk-boot-priv", "admin/install.sh"}


def shell_counts(lines):
    nonblank = prose = 0
    terminator, pending = None, []
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = pending.pop(0) if pending else None
                nonblank += 1
            continue
        stripped = line.strip()
        if not stripped:
            continue
        nonblank += 1
        if stripped.startswith("#"):
            prose += not DIRECTIVE.match(stripped)
        else:
            opened = [m.group(2) for m in HEREDOC.finditer(line)]
            if opened:
                terminator, pending = opened[0], opened[1:]
    return nonblank, prose


def python_counts(src):
    """(non-blank lines, prose lines): comments other than directives, and every docstring's lines."""
    nonblank, prose = len([l for l in src.splitlines() if l.strip()]), 0
    try:
        prose += sum(1 for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                     if tok.type == tokenize.COMMENT and not DIRECTIVE.match(tok.string.strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return nonblank, prose
    for node in ast.walk(tree):
        body = getattr(node, "body", None) if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            prose += body[0].value.end_lineno - body[0].value.lineno + 1
    return nonblank, prose


def body_counts(rel, path):
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    end = 1 if lines and lines[0].startswith("#!") else 0
    if 'usage_block "$0"' in src:
        while end < len(lines) and (lines[end].lstrip().startswith("#") or not lines[end].strip()):
            end += 1
    if rel.startswith("cmd/"):   # a command's module docstring is its -h, held to the help-text rule instead
        end = ast.parse(src).body[0].end_lineno
    if rel.endswith(".py") or rel.startswith("cmd/") or (lines and lines[0].startswith("#!") and "python3" in lines[0]):
        return python_counts("\n".join(lines[end:]))
    return shell_counts(lines[end:])


@rule()
def comment_density():
    out = ["%s is exempt but does not exist" % rel for rel in sorted(GRANT_STATEMENTS) if not (REPO / rel).is_file()]
    for rel in tracked():
        if rel.startswith(("tests/", "docs/", "claude/skills/synced/")) or rel in GRANT_STATEMENTS:
            continue
        path = REPO / rel
        if not (rel.endswith((".sh", ".py", ".yaml", ".yml", ".conf")) or rel.startswith("cmd/")):
            with path.open("rb") as f:
                if os.path.splitext(rel)[1] or f.read(2) != b"#!":
                    continue
        body, prose = body_counts(rel, path)
        if body and prose > max(1, int(MAX_BODY_PROSE * body)):
            out.append("%s: %d/%d = %.1f%% prose" % (rel, prose, body, 100 * prose / body))
    return out


# What a host installs and sets: host/linux/apt.txt names the command that needs each package, and every
# host/macos/defaults.conf entry is `domain key type value reason`.
@rule()
def host_packages_name_their_consumer():
    text = text_of("host/linux/apt.txt")
    blocks = [[l for l in chunk.splitlines() if l.strip()] for chunk in re.split(r"\n\s*\n", text)]
    out = []
    for b in blocks:
        comment, pkgs = [l for l in b if l.lstrip().startswith("#")], [l.strip() for l in b if not l.lstrip().startswith("#")]
        if not pkgs:
            continue
        out += ["not a bare package name: %r" % p for p in pkgs if " " in p]
        said = "\n".join(comment)
        cmds = re.findall(r"\bwk ([A-Za-z][A-Za-z0-9_-]*)", said)
        out += ["%s names 'wk %s', which does not exist" % (pkgs, c) for c in cmds if not (REPO / "cmd" / c).is_file()]
        paths = [m.rstrip(").,:;") for m in re.findall(r"\b[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\b", said)]
        if not ("./setup" in said or cmds or any((REPO / p).exists() for p in paths)):
            out.append("%s: no 'wk <cmd>', './setup' or existing repo path" % ", ".join(pkgs))
    for line in text_of("host/macos/defaults.conf").splitlines():
        fields = line.split(None, 4)
        if code(line) and line.strip() and (len(fields) < 5 or fields[2] not in ("string", "bool", "int", "float")):
            out.append("host/macos/defaults.conf: %s" % line)
    return out


REPORTERS = ("bench/mac-raiser.sh", "bench/mac-quiet-desktop.sh", "bench/mac-window-probe.sh", "build/guard.sh")
WORDS = ("info", "warn", "log", "die", "debug", "changed", "unchanged")


@rule()
def reporting_words_are_this_repos():
    """A library a place sources ends up with this repo's report functions (not /usr/bin/info), standalone; one that
    defines none gets none."""
    out = []
    checks = "; ".join('echo "%s=$(type -t %s || echo none)"' % (w, w) for w in WORDS)
    for rel in REPORTERS:
        cp = bash('set -euo pipefail\n. "$WK_ROOT/%s"\n%s\n' % (rel, checks))
        if cp.returncode:
            out.append("%s does not source standalone: %s" % (rel, cp.stderr.strip()[:200]))
            continue
        kinds = dict(line.partition("=")[::2] for line in cp.stdout.split())
        if re.search(r"^\s*(%s) " % "|".join(WORDS), text_of(rel), re.M):
            out += ["%s: %s is a %s" % (rel, w, k) for w, k in kinds.items() if k not in ("function", "none")]
    if "function" in bash('set -u; . "$WK_ROOT/bench/mac-window-probe.sh"; type -t info || :').stdout:
        out.append("bench/mac-window-probe.sh defines no reporting and still gets this repo's info")
    raiser = bash('. "$WK_ROOT/bench/mac-raiser.sh"; type -t info warn').stdout.split()
    return out + ([] if raiser == ["function", "function"] else ["the raiser cannot say what it did: %s" % raiser])


# The two conf registries, image/presets and machines/, against the loaders that read them.
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def conf_files(registry, kinds=fleet.KINDS):
    if registry == "machines":
        return [REAL_MACHINES / (n + ".conf") for n in fleet.Fleet(REPO, FLEET_ENV).names(kinds)]
    return sorted((REPO / "image" / "presets").glob("*.conf"))


def conf_lines(path):
    """(line number, line, key): key is "" in a quoted continuation, None where no KEY= starts."""
    in_quote = False
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if in_quote:
            in_quote ^= (line.count('"') % 2 == 1)
            yield i, line, ""
            continue
        m = ASSIGN.match(line)
        in_quote = bool(m) and line.count('"') % 2 == 1
        yield i, line, m.group(1) if m else None


def assigned(path):
    return {k: line.split("=", 1)[1].strip().strip('"') for _, line, k in conf_lines(path) if k}


@rule()
def confs_are_key_value_under_their_name():
    out = []
    for path in conf_files("image/presets") + conf_files("machines"):
        if not path.read_text().startswith("# " + path.stem):
            out.append("%s: the header does not name it" % path.name)
        out += ["%s:%d: %r" % (path.name, i, line) for i, line, key in conf_lines(path)
                if key is None and line.strip() and not line.strip().startswith("#")]
    return out + ["%s has no blurb" % n for n in images.names({"WK_ROOT": str(REPO)}) if not images.blurb(n, {"WK_ROOT": str(REPO)})]


def field_set_breaks(files, known, optional=frozenset()):
    if not files:
        return ["no confs"]
    sets = {p.name: set(assigned(p)) - {"kind"} for p in files}
    first = next(iter(sets.values()))
    return (["%s sets unknown field(s): %s" % (n, sorted(f - known)) for n, f in sets.items() if not f <= known]
            + ["%s's fields differ from its kind's" % n for n, f in sets.items() if f - optional != first - optional])


@rule()
def confs_of_a_kind_declare_one_field_set():
    """And nothing their loader does not read."""
    wk = REPO / "lib" / "wk"
    readers = [p for d in ("boot", "sysimage") for p in sorted((wk / d).glob("*.py"))] + [wk / "reach.py"]
    bench = {k for p in readers for k in re.findall(r"[\"']([a-z][a-z0-9_]*)[\"']", p.read_text())}
    out = field_set_breaks(conf_files("machines", fleet.BENCH_KINDS), bench)
    out += field_set_breaks(conf_files("machines", fleet.PLACE_KINDS), set(places.CONF_ENV))
    known = set(re.findall(r"\b(?:CFG_|IMG_|YOC_|BR_|FET_)[A-Z0-9_]+\b", text_of("lib/wk/images.py")))
    # Facts about one configuration, not fields its builder group is missing.
    optional = {"CFG_NEEDS", "BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256", "BR_KERNEL_RELEASE",
                "YOC_PORT_TARGET_FROM", "YOC_MACHINE", "YOC_MULTILIB", "YOC_MULTILIB_TUNE"}
    by_builder = {}
    for p in conf_files("image/presets"):
        by_builder.setdefault(assigned(p).get("IMG_BUILDER"), []).append(p)
    out += ["a preset names no IMG_BUILDER"] if None in by_builder else []
    for files in by_builder.values():
        out += field_set_breaks(files, known, optional)
    for path in conf_files("machines", fleet.BENCH_KINDS):
        f = assigned(path)
        if f.get("net") not in ("wifi", "ethernet") or (f.get("kind") == "board" and not f.get("dtb")):
            out.append("%s: a bench machine sets its net, and a board its dtb" % path.name)
    return out


ARM = re.compile(r"^\s*\(?((?:[\"']?[A-Za-z0-9_.-]+[\"']?\s*\|\s*)*[\"']?[A-Za-z0-9_.-]+[\"']?)\)")


@rule()
def no_code_names_a_machine():
    """A fact about one machine is a key in its conf: no case arm names one, and no default value does (`# static` escapes)."""
    names = {p.stem for p in REAL_MACHINES.glob("*.conf")}
    default = re.compile(r":-(%s)\b" % "|".join(map(re.escape, sorted(names))))
    hits = []
    for rel in tracked():
        if rel in ("CLAUDE.md", "docs/PLAN.md", SELF):
            continue
        text = users_text(rel)
        if rel.split("/")[0] in ("lib", "cmd", "image", "bench") and not rel.endswith(".conf"):
            hits += ["%s:%d: %s" % (rel, i, l.strip()) for i, l in enumerate(text.splitlines(), 1) if "# static" not in l and default.search(l)]
        if rel.endswith(".conf") or not (rel.endswith(".sh") or text.startswith(("#!/usr/bin/env bash", "#!/bin/sh"))):
            continue
        depth = 0
        for i, line in enumerate(text.splitlines(), 1):
            c = line.split(" #", 1)[0]
            depth += bool(re.search(r"\bcase\b.*\bin\b", c))
            m = ARM.match(c) if depth else None
            if m and {w.strip().strip("\"'") for w in m.group(1).split("|")} & names:
                hits.append("%s:%d: a case arm names a machine" % (rel, i))
            if re.search(r"\besac\b", c):
                depth = max(0, depth - 1)
    return hits


@rule()
def a_test_that_makes_a_workspace_removes_it():
    """addCleanup, a tearDown that removes, or a finally: a failing assertion leaves no workspace behind."""
    def verb(n):
        if not isinstance(n, ast.Call) or not n.args or (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")) not in ("run", "run_wk"):
            return None
        return n.args[0].value if isinstance(n.args[0], ast.Constant) else None

    def runs(node, v):
        return any(verb(n) == v for n in ast.walk(node))
    out = []
    for path in sorted((REPO / "tests").glob("test_*.py")):
        for cls in (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.ClassDef)):
            creates = any(verb(n) == "new" and not {a.value for a in n.args if isinstance(a, ast.Constant)} & {"--kill", "-h", "--help"}
                          for n in ast.walk(cls))
            cleans = any((isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "addCleanup")
                         or (isinstance(n, ast.FunctionDef) and n.name == "tearDown" and runs(n, "rm"))
                         or (isinstance(n, ast.Try) and any(runs(f, "rm") for f in n.finalbody)) for n in ast.walk(cls))
            if creates and not cleans:
                out.append("%s: %s" % (path.name, cls.name))
    return out


# Every entry point starts in a fresh interpreter with only lib/ on its path, importing nothing first.
LIB = str(REPO / "lib")
ENTRY_MODULE = re.compile(r'(?:-m |wk_py |wk_eval |"-m", "|isolated_module\([^\n]*?")((?:wk|credcheck)(?:\.[a-z_]+)*)\b')
RUN_MODULE = "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module(sys.argv.pop(1), run_name='__main__', alter_sys=True)"
LOAD_SCRIPT = "import runpy, sys; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[2], run_name='wk_entry_probe')"
LOAD_COMMANDS = "import sys; sys.path.insert(0, sys.argv[1]); from wk import decl; print(len(decl.commands(sys.argv[2])))"


def fresh(argv):
    return subprocess.run([sys.executable, "-I"] + argv, capture_output=True, text=True, timeout=60, env=clean_env(), cwd=str(REPO))


@rule()
def entry_points_start_clean():
    """Each `python3 -m` module the tree runs answers --help; each script importing lib loads (a guarded one, or a
    module lib imports by name) or answers --help; cmd/* all load as the dispatcher loads them."""
    shipped = [REPO / rel for rel in tracked() if rel.split("/")[0] not in (".git", ".claude", "tests", "docs")
               and Path(rel).suffix not in (".md", ".pyc", ".png", ".json")]
    out = []
    for m in sorted({m for p in shipped for m in ENTRY_MODULE.findall(p.read_text(errors="replace"))} - {"wk"}):
        cp = fresh(["-c", RUN_MODULE, LIB, m, "--help"])
        if "Traceback" in cp.stderr or "usage" not in (cp.stdout + cp.stderr).lower():
            out.append("python3 -m %s --help: %s" % (m, cp.stderr[-200:]))
    for p in shipped:
        rel = p.relative_to(REPO).as_posix()
        with open(p, "rb") as f:
            head = f.readline()
        text = p.read_text(errors="replace")
        if rel.startswith("lib/wk/") or not (p.suffix == ".py" or (head.startswith(b"#!") and b"python" in head)) \
                or not re.search(r"^ *(from|import) (wk|credcheck)\b", text, re.M):
            continue
        if re.search(r'^if __name__ == "__main__":', text, re.M) or p.parent == REPO / "lib":
            cp = fresh(["-c", LOAD_SCRIPT, LIB, str(p)])
            if cp.returncode or cp.stderr:
                out.append("%s does not load: %s" % (rel, cp.stderr[-200:]))
        else:
            cp = fresh([str(p), "--help"])
            if "Traceback" in cp.stderr or "usage" not in (cp.stdout + cp.stderr).lower():
                out.append("%s --help: %s" % (rel, cp.stderr[-200:]))
    cp = fresh(["-c", LOAD_COMMANDS, LIB, str(REPO)])
    want = str(len([p for p in (REPO / "cmd").iterdir() if p.is_file()]))
    return out + ([] if (cp.returncode, cp.stdout.strip()) == (0, want) else ["cmd/* do not all load: %s" % cp.stderr[-200:]])


@rule()
def one_prompt():
    """lib/common.sh's confirm() is the one prompt in bash: no second definition, no raw terminal read."""
    files = [p for d in ("cmd", "admin") for p in sorted((REPO / d).iterdir()) if p.is_file()]
    files += [p for d in ("lib", "boot", "bench", "image") for p in sorted((REPO / d).glob("*.sh"))]
    reads_a_heredoc = {("admin/wk-card-priv", "read -r type tran <<EOF")}
    out = []
    for p in files:
        rel = p.relative_to(REPO).as_posix()
        for line in p.read_text(errors="replace").splitlines():
            if line.startswith("confirm()") and rel != "lib/common.sh":
                out.append("%s defines a second confirm()" % rel)
            if re.search(r"read -r|read -p", line) and "while" not in line and rel != "lib/common.sh" \
                    and (rel, line.strip()) not in reads_a_heredoc:
                out.append("%s: a raw read outside confirm(): %s" % (rel, line.strip()))
    return out + ([] if re.search(r"(?m)^confirm\(\)", text_of("lib/common.sh")) else ["lib/common.sh defines no confirm()"])


@rule()
def rescue_layer_installs_the_repos_helper():
    """image/yocto/meta-wk-rescue installs admin/wk-card-priv and its checker from the tree (no second copy), and
    RDEPENDS on every command the helper runs."""
    recipe = REPO / "image/yocto/meta-wk-rescue/recipes-wk/wk-card-priv/wk-card-priv.bb"
    text = recipe.read_text()
    m = re.search(r'FILESEXTRAPATHS:prepend := "((?:\$\{THISDIR\}/[^:"]+:)+)"', text)
    if not m:
        return ["the recipe does not point FILESEXTRAPATHS at the repository's own directories"]
    dirs = [recipe.parent / d.replace("${THISDIR}/", "") for d in m.group(1).rstrip(":").split(":")]
    out = []
    for name, own in (("wk-card-priv", "admin/wk-card-priv"), ("check-boot-files.py", "boot/check-boot-files.py")):
        found = next(((d / name) for d in dirs if (d / name).is_file()), None)
        if found is None or not found.resolve().samefile(REPO / own):
            out.append("the recipe does not install %s" % own)
    if (recipe.parent / "files").exists():
        out.append("a second copy of the helper under files/")
    rdepends = re.search(r'RDEPENDS:\$\{PN\} \+= "([^"]+)"', text).group(1)
    helper = text_of("admin/wk-card-priv")
    for cmd, pkg in (("findmnt", "util-linux-findmnt"), ("lsblk", "util-linux-lsblk"), ("sfdisk", "util-linux-sfdisk"),
                     ("partx", "util-linux-partx"), ("blkid", "util-linux-blkid"), ("resize2fs", "e2fsprogs-resize2fs"),
                     ("e2fsck", "e2fsprogs-e2fsck"), ("mcopy", "mtools"), ("python3", "python3-core")):
        if re.search(r"\b%s\b" % cmd, helper) and pkg not in rdepends:
            out.append("the helper runs %s and the recipe does not RDEPEND on %s" % (cmd, pkg))
    return out


@rule()
def setup_stages_tested_or_owed():
    """Every stage ./setup runs is one tests/test_crash_only.py kills, or one it names as needing the machine."""
    from tests.test_crash_only import HOME_SCOPED, NEEDS_THE_MACHINE
    stages = re.findall(r"^run_stage\s+(\S+)", text_of("setup"), re.M)
    return [] if stages and sorted(stages) == sorted(set(HOME_SCOPED) | set(NEEDS_THE_MACHINE)) else ["./setup runs %s" % stages]


@rule()
def guest_probes_change_nothing():
    """vm/desktop.sh kills nothing; the desktop and load probes write no setting, kill nothing and source nothing of the
    tree, so they are safe on a live guest; the probe asks only about keys the settle writes."""
    desktop, probe, load = text_of("vm/desktop.sh"), text_of("vm/desktop-probe.sh"), text_of("vm/load-probe.sh")
    out = ["vm/desktop.sh: %s" % b for b in ("pkill", "killall", "kill -") if b in desktop]
    out += ["vm/desktop-probe.sh: %s" % b for b in ("defaults write", "defaults -currentHost write", "pkill", "killall",
                                                    "softwareupdate --schedule", "sysadminctl -screenLock off", "pmset -a") if b in probe]
    out += ["vm/load-probe.sh: %s" % b for b in ("pkill", "killall", "kill -", "defaults write", "sudo") if b in load]
    out += ["%s sources %s" % (rel, b) for rel, t in (("vm/desktop-probe.sh", probe), ("vm/load-probe.sh", load))
            for b in ("lib/common.sh", "$WK_ROOT", "$WK_TOOLS_DIR") if b in t]
    written = set(re.findall(r"DidSee[A-Za-z0-9]+", desktop))
    return out + ["vm/desktop-probe.sh asks about %s, which the settle never writes" % k
                  for k in sorted(set(re.findall(r"DidSee[A-Za-z0-9]+", probe)) - written)]


@rule()
def guest_probes_answer_every_key_read():
    """Every key doctor's desktop rows read is one a probe or the quiet table prints."""
    printed = "".join(text_of(r) for r in ("vm/desktop-probe.sh", "bench/mac-window-probe.sh", "bench/mac-quiet-desktop.sh"))
    keys = set(re.findall(r"printf '([a-z_]+)=", printed)) | set(re.findall(r"^[a-z]+\t([a-z_]+)\t", text_of("bench/quiet/macos.tsv"), re.M))
    read = set(re.findall(r'v\("([a-z_]+)"', inspect.getsource(doctor.desktop_rows))) | {row[0] for row in doctor.DESKTOP}
    read -= {"pin", "uninvited", "quiet"}   # desktop_readings' own, not the probe's
    return (["no key read"] if not read else []) + sorted(read - keys)


@rule()
def wall_deny_rules():
    """Both claude settings files deny exactly the build wall's two spellings per wrapped name: the bare name (which
    shell/path.sh puts the wall first on PATH for) and the path spelling (`Bash(*/<name> *)`), and no bare-word rule."""
    names = re.search(r'^WALL_NAMES="([^"]+)"', text_of("container/bin/wk-build-wall"), re.M).group(1).split()
    want = sorted(["Bash(%s *)" % n for n in names] + ["Bash(*/%s *)" % n for n in names])
    return ["%s denies %s" % (f, d) for f in ("claude/settings.json", "claude/settings-host.json")
            for d in [sorted(json.loads(text_of(f))["permissions"]["deny"])] if d != want]


# Every command's options are declared once, in its table, and read by its code; the global flags are the dispatcher's.
GLOBAL_OPTS = set(decl.GLOBALS) | set(decl.HELP)


def code_opts(path):
    text = path.read_text()
    listed = re.findall(r'(?:^[A-Z_]+ = |in )\(([^)]*)\)', text, re.M)
    return set(re.findall(r'"(-{1,2}[a-z_][a-z_-]*)" in ', text) + re.findall(r'== "(-{1,2}[a-z_][a-z_-]*)"', text) +
               re.findall(r'\.(?:flag|value|values)\("(-{1,2}[a-z_][a-z_-]*)"\)', text) +
               [o for group in listed for o in re.findall(r'"(--?[a-z_][a-z_-]*)"', group)])


@rule()
def declared_options_are_read():
    out = []
    for name, m in decl.commands(REPO):
        path = REPO / "cmd" / name
        ours = {o.flag for _, t, opts in decl.variants("x", m.COMMAND) for o in tuple(opts) + tuple(t.opts)} - {"--preset"}
        code_read = code_opts(path)
        out += ["cmd/%s parses the global %s itself" % (name, o) for o in sorted(code_read & GLOBAL_OPTS)]
        code_read -= GLOBAL_OPTS
        package = [f.read_text() for f in (REPO / "lib" / "wk" / name).glob("*.py")]
        generic = "cli.options(args" in path.read_text() or "profile.main" in path.read_text()
        read = code_read | {o for text in package for o in re.findall(r'"(--[a-z][a-z-]*)"', text)}
        out += ["cmd/%s declares %s and reads it nowhere" % (name, o) for o in sorted(set() if generic else ours - read)]
        out += ["cmd/%s reads %s and does not declare it" % (name, o) for o in sorted(code_read - ours - {"--preset"})]
    return out


REMOVING = re.compile(r"delete_vm|\"podman\", *\"(rm|rmi)\"|\"image\", \"prune\"|\"volume\", \"prune\"|unshare\", \"rm\"|tart, \"delete\"")
REMOVERS = {"lib/wk/gc.py": ("gc",), "lib/wk/sysimage/guestbase.py": ("sysimage",), "lib/wk/places.py": ("rm", "gc", "stop")}


@rule()
def removals_ask():
    """A removing effect is in a module only destructive commands (which ask) reach."""
    found = {str(p.relative_to(REPO)) for p in (REPO / "lib" / "wk").rglob("*.py") if REMOVING.search(p.read_text())}
    asking = {n for n, m in decl.commands(REPO) if any(t.destructive for _, t, _ in decl.variants(n, m.COMMAND))}
    return (["%s removes and is not named in REMOVERS" % f for f in sorted(found - set(REMOVERS))]
            + ["%s is named in REMOVERS and removes nothing" % f for f in sorted(set(REMOVERS) - found)]
            + ["wk %s removes and does not ask" % c for c in sorted({c for cs in REMOVERS.values() for c in cs} - asking)])


# lint.one_wifi_reader: a WiFi credential is read in one place, the card helper (`_host_wifi`), as root on the machine
# holding the reader; every other seed takes it from there.
WIFI_READER = re.compile(r"etc/netplan/\*\.yaml|find-generic-password|wireless-security\.psk|--show-secrets")


def wifi_readers():
    roots = ("admin", "bench", "boot", "build", "cmd", "container", "host", "image", "lib", "remote", "places", "vm")
    return [rel for rel in tracked() if rel.split("/")[0] in roots and WIFI_READER.search(text_of(rel))]


@rule()
def wifi_read_by_the_card_helper():
    return [] if "admin/wk-card-priv" in wifi_readers() else ["admin/wk-card-priv no longer reads the WiFi credential"]


@rule(owed_because="lib/wk/sysimage/macvolume.py reads the System keychain for a bench volume's: a second reader")
def one_wifi_reader():
    return [r for r in wifi_readers() if r != "admin/wk-card-priv"]


# lint.one_copy_path: bytes cross between machines through lib/wk/machine.py; each other copy is named with why.
COPY = re.compile(r"\[\s*[\"'](scp|rsync)[\"']|[\"'](scp|rsync) -|podman\(\)\s*\+\s*\[[\"']cp[\"']|\bshutil\.copy\w*\(")
COPIES_ELSEWHERE = {
    "lib/wk/places.py": "the container driver's half of the one copy: `podman cp` is how bytes cross into a container",
    "lib/wk/bench/mac.py": "pins a payload into the staging tree: both ends are this host's own filesystem",
    "lib/wk/sysimage/macvolume.py": "stages wk-tools onto the bench volume, mounted on the machine that runs the copy",
}


@rule()
def one_copy_path():
    found = {rel for rel in tracked() if (rel.startswith("lib/wk/") and rel.endswith(".py") or rel.startswith("cmd/"))
             and rel != "lib/wk/machine.py"
             and any(COPY.search(l) and not l.lstrip().startswith("#") for l in text_of(rel).splitlines())}
    return ["copies outside lib/wk/machine.py: " + r for r in sorted(found - set(COPIES_ELSEWHERE))] + \
        ["listed, and no longer copying: " + r for r in sorted(set(COPIES_ELSEWHERE) - found)]


@rule()
def commands_ask_drivers_only_the_contract():
    """A command asks a driver nothing outside places.Driver's contract."""
    asked = set()
    for rel in (r for r in tracked() if r.startswith("cmd/")):
        asked |= {a or b for a, b in re.findall(r"\bdriver\.([a-z_]+)|reg\.present\([^)]*\)\.([a-z_]+)", text_of(rel))}
    return sorted(asked - set(places.Driver.CONTRACT) - set(places.Driver.FIELDS))


@rule()
def ssh_config_forwards_no_agent():
    """dotfiles/ssh/config has no `Host *` block, keeps the gateway's, and no block forwards an agent."""
    config = text_of("dotfiles/ssh/config")
    blocks = dict((m.group(1).strip(), m.group(2)) for m in re.finditer(r"^Host (.+)\n((?:[ \t].*\n|\n)*)", config, re.M))
    return (["a Host * block"] if "*" in blocks else []) + (["no igalia.com block"] if "igalia.com" not in blocks else []) + \
        ["ForwardAgent in Host %s" % h for h, b in blocks.items() if "forwardagent" in b.lower()]


class Lint(unittest.TestCase):
    pass


def _test(fn, owed_because):
    def test(self):
        self.assertEqual(fn(), [], fn.__doc__ or "")
    test.__doc__ = fn.__doc__
    return owed(owed_because)(test) if owed_because else test


for _name, (_fn, _owed) in RULES.items():
    setattr(Lint, "test_" + _name, _test(_fn, _owed))


if __name__ == "__main__":
    unittest.main()
