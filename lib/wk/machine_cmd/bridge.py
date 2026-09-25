"""`wk machine setup|rm|tailnet|status` of a bridge phone, handed to its role (wk.bridge.role)."""

from wk.act import die
from wk.bridge import Unreachable, role
from wk.workspace import require_name


class BridgeMachines:
    def bridge(self):
        return role.Role(self.root, self.env, self.here, phones=None if self.far is None else (lambda dest: self.far))

    def refuse_bridge_flags(self, conf, given):
        if given:
            die("--at, --no-tailnet, --disk, --image and --rebuild are a bridge's, and this is a %s" % conf["KIND"])

    def tailnet(self, name, at=None):
        require_name(name)
        conf = self.conf(name)
        if conf is None or conf["KIND"] != "bridge":
            die("'%s' is not a bridge: 'wk machine tailnet' joins a bridge phone to the tailnet" % name)
        return self.bridge().tailnet(name, at=at)

    def status(self, name=None, at=None):
        b = self.bridge().b
        if name is None:
            return b.ls()
        require_name(name)
        conf = self.conf(name)
        if conf is None or conf["KIND"] != "bridge":
            die("'%s' is not a bridge: 'wk machine status' is a bridge phone's health check; 'wk status' is every machine's" % name)
        try:
            return b.status(name, at=at)
        except Unreachable as e:
            die(str(e))
