"""act, confirm and barrier: the rules lib/common.sh holds, from the same variables, so a bash and a Python command behave alike under --dry-run, --yes, --force and a destructive declaration."""

import os
import shlex
import subprocess
import sys

RETRY_EXIT = 75   # lib/common.sh's WK_RETRY_EXIT is this number; lib/wk/sched.py retries a step on it


class Refused(Exception):
    def __init__(self, status=1):
        self.status = status


def _colour(code):
    try:
        return code if os.isatty(2) else ""
    except OSError:
        return ""


def log(msg):
    if not os.environ.get("WK_QUIET"):
        sys.stderr.write(msg + "\n")


def info(msg):
    if not os.environ.get("WK_QUIET"):
        sys.stderr.write("%s==>%s %s\n" % (_colour("\033[32m"), _colour("\033[0m"), msg))


def warn(msg):
    sys.stderr.write("%swarning:%s %s\n" % (_colour("\033[33m"), _colour("\033[0m"), msg))


def err(msg):
    sys.stderr.write("%serror:%s %s\n" % (_colour("\033[31m"), _colour("\033[0m"), msg))


def die(msg, status=1):
    err(msg)
    raise Refused(status)


def debug(msg):
    if os.environ.get("WK_DEBUG"):
        sys.stderr.write("%s  %s%s\n" % (_colour("\033[2m"), msg, _colour("\033[0m")))


def dry_run():
    return bool(os.environ.get("WK_DRY_RUN"))


def exec_into(argv, cwd=None, env=None):
    """Replaces this process, so the far side's tty and job control are the caller's own."""
    if cwd is not None:
        os.chdir(cwd)
    sys.stdout.flush()
    sys.stderr.flush()
    if env is not None:
        os.execvpe(argv[0], argv, env)
    os.execvp(argv[0], argv)


def confirm(prompt, stdin=None):
    if dry_run():
        sys.stderr.write("would ask: %s [y/N]\n" % prompt)
        os.environ["WK_CONFIRMED"] = "1"
        return True
    if os.environ.get("WK_YES"):
        os.environ["WK_CONFIRMED"] = "1"
        return True
    stdin = sys.stdin if stdin is None else stdin
    try:
        tty = stdin.isatty()
    except (AttributeError, OSError):
        tty = False
    if not tty:
        warn("%s -- declining (no terminal; re-run interactively, or pass --yes)" % prompt)
        return False
    sys.stderr.write("%s [y/N] " % prompt)
    sys.stderr.flush()
    reply = stdin.readline()
    if reply.lower().startswith("y"):
        os.environ["WK_CONFIRMED"] = "1"
        return True
    return False


def nothing_to_ask():
    """A destructive command whose destructive part does not apply this run says so here, and then acts unasked."""
    os.environ["WK_CONFIRMED"] = "1"


def asked():
    return bool(os.environ.get("WK_CONFIRMED"))


def act(argv, **kw):
    if dry_run():
        sys.stderr.write("would run: %s\n" % " ".join(shlex.quote(a) for a in argv))
        return None
    if os.environ.get("WK_DESTRUCTIVE") and not asked():
        die("BUG: this command is declared destructive and acted before asking:\n    %s"
            % " ".join(shlex.quote(a) for a in argv))
    debug("run: %s" % " ".join(shlex.quote(a) for a in argv))
    return subprocess.run(argv, **kw)


_forced = []


def barrier(message, retry=False):
    if not os.environ.get("WK_FORCE"):
        err("%s\n    --force proceeds anyway, with a warning." % message)
        raise Refused(RETRY_EXIT if retry else 1)
    warn("FORCED past a barrier: %s" % message)
    if not _forced:
        import atexit
        atexit.register(forced_summary)
    _forced.append(message.splitlines()[0])


def forced_summary():
    if not _forced:
        return
    sys.stderr.write("%swarning:%s this command was forced past %d barrier(s):\n%s\n"
                     % (_colour("\033[33m"), _colour("\033[0m"), len(_forced),
                        "\n".join("- " + m for m in _forced)))
