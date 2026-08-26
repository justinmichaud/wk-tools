#!/usr/bin/env python3
"""Structured-data operations for `wk bench`: reading and writing the JSON
records a run produces, merging jsc-shell iteration logs into one result, and
warning when two runs are not comparable. Stdlib only -- it has to run on
whatever python3 is on macOS and on a bare-metal Linux board, with no pip
step. Every operation takes its input as argv or stdin, never spliced into
source text, so a value from the shell cannot break out of a string literal.
"""

import argparse
import json
import re
import sys


def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _get_nested(doc, dotted_key):
    node = doc
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_nested(doc, dotted_key, value):
    parts = dotted_key.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


# --- get ---------------------------------------------------------------------
# One field out of a JSON file, by dotted path. Never fails: a missing file, a
# parse error or a missing key all print the default, because every call site
# that reads a bench record already treats "unknown" and "absent" the same.
def cmd_get(args):
    doc = _load(args.file)
    value = _get_nested(doc, args.key)
    print(value if value is not None else args.default)


# --- ls-summary ----------------------------------------------------------------
# One line of `wk bench ls`: the axes a saved run was taken on. Left to raise
# on a malformed env.json -- a run whose own record does not parse is not one
# `wk bench ls` can summarize, and printing a wrong answer would be worse.
def cmd_ls_summary(args):
    m = json.load(open(args.file))
    axes = m.get("runner", "browser")
    if m.get("arch", "native") != "native":
        axes += "/" + m["arch"]
    if m.get("bench_host", "container") != "container":
        axes += "/" + m["bench_host"]
    print(
        "%-14s %-12s %-12s %s%s"
        % (
            m.get("plan", "?"),
            m.get("config", "?"),
            axes,
            (m.get("webkit_sha") or "?")[:10],
            "  [FORCED]" if m.get("forced") else "",
        )
    )


# --- plan-spec -----------------------------------------------------------------
# Where a plan's payload comes from, read from the plan JSON on stdin.
# Prints "<kind> <url> <ref> <subdir>"; a plan with no fetchable source, or an
# unparseable github_source, exits nonzero with the reason on stderr.
def cmd_plan_spec(_args):
    p = json.load(sys.stdin)
    if "git_repository" in p:
        g = p["git_repository"]
        print("git", g["url"], g.get("branch", "main"), ".")
    elif "github_source" in p:
        m = re.match(
            r"https://github.com/([^/]+/[^/]+)/tree/([0-9a-f]+)/?(.*)",
            p["github_source"],
        )
        if not m:
            sys.exit("unparseable github_source: " + p["github_source"])
        print("git", "https://github.com/%s.git" % m.group(1), m.group(2), m.group(3) or ".")
    elif "local_copy" in p:
        print("local", p["local_copy"], "", "")
    else:
        sys.exit("plan has no fetchable source; run-benchmark will handle it")


# --- merge-jsc-logs --------------------------------------------------------------
# One result.json out of N per-iteration run logs from the jsc-shell runner.
# Each log holds the driver's resultsJSON() somewhere in its output; this
# takes the last line in each that parses as a JSON object (jsc's own exit
# noise follows it) and appends each iteration's "current" scores onto one
# merged tree, which is what compare-results needs to have variance to work
# with -- the same shape run-benchmark's own --count produces.
def _extract(path):
    with open(path, errors="replace") as f:
        lines = f.read().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _merge(into, other):
    for key, value in other.items():
        if key not in into:
            into[key] = value
        elif isinstance(value, dict) and isinstance(into[key], dict):
            _merge(into[key], value)
        elif key == "current" and isinstance(value, list) and isinstance(into[key], list):
            into[key].extend(value)
    return into


def cmd_merge_jsc_logs(args):
    merged = None
    missing = []
    for path in args.logs:
        one = _extract(path)
        if one is None:
            missing.append(path)
            continue
        merged = one if merged is None else _merge(merged, one)

    if merged is None:
        sys.exit(
            "no results in any iteration log -- the suite printed no JSON. "
            "Check the run-*.log files: a payload whose cli.js does not accept "
            "--dump-json-results will have run the whole suite and reported "
            "only in its own text format."
        )
    if missing:
        print("warning: no results in %s" % ", ".join(missing), file=sys.stderr)

    with open(args.out, "w") as f:
        json.dump(merged, f, indent=2)


