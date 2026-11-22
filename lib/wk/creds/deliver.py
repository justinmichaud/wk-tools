"""Delivery: what `wk key sync` puts on one of your servers, and how it gets there.

Every secret lands under <wk home>/secrets (0700), which is the one directory the agent wrapper hides. Outside it go
only what an agent session needs: the injector's CA, the placeholder claude.ai login, and pi's models.json.

A file is written only when the far side's sha256 or mode differs; a secret is never read back. Each write is
atomic, so a killed run is finished by running it again."""

import dataclasses
import hashlib
import json
import shlex

from wk.core.act import barrier, die, warn
from wk.core.errors import NEEDS_PERSON
from wk.creds import injector, rules

# What git-webkit (webkitcorepy.credentials: <HOST>_USERNAME and <HOST>_TOKEN or _PASSWORD) and gh read.
BUGZILLA = rules.BUGZILLA.split("//")[1].upper().replace(".", "_")
ENV = (("GITHUB_COM_USERNAME", "github-user"), ("GITHUB_COM_TOKEN", "github-token"), ("GH_TOKEN", "github-token"),
       (BUGZILLA + "_USERNAME", "bugzilla-user"), (BUGZILLA + "_PASSWORD", "bugzilla-password"))
DELIVERED = ("github-user", "github-token", "bugzilla-user", "bugzilla-password", "litellm-key")
RC = {"linux": ".bashrc", "mac": ".zshrc"}
CA = "injector-ca.pem"
LOGIN_DIR = "claude-login"

STATUS = """import hashlib, json, os, sys
out = {}
for path in sys.argv[1:]:
    try:
        mode = "%o" % (os.stat(path).st_mode & 0o777)
    except FileNotFoundError:
        out[path] = None
        continue
    digest = None
    if os.path.isfile(path):
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    out[path] = [mode, digest]
print(json.dumps(out))
"""


@dataclasses.dataclass(frozen=True)
class Material:
    """What one sync delivers to every server. `unknown` lists what could not be learned, which is left as it was."""
    values: dict
    ca: object
    pi: object
    unknown: tuple


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def status(machine, paths):
    """{path: [mode, sha256 or None for a directory], or None when absent}; None when the machine could not say."""
    r = machine.run(["python3", "-I", "-c", STATUS] + list(paths), timeout=60)
    try:
        return json.loads(r.out) if r.ok else None
    except ValueError:
        return None


def alias(repo):
    """The ssh host alias that selects a repository's deploy key. ssh lowercases the host it is given."""
    return "github-" + repo.replace("/", "-").lower()


def key_path(secrets, repo):
    return "%s/deploy/%s" % (secrets, repo.replace("/", "--"))


def pi_models(url, key, ids):
    return json.dumps({"providers": {"litellm": {"baseUrl": url, "api": "openai-completions", "apiKey": key,
                                                 "models": [{"id": i} for i in ids]}}}, indent=2) + "\n"


def gather(inv):
    """Read the store, the injector's CA and the model list once, saying what is absent."""
    store, unknown = inv.secrets(), []
    values = {name: store.get(name) for name in store.list() if name in DELIVERED or rules.repos([name])}
    for name in DELIVERED:
        if name not in values:
            warn("%s is not in the store, so it is not delivered: `wk key set %s`" % (name, name))
    vault = inv.fleet.credential.name
    far = inv.machine(vault)
    ca = far.read(inv.wk_home_on(far) + "/injector/ca.pem")
    if ca is None:
        warn("%s has no injector yet, so its CA is not delivered: `wk machine setup %s`" % (vault, vault))
    url, pi = inv.fleet.here.get("litellm_url"), None
    if "litellm-key" in values and not url:
        warn("no litellm_url= in machines/%s.conf, so pi's models.json is not delivered" % inv.fleet.here.name)
    elif "litellm-key" in values:
        answered, doc = rules.litellm_models(values["litellm-key"], url)
        if answered == 200:
            pi = pi_models(url, values["litellm-key"], [m["id"] for m in doc.get("data") or []])
        else:
            unknown.append("pi's models.json (%s)" % rules.unanswered("GET %s/models" % url, answered, doc)[1])
    return Material(values, ca, pi, tuple(unknown))


