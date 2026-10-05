#!/usr/bin/env python3
"""workspace-config.py <checkout>: records onboarding, the remote-control dialog and trust in ~/.claude.json."""
import json
import os
import sys
import tempfile


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

    before = json.dumps(doc, sort_keys=True)
    doc["hasCompletedOnboarding"] = True
    doc["remoteDialogSeen"] = True
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
    print("recorded onboarding, the remote-control dialog and trust for "
          "%s in %s" % (checkout, path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
