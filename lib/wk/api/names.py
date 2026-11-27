"""The fleet's workspace names, the one record wk keeps: a file per name under the controller's <wk home>/ws, holding
the name of the server that reserved it. A name is reserved by creating its file with O_EXCL, so two reservations
cannot both win."""


def path(home, name):
    return "%s/ws/%s" % (home, name)


def holder(m, home, name):
    """The server holding `name`, or None when no server does."""
    text = m.read(path(home, name))
    return None if text is None else text.strip()


def reserve(m, home, name, server):
    """The server holding `name` once this returns: `server` when the name was free or already its."""
    while not m.create(path(home, name), server):
        held = holder(m, home, name)
        if held is not None:
            return held
    return server


def release(m, home, name):
    m.remove(path(home, name))


def holders(m, home):
    """{name: server} for every reserved name."""
    return {name: holder(m, home, name) for name in m.listdir(home + "/ws")}
