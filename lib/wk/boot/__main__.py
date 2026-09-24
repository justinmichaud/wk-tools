"""python3 -m wk.boot <driver> <verb> [args]: one verb of a boot driver over this process's NODE_*, MODE, MODE_CHANNEL,
ARM_SYS_PART (boot/machines.sh's shims). A refusal exits 1; a predicate verb answers by its exit status."""

import os
import shlex
import sys

from wk import act
from wk.boot import driver_class
from wk.boot.driver import BashChannel, Driver


def build(name, env, root):
    cls = driver_class(name)
    conf = {k: v for k, v in env.items() if k.startswith("NODE_")}
    ch = BashChannel(root, conf, env.get("MODE_CHANNEL") or "none",
                     bash_driver=cls is Driver and bool(conf.get("NODE_DRIVER")))
    d = cls(root, conf, ch, mode=env.get("MODE", ""))
    d.armed_part = env.get("ARM_SYS_PART", "")
    return d


def say(text):
    if text:
        print(text)
    return 0


def pred(ok):
    return 0 if ok else 1


def probe(d):
    d.probe()
    print("MODE=%s MODE_CHANNEL=%s" % (shlex.quote(d.mode), shlex.quote(d.ch.channel)))
    return 0


def systems(d):
    found = d.systems()
    if found is None:
        return 1
    return say("\n".join("%s %s" % s for s in found))


VERBS = {
    "facts": lambda d, a: say("\n".join("%s=%s" % (k, shlex.quote(v)) for k, v in d.facts().items())),
    "probe": lambda d, a: probe(d),
    "probeable": lambda d, a: pred(d.probeable()),
    "system-kind": lambda d, a: say(d.system_kind(a[0] if a else "")),
    "boot-id": lambda d, a: say(d.boot_id()),
    "booted-at": lambda d, a: say(d.booted_at()),
    "display": lambda d, a: say(d.display()) if d.display() else 1,
    "watchdog-present": lambda d, a: pred(d.watchdog_present()),
    "systems": lambda d, a: systems(d),
    "select": lambda d, a: say("%s %s" % d.select_system(a[0] if a else "")),
    "diag": lambda d, a: say(d.diag()),
    "arm": lambda d, a: d.arm(d.armed_part, a[0] if a else ""),
    "reboot": lambda d, a: d.reboot(armed="--armed" in a),
    "disarm": lambda d, a: d.disarm(),
    "disarm-note": lambda d, a: say(d.disarm_note()),
    "self-disarm": lambda d, a: say(d.self_disarm_sh()) if d.failsafe else 1,
    "selects-by-partition": lambda d, a: pred(d.selects_by_partition),
    "media": lambda d, a: say(d.media()),
    "evidence": lambda d, a: say(d.evidence()),
    "reprovision": lambda d, a: say(d.reprovision()),
    "record-write": lambda d, a: d.record_write(*(list(a) + ["", "", "", ""])[:4]),
    "record-read": lambda d, a: say(d.record_read().rstrip("\n")),
    "record-clear": lambda d, a: d.record_clear(),
    "barrier": lambda d, a: d.armed_barrier(a[0] if a else "") or 0,
}


def main(argv, env=None):
    env = os.environ if env is None else env
    if len(argv) < 2 or argv[1] not in VERBS:
        print("usage: python3 -m wk.boot <driver> {%s} [args]" % "|".join(VERBS), file=sys.stderr)
        return 2
    root = env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    try:
        return VERBS[argv[1]](build(argv[0], env, root), argv[2:]) or 0
    except act.Refused as e:
        return e.status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
