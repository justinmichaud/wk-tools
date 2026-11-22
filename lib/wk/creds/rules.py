"""Every credential wk stores: its name, what it is for, a check of its shape, and a live probe that writes nothing.

    rule = rules.find("github-token")
    rule.check(value)           # None, or why the value is not one
    rule.probe(value, ctx)      # (OK | BAD | UNKNOWN, one line); UNKNOWN is an issuer that did not answer

`ctx` is a Context: the store's other values, the time, and the client's litellm_url."""

import base64
import dataclasses
import json
import re
import struct
import urllib.parse
from typing import Callable, Optional

from wk.core.act import die
from wk.creds import injector, net
from wk.webkit import PROJECT

OK, BAD, UNKNOWN = "ok", "bad", "unknown"

GITHUB_API = "https://api.github.com"
UPSTREAM = PROJECT["upstream"][len("https://github.com/"):-len(".git")]
BUGZILLA = PROJECT["bugzilla"]
ANTHROPIC_API = "https://api.anthropic.com"
DEPLOY = "deploy-key/"
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
WORD = re.compile(r"[A-Za-z0-9_.-]+")
KEY_MAGIC = b"openssh-key-v1\0"


@dataclasses.dataclass(frozen=True)
class Context:
    get: Callable
    now: float
    litellm: Optional[str]


@dataclasses.dataclass(frozen=True)
class Rule:
    name: str
    what: str
    check: Callable
    probe: Callable


def ask(method, url, token=None, body=None, headers=None):
    """(status, the answer as a dict), or (None, why) when nothing answered."""
    try:
        status, raw = net.request(method, url, dict(headers or {}, **({"Authorization": "Bearer " + token} if token else {})), body)
    except net.NoAnswer as e:
        return None, {"message": str(e)}
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = {}
    return status, doc


def unanswered(what, status, doc):
    return UNKNOWN, "%s %s" % (what, "answered HTTP %d" % status if status else "did not answer (%s)" % doc["message"])


def one_word(value):
    return None if WORD.fullmatch(value) else "it is not one word of letters, digits, `_`, `.` and `-`"


def github_user_probe(value, ctx):
    status, doc = ask("GET", "%s/users/%s" % (GITHUB_API, value))
    if status == 200:
        return OK, "GitHub knows %s" % value
    if status == 404:
        return BAD, "GitHub knows no account named %s" % value
    return unanswered("GET /users/%s" % value, status, doc)


def github_token_check(value):
    if value.startswith("github_pat_"):
        return "it is a fine-grained token, which reaches only its own account's repositories and so opens no pull request on %s: make a classic one" % UPSTREAM
    return None if value.startswith("ghp_") and WORD.fullmatch(value) else "a classic GitHub personal access token starts `ghp_`"


def github_token_probe(value, ctx):
    status, doc = ask("GET", GITHUB_API + "/user", value)
    if status == 401:
        return BAD, "GitHub does not accept this token (HTTP 401): revoked, expired or mistyped"
    if status != 200:
        return unanswered("GET /user", status, doc)
    user = ctx.get("github-user")
    if user and user != doc.get("login"):
        return BAD, "it is %s's token, and github-user is %s" % (doc.get("login"), user)
    # An empty body names no branch, so an authorised call is 422 and an unauthorised one 403: nothing is created.
    status, pulls = ask("POST", "%s/repos/%s/pulls" % (GITHUB_API, UPSTREAM), value, b"{}")
    if status == 422:
        return OK, "as %s, and it can open a pull request on %s" % (doc.get("login"), UPSTREAM)
    if status == 403:
        return BAD, "%s refuses this token a pull request (HTTP 403): a classic token needs the public_repo scope" % UPSTREAM
    return unanswered("POST /repos/%s/pulls" % UPSTREAM, status, pulls)


def bugzilla_user_check(value):
    return None if re.fullmatch(r"[^@\s]+@[^@\s]+", value) else "a Bugzilla login is an email address"


def bugzilla_password_check(value):
    return None if value and len(value.split()) == 1 else "it is not one word"


def bugzilla_probe(user, password):
    """The request git-webkit itself validates a login with (webkitbugspy/bugzilla.py)."""
    if not user or not password:
        return UNKNOWN, "it is checked with its other half: `wk key set %s`" % ("bugzilla-password" if user else "bugzilla-user")
    login = urllib.parse.quote(user)
    status, doc = ask("GET", "%s/rest/user/%s?login=%s&password=%s" % (BUGZILLA, login, login, urllib.parse.quote(password)))
    if status == 200:
        return OK, "%s accepts it as %s" % (BUGZILLA, user)
    if status == 401:
        return BAD, "%s does not accept this login and password together (HTTP 401)" % BUGZILLA
    return unanswered("GET /rest/user", status, doc)


def litellm_key_check(value):
    if value.startswith("sk-ant-"):
        return "it is an Anthropic key, which reaches the upstream account directly"
    return one_word(value)


def litellm_models(key, url):
    """(status, the answer) of the endpoint's model list."""
    return ask("GET", url.rstrip("/") + "/models", key)


def litellm_key_probe(value, ctx):
    if not ctx.litellm:
        return UNKNOWN, "no endpoint to ask: set litellm_url= in the client's machines/<name>.conf"
    status, doc = litellm_models(value, ctx.litellm)
    if status == 200:
        return OK, "%s accepts it and serves it %d model(s)" % (ctx.litellm, len(doc.get("data") or []))
    if status == 401:
        return BAD, "%s does not accept this key (HTTP 401)" % ctx.litellm
    return unanswered("GET %s/models" % ctx.litellm, status, doc)


