from tests.provision.toy.one.hooks import mark
from wk.core.hooks import Stage


def stages(inv, conf):
    return [Stage(20, "second", mark(inv, conf, "second"))]
