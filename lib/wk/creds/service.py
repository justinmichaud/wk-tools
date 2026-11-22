"""The injector as a service on the credential server: its setup stage, its doctor row, and what an agent session
is started with to use it."""

from wk.core import hooks
from wk.core.act import die, warn
from wk.core.errors import NEEDS_PERSON
from wk.creds import deliver, injector, net

SERVICE = "wk-injector.service"
UNIT = """[Unit]
Description=wk injector: the one holder of the claude.ai login

[Service]
ExecStart=/usr/bin/env python3 -I %s/injector.py %s %s
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def port(conf):
    return conf.get("injector_port", injector.PORT)


def agent_env(inv):
    """The environment an agent session, and nothing else, is started with: the injector as its proxy, the injector's
    CA added to its trust, and the directory holding the placeholder login."""
    vault = inv.fleet.credential
    proxy = "http://%s:%s" % (vault.name, port(vault))
    return {"HTTPS_PROXY": proxy, "https_proxy": proxy, "NODE_EXTRA_CA_CERTS": "%s/%s" % (inv.wk_home, deliver.CA),
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "%s/%s" % (inv.wk_home, deliver.LOGIN_DIR)}


def setup(inv, conf):
    """Converge the injector on the credential server; returns whether anything changed."""
    if conf.kind != "linux":
        die("the credential server %s is kind=%s, and the injector is installed as a systemd user unit\n    make a Linux server the credential server"
            % (conf.name, conf.kind))
    machine = inv.machine(conf.name)
    directory, unit = inv.wk_home_on(machine) + "/injector", "%s/.config/systemd/user/%s" % (machine.home, SERVICE)
    program, login, marker = directory + "/injector.py", directory + "/login/.credentials.json", directory + "/login/delivered"
    dirs = [directory, directory + "/login"]
    wanted = [(program, "600", inv.here.read("%s/lib/wk/creds/injector.py" % inv.root)), (unit, "644", UNIT % (directory, directory, port(conf)))]
    have = deliver.status(machine, dirs + [program, unit])
    if have is None:
        die("%s could not say what it holds (python3 -I failed there), so the injector is not installed" % conf.name, NEEDS_PERSON)
    changed = False
    if any(have[d] != ["700", None] for d in dirs):
        machine.act(["install", "-d", "-m", "700"] + dirs)
        changed = True
    stale = [(path, mode, content) for path, mode, content in wanted if have[path] != [mode, deliver.digest(content)]]
    if stale:
        # Stopped before it is replaced: a run killed from here on leaves it stopped, which the next run sees and starts.
        machine.act(["systemctl", "--user", "stop", SERVICE])
        for path, mode, content in stale:
            machine.write(path, content, mode)
        changed = True
    doc = inv.secrets().get("claude-login")
    if doc is None:
        warn("claude-login is not in the store, so the injector holds no login: `wk key set claude-login`, then this again")
    # The injector rewrites the login at each refresh, so the file is compared with what was last delivered, not with the store.
    elif machine.read(marker) != deliver.digest(doc):
        machine.write(login, doc, "600")
        machine.write(marker, deliver.digest(doc), "600")
        changed = True
    user = machine.run(["id", "-un"]).out.strip()
    # A user unit runs without a login session only while the user lingers.
    if not machine.exists("/var/lib/systemd/linger/" + user):
        machine.act(["loginctl", "enable-linger", user])
        changed = True
    if not all(machine.run(["systemctl", "--user", ask, SERVICE]).ok for ask in ("is-enabled", "is-active")):
        for verb in (["daemon-reload"], ["enable", SERVICE], ["restart", SERVICE]):
            machine.act(["systemctl", "--user"] + verb)
        changed = True
    return changed


def doctor(inv, conf):
    """One row: a plain request to the injector, which answers with its banner and whether it holds a login."""
    remedy, at = "wk machine setup %s" % conf.name, "%s:%s" % (conf.name, port(conf))
    try:
        _, body = net.request("GET", "http://%s/" % at)
    except net.NoAnswer as e:
        return hooks.DoctorRow("injector", hooks.UNKNOWN, "%s did not answer (%s)" % (at, e), remedy)
    said = body.decode(errors="replace").splitlines()
    if not said or not said[0].startswith(injector.BANNER.decode()):
        return hooks.DoctorRow("injector", hooks.MISSING, "something else answers at %s" % at, remedy)
    if said[1:] != ["login held"]:
        return hooks.DoctorRow("injector", hooks.MISSING, "".join(said[1:]), "wk key set claude-login && " + remedy)
    return hooks.DoctorRow("injector", hooks.OK, "%s holds the claude.ai login" % at)