def files(home, wk_home, material):
    """[(path, mode, content)] for a server whose login's home is `home`."""
    secrets, values = wk_home + "/secrets", material.values
    repos = rules.repos(sorted(values))
    env = ["# Written by `wk key sync` and sourced by an interactive shell. An agent session cannot read it."]
    env += ["export %s=%s" % (var, shlex.quote(values[name])) for var, name in ENV if name in values]
    # git reads these as configuration (2.31 on): a push to the fork's https URL goes through the alias that selects its key.
    for n, repo in enumerate(repos):
        env += ["export GIT_CONFIG_KEY_%d=%s" % (n, shlex.quote("url.git@%s:%s.git.pushInsteadOf" % (alias(repo), repo))),
                "export GIT_CONFIG_VALUE_%d=%s" % (n, shlex.quote("https://github.com/%s.git" % repo))]
    env += ["export GIT_CONFIG_COUNT=%d" % len(repos)] if repos else []
    ssh = ["# Written by `wk key sync`: one alias per repository, each offering that repository's deploy key alone."]
    for repo in repos:
        ssh += ["Host %s" % alias(repo), "    HostName github.com", "    User git", "    IdentityFile %s" % key_path(secrets, repo),
                "    IdentitiesOnly yes"]
    out = [(key_path(secrets, repo), "600", values[rules.DEPLOY + repo].strip() + "\n") for repo in repos]
    out += [(secrets + "/ssh_config", "600", "\n".join(ssh) + "\n"), (secrets + "/env.sh", "600", "\n".join(env) + "\n"),
            ("%s/%s/.credentials.json" % (wk_home, LOGIN_DIR), "600", injector.placeholder())]
    out += [("%s/%s" % (wk_home, CA), "644", material.ca)] if material.ca else []
    out += [(home + "/.pi/agent/models.json", "600", material.pi)] if material.pi else []
    return out


def lines(home, wk_home, kind):
    """[(path, mode, line, whether it goes first)]: the one line each that makes ssh and the login shell read secrets/."""
    secrets = wk_home + "/secrets"
    return [(home + "/.ssh/config", "600", "Include %s/ssh_config" % secrets, True),
            ("%s/%s" % (home, RC[kind]), "644", "[ -r %s/env.sh ] && . %s/env.sh" % (secrets, secrets), False)]


def server(inv, conf, material):
    """Converge one server; prints a row per item and returns whether anything changed."""
    machine = inv.machine(conf.name)
    home, wk_home, changed = machine.home, inv.wk_home_on(machine), False
    dirs = [wk_home + "/secrets", wk_home + "/secrets/deploy"]
    items = files(home, wk_home, material)
    asked = dirs + [path for path, _, _ in items]
    have = status(machine, asked)
    if have is None:
        barrier("%s could not say what it holds (python3 -I failed there), so nothing is delivered to it\n"
                "    `wk machine setup %s` installs what a server needs" % (conf.name, conf.name), NEEDS_PERSON)
        have = dict.fromkeys(asked)     # forced: everything is written as if absent

    def row(same, path):
        print("%-12s %-10s %s" % (conf.name, "unchanged" if same else "changed", path))
        return not same
    if any(have[d] != ["700", None] for d in dirs):
        machine.act(["install", "-d", "-m", "700"] + dirs)
        changed = True
    for path, mode, content in items:
        if row(have[path] == [mode, digest(content)], path):
            machine.write(path, content, mode)
            changed = True
    for path, mode, line, first in lines(home, wk_home, conf.kind):
        text = machine.read(path) or ""
        if row(line in text.splitlines(), path):
            machine.write(path, "%s\n%s" % (line, text) if first else "%s\n%s\n" % (text.rstrip("\n"), line), mode)
            changed = True
    return changed


def converge(inv, conf):
    """The setup stage: deliver to one server, refusing to call it done while something is unknown."""
    material = gather(inv)
    changed = server(inv, conf, material)
    unknown(material.unknown)
    return changed


def unknown(what):
    if what:
        die("not everything is delivered: %s\n    it is left as it was; run `wk key sync` again" % "; ".join(what), NEEDS_PERSON)
