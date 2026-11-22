"""Install a server's operating system from nothing, unattended, through its BMC. Its disk is erased.

It runs `ansible-playbook --limit <server> ansible/install.yml` and its exit code is ansible-playbook's. The playbook
puts an autoinstall file into Ubuntu's pinned image, attaches the image to the server as its BMC's virtual USB disk
(qemu-nbd serves it on this machine, websocat carries it to the BMC's websocket), boots the server from it once, and
waits for the installed system to join the tailnet; then it retires the old system's node, gives the new one the
server's name and waits for ssh. The image is detached and removed however the run ends. Then `wk machine setup
<server>` converges it.

The server's conf says how: bmc=, arch=, os=, and wifi= and console= where they apply (DESIGN.md, Installing a
server). The store holds bmc/<server>/user, bmc/<server>/password, tailnet-auth-key and tailnet-api-key, wifi-ssid and
wifi-psk for a server with wifi=, and sudo-password while the system being replaced asks for one.

Refused while the server holds a workspace, which a rehearsal leaves alone. A server that does not answer is installed
all the same, restarted by its BMC. While it runs, the server's serial console is `ssh -p 2200 <BMC login>@<bmc>`.
`--dry-run` asks the store and the BMC and changes nothing."""

from wk.core import act, route
from wk.core.act import barrier, confirm, die, info
from wk.core.decl import Command, Opt
from wk.provision import ansible

COMMAND = Command(where="client", takes=("server",), destructive=True,
                  opts=(Opt("--rehearse", "attach the image, see it from the server and detach it: nothing boots and no disk is erased"),))
NOT_INSTALLED = {"client": "the client", "borrowed": "a borrowed box", "mac": "a Mac"}


def main(args, inv):
    conf = inv.fleet.get(args.server)
    name = conf.name
    if conf.kind in NOT_INSTALLED:
        die("%s is %s, and install puts Ubuntu on a Linux server of yours" % (name, NOT_INSTALLED[conf.kind]))
    if not conf.get("bmc"):
        die("%s has no BMC to be installed through: set bmc=<its BMC's address> in machines/%s.conf (`wk machine install -h` "
            "lists the other keys)" % (name, name))
    if args.rehearse:
        confirm("attach the install image to %s through its BMC at %s, in place of one an earlier run left? nothing boots" % (name, conf.get("bmc")))
        return ansible.play(inv, name, None, "install.yml", ["--skip-tags", "wipe"])
    if conf.kind == "credential":
        barrier("%s is the credential server: its disk holds the store and the claude.ai login, and both go with it" % name)
    if conf.holds_workspaces:
        held, silent = route.workspaces(inv, [name])
        if held:
            barrier("%s holds %s, and its disk is erased: remove each with `wk rm <ws>`" % (name, ", ".join(ws.name for ws in held)))
        if silent:
            info("%s does not answer, so what it holds is unknown: a machine being reinstalled usually does not" % name)
    confirm("ERASE %s's disk and install Ubuntu on it, through its BMC at %s?" % (name, conf.get("bmc")))
    rc = ansible.play(inv, name, None, "install.yml")
    if rc == 0 and not act.flags.dry_run:
        info("%s is installed and on the tailnet; converge it next: wk machine setup %s" % (name, name))
    return rc
