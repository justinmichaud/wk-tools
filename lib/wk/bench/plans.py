"""The plans and what runs them: a plan's class, run-benchmark's arguments, the jsc shell's, and the pinned payload."""

import json

from wk import record as progress, webkit
from wk.act import die, warn
from wk.bench import seed
from wk.machine import replace_file

# gpu by default: guessing gpu fails as an easy refusal, guessing cpu as a MotionMark score off llvmpipe.
CPU_PLANS = ("jetstream", "octane", "kraken", "sunspider", "ares6", "jsbench")


def bench_class(plan):
    return "cpu" if plan.startswith(CPU_PLANS) else "gpu"


def measure_args(leg, output, payload):
    return (["--plan", leg.plan, "--output-file", output, "--no-adjust-unit", "--show-iteration-values"]
            + (["--count", leg.count] if leg.count else []) + (["--timeout", leg.o["timeout"]] if leg.o.get("timeout") else [])
            + (["--local-copy", payload] if payload else []) + (["--subtests"] + leg.subtests.split() if leg.subtests else []))


def browser_args(leg, output, payload, build):
    extra = (["--headless"] if leg.software else []) + (leg.o.get("browser_args") or "").split() + leg.args
    return measure_args(leg, output, payload) + ["--build-directory", build] + (["--"] + extra if extra else [])


def board_runner_args(leg, port, diagnose, output):
    """run-benchmark driving a board's browser (lib/wk/bench/board_driver.py) from this host's page server."""
    return ([webkit.BENCH_RUNNER, "--browser", "wk-board", "--platform", "linux", "--driver", "webserver", "--http-server-type", "builtin",
             "--http-server-port", port, "--diagnose-directory", diagnose] + measure_args(leg, output, leg.payload)
            + (["--generate-pgo-profiles"] if leg.o.get("pgo_dir") else []))


def shell_argv(shell, args, subtests):
    return [shell] + list(args) + ["cli.js", "--", "--dump-json-results"] + (["--test=" + ",".join(subtests.split())] if subtests else [])


def pin_payload(machine, lock, store, ws_driver, ws, plan):
    return seed.pin(machine, lock, store, seed.ws_reader(ws_driver, ws), plan)[1]


def _last_json(path):
    for line in reversed(progress.normalised(path).split("\n")):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                return doc
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


def merge_jsc_logs(out, logs):
    merged, missing = None, []
    for path in logs:
        one = _last_json(path)
        if one is None:
            missing.append(path)
        else:
            merged = one if merged is None else _merge(merged, one)
    if merged is None:
        die("no results in any iteration log -- the suite printed no JSON. A payload whose cli.js does not\n"
            "    accept --dump-json-results runs the whole suite and reports only in its own text format.")
    if missing:
        warn("no results in %s" % ", ".join(missing))
    replace_file(out, json.dumps(merged, indent=2))
