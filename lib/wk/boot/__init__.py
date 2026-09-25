"""Boot drivers: the interface (driver.py), the Pi arrangements (pi.py), the Macs (mac.py) and a board in memory (fake.py)."""


def drivers():
    from wk.boot import mac, pi
    return dict(pi.DRIVERS, **mac.DRIVERS)


def driver_class(name):
    from wk.boot import driver
    return drivers().get(name, driver.Driver)


def open_driver(root, conf, env=None, via=None, channel="none", mode="", name=None):
    cls = driver_class(conf.get("NODE_DRIVER", "") if name is None else name)
    return cls(root, conf, cls.transport(root, conf, channel, env=env, via=via), mode=mode)
