"""Deliver the store's credentials to your servers, or to one.

Each Linux and Mac server gets, under ~/wk/secrets (which an agent session cannot read): the deploy keys with one
ssh alias per repository, and env.sh, which an interactive shell sources so that git-webkit, gh and `git push` work
as they are. Outside it go the injector's CA, the placeholder claude.ai login, and pi's models.json, which holds the
LiteLLM key: the one key an agent can read. A borrowed machine gets nothing; ssh agent forwarding serves it.

One row per item says changed or unchanged: a file is written only when its digest or mode differs."""

from wk.core.decl import Command
from wk.core.errors import Unreachable
from wk.creds import deliver

COMMAND = Command(where="client", takes=("server?",))


def main(args, inv):
    confs = [inv.fleet.server(args.server)] if args.server else inv.fleet.servers()
    material = deliver.gather(inv)
    unknown = list(material.unknown)
    for conf in confs:
        if conf.kind == "borrowed":
            print("%-12s %-10s a borrowed machine holds no credential: ssh agent forwarding makes `git push` work there" % (conf.name, "skipped"))
            continue
        try:
            deliver.server(inv, conf, material)
        except Unreachable:
            print("%-12s %-10s it did not answer" % (conf.name, "unknown"))
            unknown.append("%s did not answer" % conf.name)
    deliver.unknown(unknown)
