"""Boot drivers: the interface and its Channel (driver.py), the Pi arrangements (pi.py) and the Macs (mac.py)."""


def drivers():
    from wk.boot import mac, pi
    return dict(pi.DRIVERS, **mac.DRIVERS)


def driver_class(name):
    from wk.boot import driver
    return drivers().get(name, driver.Driver)


def open_driver(root, conf, env=None, via=None):
    cls = driver_class(conf.get("driver", ""))
    return cls(root, conf, cls.channel(root, conf, env=env, via=via))
