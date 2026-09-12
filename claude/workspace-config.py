#!/usr/bin/env python3
"""Records in a workspace's ~/.claude.json the answers `wk` has already given,
and the account behind the login it was handed.

Run inside the workspace by cmd/ai, before any `claude` starts. Three prompts
otherwise wait for a terminal the agent may not have, and each one measured
against Claude Code 2.1.268:

  hasCompletedOnboarding        the first-run flow
  remoteDialogSeen              "Enable Remote Control? (y/n)", which
                                `claude remote-control` asks on stdin and
                                answers no -- and exits 0 -- when stdin is
                                /dev/null, as a spawned server's is
  projects[<checkout>]
    .hasTrustDialogAccepted     the workspace trust dialog, without which
                                remote control refuses to start at all
  oauthAccount                  the account record `claude auth login` wrote
                                beside the shared credential, in the CLI's
                                config file in $CLAUDE_SECURESTORAGE_CONFIG_DIR;
                                remote control reads organizationUuid from it
                                and refuses to start without one (measured
                                against 2.1.269), and it is copied in on every
                                start so a rotated login converges

`wk new` created the checkout in a workspace whose whole point is to be the
blast radius, so these are wk's answers to give.

Never overwrites an unreadable file: the CLI keeps live state in this one.
"""
import json
import os
import sys
import tempfile


def account_record():
    """The record beside the shared credential, or (None, why) when a credential
    is there without one; (None, None) where no shared credential is mounted --
    a guest logs in for itself and holds its own record."""
    shared = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if not shared:
        return None, None
    config = os.path.join(shared, ".claude.json")
    try:
        with open(config) as f:
            doc = json.load(f)
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError) as e:
        return None, "%s is not readable as JSON (%s)" % (config, e)
    record = doc.get("oauthAccount") if isinstance(doc, dict) else None
    if isinstance(record, dict) and record.get("organizationUuid"):
        return record, None
    credential = os.path.join(shared, ".credentials.json")
    try:
        held = os.path.getsize(credential) > 0
    except OSError:
        held = False
    if not held:
        return None, None
    return None, ("the login at %s carries no account record (%s, "
                  "oauthAccount.organizationUuid), and remote control refuses "
                  "a login whose organization it cannot read. On the host: "
                  "wk key set claude-login --replace" % (shared, config))


def main(argv):
    if len(argv) != 2:
        print("usage: workspace-config.py <checkout>", file=sys.stderr)
        return 2
    checkout = os.path.realpath(argv[1])
    path = os.path.join(os.path.expanduser("~"), ".claude.json")

    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError) as e:
        print("%s is not readable as JSON (%s), so nothing was recorded in it: "
              "the Claude CLI keeps its live state there and this will not "
              "overwrite it. Remove it to start again." % (path, e),
              file=sys.stderr)
        return 1
    if not isinstance(doc, dict):
        print("%s holds %s, not an object" % (path, type(doc).__name__),
              file=sys.stderr)
        return 1

    record, why = account_record()
    if why:
        print(why, file=sys.stderr)
        return 1

    before = json.dumps(doc, sort_keys=True)
    doc["hasCompletedOnboarding"] = True
    doc["remoteDialogSeen"] = True
    if record is not None:
        doc["oauthAccount"] = record
    projects = doc.setdefault("projects", {})
    if not isinstance(projects, dict):
        print("%s holds no projects object" % path, file=sys.stderr)
        return 1
    project = projects.setdefault(checkout, {})
    if not isinstance(project, dict):
        print("%s holds no object for %s" % (path, checkout), file=sys.stderr)
        return 1
    project["hasTrustDialogAccepted"] = True

    if json.dumps(doc, sort_keys=True) == before:
        return 0

    # Same directory, so the rename cannot cross a filesystem.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claude.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    print("recorded onboarding, the remote-control dialog%s and trust for "
          "%s in %s" % (", the account record" if record is not None else "",
                        checkout, path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