def claude_login_check(value):
    try:
        injector.parse(value)
    except ValueError as e:
        return str(e)


def claude_login_probe(value, ctx):
    oauth = injector.parse(value)
    if oauth["expiresAt"] <= ctx.now * 1000:
        return UNKNOWN, "the stored copy's access token has expired; the injector holds the refreshed one, and `wk doctor` asks it"
    # An accepted bearer answers 200 and a refused one 401; no model is run.
    status, doc = ask("GET", ANTHROPIC_API + "/v1/models?limit=1", oauth["accessToken"], headers={"anthropic-version": "2023-06-01"})
    if status == 200:
        return OK, "Anthropic accepts it; only the injector holds it"
    if status == 401:
        return BAD, "Anthropic does not accept it (HTTP 401): the login was revoked, or another holder refreshed it and spent this one"
    return unanswered("GET /v1/models", status, doc)


def tailnet_key_check(value):
    return None if value.startswith("tskey-auth-") and WORD.fullmatch(value) else "a tailnet auth key starts `tskey-auth-`"


def tailnet_key_probe(value, ctx):
    return UNKNOWN, "Tailscale has no probe that does not enroll a node: its tag, reuse and expiry are at https://login.tailscale.com/admin/settings/keys"


def public_key(private):
    """(cipher, "type base64") of an OpenSSH private key; ValueError when it is not one."""
    lines = private.strip().splitlines()
    if len(lines) < 3 or lines[0] != "-----BEGIN OPENSSH PRIVATE KEY-----" or lines[-1] != "-----END OPENSSH PRIVATE KEY-----":
        raise ValueError("it is not an OpenSSH private key (`ssh-keygen -t ed25519 -N '' -f <file>` makes one)")
    try:
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
        if not blob.startswith(KEY_MAGIC):
            raise ValueError
        fields, at = [], len(KEY_MAGIC)
        for field in range(5):      # cipher, kdf, kdf options, the key count (a bare uint32), the public key
            size = 0 if field == 3 else struct.unpack_from(">I", blob, at)[0]
            fields.append(blob[at + 4:at + 4 + size])
            at += 4 + size
        kind = fields[4][4:4 + struct.unpack_from(">I", fields[4])[0]]
    except (ValueError, struct.error):
        raise ValueError("its OpenSSH private key does not parse")
    return fields[0].decode(), "%s %s" % (kind.decode(), base64.b64encode(fields[4]).decode())


def deploy_key_check(value):
    try:
        cipher, _ = public_key(value)
    except ValueError as e:
        return str(e)
    return None if cipher == "none" else "it has a passphrase, and nothing on a server can type one: make it with -N ''"


def deploy_key_probe(repo):
    def probe(value, ctx):
        token = ctx.get("github-token")
        if not token:
            return UNKNOWN, "GitHub is asked with github-token: `wk key set github-token`"
        status, doc = ask("GET", "%s/repos/%s/keys?per_page=100" % (GITHUB_API, repo), token)
        if status != 200:
            return unanswered("GET /repos/%s/keys" % repo, status, doc if isinstance(doc, dict) else {})
        public = public_key(value)[1]
        found = [k for k in doc if " ".join(k.get("key", "").split()[:2]) == public]
        if not found:
            return BAD, "it is not a deploy key of %s: add `%s` at https://github.com/%s/settings/keys/new, allowing write access" % (repo, public, repo)
        if found[0].get("read_only"):
            return BAD, "it is registered on %s read-only, so a push is refused: re-add it allowing write access" % repo
        return OK, "registered on %s with write access" % repo
    return probe


RULES = (
    Rule("github-user", "your GitHub account name, which git-webkit sends with the token", one_word, github_user_probe),
    Rule("github-token", "a classic GitHub personal access token (scope public_repo), for git-webkit and gh", github_token_check, github_token_probe),
    Rule("bugzilla-user", "your %s login, an email address" % BUGZILLA, bugzilla_user_check,
         lambda value, ctx: bugzilla_probe(value, ctx.get("bugzilla-password"))),
    Rule("bugzilla-password", "your %s password, which git-webkit sends with bugzilla-user" % BUGZILLA, bugzilla_password_check,
         lambda value, ctx: bugzilla_probe(ctx.get("bugzilla-user"), value)),
    Rule("litellm-key", "pi's model key for the client's litellm_url; it is the one key an agent can read", litellm_key_check, litellm_key_probe),
    Rule("claude-login", "the claude.ai login document (.credentials.json) of a `claude` logged in for this purpose alone; "
         "only the injector holds it", claude_login_check, claude_login_probe),
    Rule("tailnet-auth-key", "the auth key a new server joins the tailnet with", tailnet_key_check, tailnet_key_probe),
)
DEPLOY_WHAT = "the ssh key that pushes to one repository: a private key with no passphrase, its public half a deploy key there"


def find(name):
    """The Rule for a credential's name; refused when wk stores no such credential."""
    for rule in RULES:
        if rule.name == name:
            return rule
    if name.startswith(DEPLOY) and REPO.fullmatch(name[len(DEPLOY):]):
        return Rule(name, DEPLOY_WHAT, deploy_key_check, deploy_key_probe(name[len(DEPLOY):]))
    die("no credential is named %s: the names are %s and %s<owner>/<repo>" % (name, ", ".join(r.name for r in RULES), DEPLOY))


def repos(names):
    """The repositories that have a deploy key among the store's `names`."""
    return [name[len(DEPLOY):] for name in names if name.startswith(DEPLOY) and REPO.fullmatch(name[len(DEPLOY):])]
