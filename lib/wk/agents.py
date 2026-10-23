"""Claude Code and pi, installed as a workspace is made (a macOS guest at its first start) from the agents cache, which only its machine's side writes."""

import json
import os
import shlex

from wk.act import die, info, log, warn
from wk.kv import ASSIGN, kv
from wk.secrets import Secrets, first_line

AGENTS = ("claude", "pi")
SCRIPT = "container/agents.sh"
CLAUDE_VERSION = "2.1.308"
HOME_CACHE = "$HOME/.cache/wk/agents"


def script(root):
    with open(os.path.join(str(root), SCRIPT)) as f:
        return f.read()


def verb_argv(root, *verb):
    return ["bash", "-lc", script(root), SCRIPT, *verb]


# pi has no LiteLLM provider (https://github.com/earendil-works/pi#4561); LiteLLM speaks OpenAI's wire format.
def pi_models(endpoint, ids):
    return json.dumps({"providers": {"litellm": {
        "baseUrl": endpoint,
        "api": "openai-completions",
        "apiKey": "$LITELLM_API_KEY",
        "models": [{"id": i} for i in ids],
    }}}, indent=2)


def install(root, env, machine, run, ws, tools, src, cache, fill):
    """`run(argv)` is an effect in the workspace, which reads the agents artifacts at `cache`; `fill` is (effect, path): what writes them, and where."""
    export = "export WK_AGENTS_CACHE=%s WK_CLAUDE_VERSION=%s\n%s"
    r = fill[0](["bash", "-lc", export % (fill[1], CLAUDE_VERSION, script(root)), SCRIPT, "fetch"])
    r = run(["bash", "-lc", export % (cache, CLAUDE_VERSION, script(root))]) if r.ok else r
    said = kv(r.out)
    for agent in AGENTS:
        if said.get(agent) == "installed":
            info("%s installed in '%s'" % (agent, ws))
    if said.get("pi", "").startswith("no-node"):
        log("  no pi in '%s': it needs node >= 22.19 and npm, and found node %s" % (ws, said["pi"].split(" ", 1)[-1]))
    if not r.ok:
        why = [l.rstrip() for l in (r.out + "\n" + r.err).replace("\r", "").splitlines() if l.strip() and not ASSIGN.match(l)]
        die("could not install the coding agents in '%s' (%s).%s\n    claude.ai must answer where the agents cache is written, "
            "registry.npmjs.org the workspace;\n    'wk new %s' destroys the half-made workspace and tries again."
            % (ws, " ".join("%s=%s" % (a, said.get(a, "?")) for a in AGENTS), "".join("\n    " + l for l in why), ws))
    config = run(["bash", "-lc", "python3 %s %s" % (shlex.quote(tools + "/claude/workspace-config.py"), shlex.quote(src))])
    if not config.ok:
        die("could not record Claude's start-up answers in '%s', and every session there\n    would stop at a dialog instead "
            "of starting:\n    %s" % (ws, (config.out + config.err).replace("\r", "").strip().replace("\n", "\n    ")))
    if said.get("pi") in ("present", "installed"):
        write_pi_models(root, env, machine, ws, *fill)


def write_pi_models(root, env, machine, ws, run, cache):
    models, sec = '"%s/pi/models.json"' % cache, Secrets(root, env, machine)
    if not sec.cred_stored("litellm"):
        log("  no LiteLLM key on this machine ('wk key set litellm'), so pi in '%s' asks you to /login" % ws)
        return run(["bash", "-c", "rm -f " + models])
    import credcheck
    endpoint, key = credcheck.LITELLM_ENDPOINT, first_line(sec.cred_read("litellm"))
    try:
        ids = credcheck.litellm_models(key)
        first = credcheck.litellm_callable(key, ids)
    except credcheck.Unreachable as e:
        return warn("could not ask %s which models the LiteLLM key may call (%s),\n    so pi's models.json is left as it was" % (endpoint, e))
    if not first:
        warn("%s answers a completion from none of the %d chat model(s) it lists for the stored LiteLLM key,\n    so pi "
             "has nothing to call: 'wk key check' says what the endpoint makes of the key" % (endpoint, len(ids)))
        return run(["bash", "-c", "rm -f " + models])
    write = "mkdir -p \"%s/pi\" && umask 077 && printf '%%s\\n' %s > %s.$$ && mv -f %s.$$ %s" % (
        cache, shlex.quote(pi_models(endpoint, [first] + [i for i in ids if i != first])), models, models, models)
    if not run(["bash", "-c", write]).ok:
        die("could not write pi's models.json for this machine from '%s'" % ws)
    info("wrote pi's models.json for this machine from '%s' ($LITELLM_API_KEY, %s; %d model(s), %s first)" % (ws, endpoint, len(ids), first))
