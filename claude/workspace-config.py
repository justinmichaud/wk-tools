#!/usr/bin/env python3
"""workspace-config.py <checkout>: records in ~/.claude.json the onboarding, remote-control-dialog
and trust answers `wk` already gave, plus the account record beside the shared login (cmd/ai says why each is needed)."""
import json
import os
import sys
import tempfile


def account_record():  # (record, None) | (None, why) | (None, None) with no shared credential mounted
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
