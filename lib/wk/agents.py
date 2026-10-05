"""Claude Code and pi, installed when a workspace is made; a macOS guest, not booted then, at its first start."""

import json
import os
import shlex

from wk.act import die, info, log, warn
from wk.kv import kv
from wk.secrets import Secrets, first_line

AGENTS = ("claude", "pi")
SCRIPT = "container/agents.sh"


def script(root):
    with open(os.path.join(str(root), SCRIPT)) as f:
        return f.read()


def find_argv(root, agent):
    return ["bash", "-lc", script(root), SCRIPT, "find", agent]


# pi has no LiteLLM provider (https://github.com/earendil-works/pi#4561); LiteLLM speaks OpenAI's wire format.
def pi_models(endpoint, ids):
    return json.dumps({"providers": {"litellm": {
        "baseUrl": endpoint,
        "api": "openai-completions",
        "apiKey": "$LITELLM_API_KEY",
        "models": [{"id": i} for i in ids],
    }}}, indent=2)


def install(root, env, machine, run, ws, tools, src):
    """`run(argv)` is an effect in the workspace; `machine` holds this machine's store."""
    r = run(["bash", "-lc", script(root)])
    said = kv(r.out)
    for agent in AGENTS:
        if said.get(agent) == "installed":
            info("%s installed in '%s'" % (agent, ws))
    if said.get("pi", "").startswith("no-node"):
        log("  no pi in '%s': it needs node >= 22.19 and npm, and found node %s" % (ws, said["pi"].split(" ", 1)[-1]))
    if not r.ok:
        die("could not install the coding agents in '%s' (%s).\n    The workspace's egress must reach claude.ai and "
            "registry.npmjs.org;\n    'wk new %s' destroys the half-made workspace and tries again."
            % (ws, " ".join("%s=%s" % (a, said.get(a, "?")) for a in AGENTS), ws))
    config = run(["bash", "-lc", "python3 %s %s" % (shlex.quote(tools + "/claude/workspace-config.py"), shlex.quote(src))])
    if not config.ok:
        die("could not record Claude's start-up answers in '%s', and every session there\n    would stop at a dialog instead "
            "of starting:\n    %s" % (ws, (config.out + config.err).replace("\r", "").strip().replace("\n", "\n    ")))
    if said.get("models") == "no" and said.get("pi") in ("present", "installed"):
        write_pi_models(root, env, machine, run, ws)


def write_pi_models(root, env, machine, run, ws):
    sec = Secrets(root, env, machine)
    if not sec.cred_stored("litellm"):
        log("  no LiteLLM key on this machine ('wk key set litellm'), so pi in '%s' asks you to /login" % ws)
        return
    import credcheck
    endpoint, key = credcheck.LITELLM_ENDPOINT, first_line(sec.cred_read("litellm"))
    try:
        ids = credcheck.litellm_models(key)
        first = credcheck.litellm_callable(key, ids)
    except credcheck.Unreachable as e:
        warn("could not ask %s which models the LiteLLM key may call (%s),\n    so pi in '%s' asks you to /login" % (endpoint, e, ws))
        return
    if not first:
        warn("%s answers a completion from none of the %d chat model(s) it lists for the\n    stored LiteLLM key, so pi in '%s' "
             "has nothing to call: 'wk key check' says what\n    the endpoint makes of the key" % (endpoint, len(ids), ws))
        return
    ids = [first] + [i for i in ids if i != first]
    write = "mkdir -p ~/.pi/agent && umask 077 && printf '%%s\\n' %s > ~/.pi/agent/models.json" % shlex.quote(pi_models(endpoint, ids))
    if not run(["bash", "-c", write]).ok:
        die("could not write ~/.pi/agent/models.json in '%s'" % ws)
    info("wrote ~/.pi/agent/models.json in '%s' ($LITELLM_API_KEY, %s; %d model(s), %s first)" % (ws, endpoint, len(ids), ids[0]))
