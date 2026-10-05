"""The project lab code checks out, builds and measures, as names the wk layer hands in before any is read."""

_HANDED = {}


def hand_in(**names):
    _HANDED.update(names)


def source():
    return "from wk import project\nproject.hand_in(**%r)\n" % {k: v for k, v in _HANDED.items() if not callable(v)}


def __getattr__(name):
    try:
        return _HANDED[name]
    except KeyError:
        raise AttributeError("wk.project.%s has not been handed in: run lib/wk modules as `python3 -m wk <module>`" % name) from None
