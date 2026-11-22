"""The exit-code vocabulary and the exceptions that carry it to the dispatcher."""

OK, FAILED, BUSY, STALLED, NEEDS_PERSON, REFUSED_FOR_NOW = 0, 1, 2, 3, 4, 75


class Refused(Exception):
    """A command stops here: the message says why and names the remedy; `status` is the exit code."""

    def __init__(self, message, status=FAILED):
        super().__init__(message)
        self.status = status


class Usage(Exception):
    """The command line names something the command's table does not declare; the dispatcher adds the usage line."""


class Unreachable(Refused):
    """A machine did not answer. What it holds is unknown, never absent."""

    def __init__(self, name, why=""):
        super().__init__("cannot reach %s%s\n    check `tailscale status` and that the machine is up" % (name, why and ": " + why.strip()),
                         NEEDS_PERSON)
        self.name = name


class Bug(Exception):
    """A command broke a rule of its own table. Never caught: it is a traceback, in a test first."""
