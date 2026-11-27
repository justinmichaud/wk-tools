"""Say, for each credential, whether the store holds it and whether its issuer accepts it.

One row per credential: its name, present or absent, and the verdict of a live probe that writes nothing: ok, bad,
or unknown when the issuer did not answer or cannot be asked. It exits 1 when a credential is bad."""

from wk.core.decl import CONTROLLER, Command
from wk.creds import rules

COMMAND = Command(on=CONTROLLER, readonly=True, default=True)


def main(args, inv):
    store = inv.secrets()
    held = store.list()
    ctx = rules.Context(store.get, inv.clock.now(), inv.fleet.here.get("litellm_url"))
    bad = False
    for name in [r.name for r in rules.RULES] + rules.named(held):
        rule = rules.find(name)
        if name not in held:
            print("%-28s %-8s %-8s %s: `wk key set %s`" % (name, "absent", "-", rule.what, name))
            continue
        verdict, detail = rule.probe(store.get(name), ctx)
        bad = bad or verdict == rules.BAD
        print("%-28s %-8s %-8s %s" % (name, "present", verdict, detail))
    return 1 if bad else 0