# --- env-record ----------------------------------------------------------------
# The one writer for a bench run's env.json, for every runner and every host:
# a container run, a staged macOS run, and (by hand-off) `wk pi bench`. Fields
# arrive as `key=value` (a dotted key nests, e.g. `host.kernel=...`), never
# spliced into source, so a value with a quote or a backslash in it cannot
# reach the JSON any way but as that literal string. `--bool key=value` is for
# the handful of fields that are booleans in the record (forced, software,
# role_marker_overridden, ...): empty string is false, anything else is true --
# the same rule `bool()` applies to a shell flag that is either unset or "1".
def cmd_env_record(args):
    doc = {}
    for field in args.fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("env-record: not a key=value: %s" % field)
        _set_nested(doc, key, value)
    for field in args.bool_fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("env-record: not a key=value: %s" % field)
        _set_nested(doc, key, bool(value))
    with open(args.out, "w") as f:
        json.dump(doc, f, indent=2)


# --- axis-check ----------------------------------------------------------------
# Whether two saved runs are comparable, printed as warnings (never fatal --
# the caller decides what to do with a mismatch). Three axes decide what a run
# needs and what it may be compared with, and are recorded with every result:
#
#   class     what the benchmark measures (cpu-class or gpu-class).
#   runner    what executed it (the jsc shell or a real browser).
#   host      where it ran (a container, or a booted bench image).
#
# A mismatch on any of them means the two runs are not the same measurement,
# not that one is slower -- so this is the first thing `wk bench compare`
# checks, before any statistic is computed from the numbers themselves.
def cmd_axis_check(args):
    a, b = _load(args.a), _load(args.b)
    if not a or not b:
        return

    if a.get("plan") != b.get("plan"):
        print("warning: different plans (%s vs %s)" % (a.get("plan"), b.get("plan")))
    if a.get("config") != b.get("config"):
        print("warning: different build configs (%s vs %s)" % (a.get("config"), b.get("config")))

    # The three axes. A runner or host mismatch is not a caveat on a comparison
    # -- it means the two runs measured different machines doing different
    # work, and the statistics below will still happily produce a p-value for
    # them.
    if a.get("runner", "browser") != b.get("runner", "browser"):
        print(
            "warning: different runners (%s vs %s) -- the jsc shell and MiniBrowser "
            "are not the same measurement" % (a.get("runner", "browser"), b.get("runner", "browser"))
        )
    if a.get("bench_host", "container") != b.get("bench_host", "container"):
        print(
            "warning: different benchmark hosts (%s vs %s) -- a container shares a kernel "
            "and a desktop with everything else on the machine; an image does not"
            % (a.get("bench_host", "container"), b.get("bench_host", "container"))
        )
    if a.get("arch", "native") != b.get("arch", "native"):
        print("warning: different architectures (%s vs %s)" % (a.get("arch", "native"), b.get("arch", "native")))

    # No default for class: runs predating the field say nothing about what
    # they measured, and "absent" is not "different".
    if a.get("class") and b.get("class") and a["class"] != b["class"]:
        print("warning: different benchmark classes (%s vs %s)" % (a["class"], b["class"]))

    # The renderer and the session are only evidence for a gpu-class run.
    # Warning that a JetStream run in the jsc shell had no renderer is noise,
    # and noise in front of real warnings is how the real ones stop being
    # read.
    gpu_class = a.get("class") != "cpu" and b.get("class") != "cpu"
    if gpu_class and a.get("gpu_renderer") != b.get("gpu_renderer"):
        print("warning: different renderers (%s vs %s)" % (a.get("gpu_renderer"), b.get("gpu_renderer")))
    if gpu_class and a.get("session_mode") != b.get("session_mode"):
        print(
            "warning: different session modes (%s vs %s) -- only 'gpu' is a measurable display path"
            % (a.get("session_mode"), b.get("session_mode"))
        )
    if bool(a.get("software")) != bool(b.get("software")):
        print("warning: one run is software-rendered and the other is not -- these are not comparable")
    if a.get("forced") or b.get("forced"):
        print("warning: at least one run was taken with failing preflight checks (--force)")

    # A rehearsal: bench mode was asserted by an override rather than by the
    # machine having booted the image, so the number came off a workstation
    # however it is labelled (cmd_staged, WK_IMAGE_MARKER).
    if a.get("role_marker_overridden") or b.get("role_marker_overridden"):
        print(
            "warning: at least one run only *claimed* bench mode "
            "(WK_IMAGE_MARKER was overridden) -- it was measured on a workstation"
        )
    if a.get("local_copy") != b.get("local_copy"):
        print("warning: different benchmark payloads (%s vs %s)" % (a.get("local_copy"), b.get("local_copy")))

    # The machine, for on-board runs. Two boards are two different computers,
    # and an rpi3 score against an rpi4 score is not a comparison however
    # similar the axes look -- different SoC, different width, different
    # memory. This is a warning where the kernel below is not, because nobody
    # sets out to compare two boards to each other.
    if a.get("machine") and b.get("machine") and a["machine"] != b["machine"]:
        print(
            "warning: different machines (%s vs %s) -- these are two computers, not "
            "two states of one" % (a["machine"], b["machine"])
        )

    # The kernel and the system, *reported* rather than warned about. For most
    # comparisons these being equal is what you want; for a kernel A/B their
    # differing is the entire experiment, so a warning would fire on the
    # intended case and train the eye to skip it. Printing the delta serves
    # both readings.
    #
    # The width of the kernel, which `arch` above does not answer. Two runs
    # whose kernels differ in width are two measurements however identical
    # their builds.
    kaa = (a.get("host") or {}).get("kernel_arch")
    kab = (b.get("host") or {}).get("kernel_arch")
    if kaa and kab and kaa != kab:
        print(
            "warning: different kernel widths (%s vs %s) -- a 32-bit system and a "
            "32-bit process on a 64-bit kernel are not the same measurement" % (kaa, kab)
        )

    # Storage. Cheap flash contributes variance rather than a subtractable
    # bias, so a stick run and an SSD run are two series. Reported rather than
    # warned about when only the model differs on the same transport.
    ra = (a.get("host") or {}).get("root_device")
    rb = (b.get("host") or {}).get("root_device")
    if ra and rb and ra != rb:
        print(
            "warning: different root storage (%s vs %s) -- cheap flash contributes "
            "variance, not a bias that can be subtracted afterwards" % (ra, rb)
        )

    for key, label in (("system", "system"), ("profile", "profile")):
        if a.get(key) and b.get(key) and a[key] != b[key]:
            print("note: %s differs -- %s vs %s" % (label, a[key], b[key]))

    ka = (a.get("host") or {}).get("kernel")
    kb = (b.get("host") or {}).get("kernel")
    if ka and kb and ka != kb:
        print("note: kernel differs -- %s vs %s" % (ka, kb))
    elif ka and kb and ka == kb and a.get("system") != b.get("system"):
        print(
            "note: same kernel release (%s) on both sides. If this was meant to be a "
            "kernel A/B, the patched build needs its own LOCALVERSION -- otherwise "
            "the two are indistinguishable here and their modules collide on disk." % ka
        )


def main(argv):
    parser = argparse.ArgumentParser(prog="wkdata.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get", help="print one dotted field out of a JSON file")
    p.add_argument("file")
    p.add_argument("key")
    p.add_argument("--default", default="")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("ls-summary", help="one `wk bench ls` line for a saved run's env.json")
    p.add_argument("file")
    p.set_defaults(func=cmd_ls_summary)

    p = sub.add_parser("plan-spec", help="a plan's fetchable source, read from stdin")
    p.set_defaults(func=cmd_plan_spec)

    p = sub.add_parser("merge-jsc-logs", help="merge jsc-shell iteration logs into one result.json")
    p.add_argument("out")
    p.add_argument("logs", nargs="+")
    p.set_defaults(func=cmd_merge_jsc_logs)

    p = sub.add_parser("env-record", help="write a run's env.json")
    p.add_argument("out")
    p.add_argument("fields", nargs="*", metavar="key=value")
    p.add_argument("--bool", dest="bool_fields", action="append", default=[], metavar="key=value")
    p.set_defaults(func=cmd_env_record)

    p = sub.add_parser("axis-check", help="warn where two runs are not comparable")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_axis_check)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
