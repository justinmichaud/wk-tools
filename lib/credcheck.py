#!/usr/bin/env python3
"""Every credential wk holds, what it must be able to do, and what it must not (`credcheck.py -h`).

The value arrives on stdin, always; reading a stored one is lib/secretfile.py's discipline.
One verdict comes back, first line `<verdict>\\t<summary>`, further lines the detail:

    absent      nothing is stored -- a state, not a fault, when optional
    ok          it can do the job and reaches no further
    wide        it can do the job and reaches further than wk spends it; stored
    bad         it cannot do the job, is malformed, or carries a power wk refuses to hold; nothing is stored
    unverified  the answer needs a network that did not answer; stored, and re-asked by every reader -- no verdict is ever written down"""
import argparse
import collections
import http.client as http_client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from wk.machine import Local

OK, WIDE, BAD, UNVERIFIED, ABSENT = ("ok", "wide", "bad",
                                    "unverified", "absent")

def api_base(var, default):
    value = os.environ.get(var)
    if not value:
        return default
    parts = urllib.parse.urlsplit(value)
    if parts.scheme == "https" or parts.hostname in ("127.0.0.1", "localhost"):
        return value
    raise SystemExit(
        "%s=%s: a credential is sent to that address in an Authorization "
        "header, so it has to be https, or http on 127.0.0.1 or localhost "
        "(which is what the tests stand a stub up on). Unset %s to use %s."
        % (var, value, var, default))


GITHUB_API = api_base("WK_GITHUB_API", "https://api.github.com")
BUGZILLA_API = api_base("WK_BUGZILLA_API", "https://bugs.webkit.org")
BUGZILLA_KEYS = "https://bugs.webkit.org/userprefs.cgi?tab=apikey"
TIMEOUT = 20
PER_PAGE = 100

# `url` mints one with what a link can carry filled in, `remedy` is what is left to choose there.
Rule = collections.namedtuple(
    "Rule", "needs forbids what url remedy store_with check mint",
    defaults=(None,))

FIELDS = tuple(f for f in Rule._fields if f not in ("check", "mint"))


TAILSCALE_KEYS = "https://login.tailscale.com/admin/settings/keys"
LITELLM_API = api_base("WK_LITELLM_API", "https://ai.igalia.com")
LITELLM_KEYS = "https://ai.igalia.com/ui/api-keys/"
LITELLM_ENDPOINT = LITELLM_API + "/v1"  # `wk ai pi` -- pi's OpenAI-compatible endpoint for the key above


def fix_of(rule):
    return " -- ".join(x for x in (rule.url, rule.remedy) if x)


# The longest lifetime an organization's token policy allows; a token minted to never expire is refused every call to every repository the organization owns (measured against WebKit/WebKit, 2026-09-15).
MAX_PAT_DAYS = 365

# Classic: a fine-grained token reaches only its owner's repositories, so it opens a pull request on no upstream project.
CLASSIC_TOKEN_PAGE = (
    "https://github.com/settings/tokens/new?"
    + urllib.parse.urlencode([("scopes", "public_repo"),
                              ("description", "wk -- opens pull requests "
                                              "from a wk workspace")]))


class Unreachable(Exception):
    pass


GITHUB_HEADERS = (("Accept", "application/vnd.github+json"),)
ANTHROPIC_HEADERS = (("anthropic-version", "2023-06-01"),)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method, url, headers=(), body=None, timeout=TIMEOUT, follow=True):
    """(status, {header: value} lowercased, body); Unreachable when no HTTP answer came."""
    req = urllib.request.Request(url, method=method, data=body, headers={"User-Agent": "wk"})
    for name, value in headers:
        req.add_header(name, value)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(*(() if follow else (_NoRedirect,)))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, _lower(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers), e.read()
    except (OSError, http_client.HTTPException) as e:
        raise Unreachable("%s: %s" % (e.__class__.__name__, e))


def _http(method, url, token, body=None, headers=()):
    return http(method, url, ((("Authorization", "Bearer " + token),) if token else ()) + tuple(headers), body)


def _lower(headers):
    return dict((k.lower(), v) for k, v in headers.items())


def _json(raw):
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return doc if isinstance(doc, dict) else {}


# Powers no wk path spends; every other classic scope is merely broader than needed.
CLASSIC_REFUSED = ("delete_repo", "site_admin")


def _pat_kind(token):
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith("ghp_"):
        return "classic"
    if token[:4] in ("gho_", "ghu_", "ghs_", "ghr_"):
        return "other"
    return None


def _refused_scopes(scopes):
    return [s for s in scopes
            if s in CLASSIC_REFUSED or s.startswith("admin:")]


def _github_pat(value, repos, path, evidence):
    token = value.strip()
    if not token or len(token.split()) != 1:
        return BAD, "there is no token there, or it is not one line."
    kind = _pat_kind(token)
    if kind is None:
        return BAD, ("that does not start like a GitHub personal access token "
                     "('github_pat_' fine-grained, 'ghp_' classic).")
    if kind == "other":
        return BAD, ("that is an OAuth, app or refresh token, not a personal "
                     "access token: it belongs to whatever minted it and is "
                     "not something to store here.")
    try:
        status, headers, body = _http("GET", GITHUB_API + "/user", token,
                                      headers=GITHUB_HEADERS)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s), so what this token can do "
                            "is not known here; 'wk doctor' asks again."
                            % (GITHUB_API, e))
    if status in (401, 403):
        return BAD, ("GitHub does not accept this token (HTTP %d): it is "
                     "revoked, expired, or mistyped." % status)
    if status != 200:
        return UNVERIFIED, ("GET /user answered HTTP %d rather than 200 or 401, "
                            "so nothing about this token was established."
                            % status)
    try:
        login = (json.loads(body) or {}).get("login") or "?"
    except ValueError:
        login = "?"
    scopes = [s.strip() for s in headers.get("x-oauth-scopes", "").split(",")
              if s.strip()]
    expiry = headers.get("github-authentication-token-expiration", "")
    facts = ["as %s" % login]
    if expiry:
        facts.append("expires %s" % expiry)

    refused = _refused_scopes(scopes)
    if refused:
        return BAD, ("a classic token carrying %s, on every repository this "
                     "account can reach.\n    %s"
                     % (", ".join(refused), "; ".join(facts)))

    for repo in repos:
        verdict, why = _github_pr_verdict(token, kind, repo, False)
        if verdict != OK:
            return verdict, why
        facts.append("can open a pull request on %s" % repo)
    try:
        projects = _github_pr_bases(token, repos)
    except Unreachable as e:
        return UNVERIFIED, ("could not ask %s which project each fork belongs "
                            "to (%s); 'wk doctor' asks again." % (GITHUB_API, e))
    for project in projects:
        verdict, why = _github_pr_verdict(token, kind, project, True)
        if verdict != OK:
            return verdict, why
        facts.append("can open a pull request on %s" % project)
    if kind == "classic":
        reach = ("every repository this account can write, public and private"
                 if "repo" in scopes else
                 "every public repository this account can write")
        return WIDE, ("a classic token (scopes: %s): it reaches %s, not only "
                      "the forks -- which is the narrowest a token that can "
                      "open a pull request on the projects comes.\n    %s"
                      % (", ".join(scopes) or "none", reach, "; ".join(facts)))
    want = [r.lower() for r in repos]
    try:
        others = [r for r in _github_account_repos(token)
                  if r.lower() not in want]
        extra = [r for r in others if _pull_request_probe(token, r)[0] == 422]
    except Unreachable as e:
        return UNVERIFIED, ("could not ask %s which repositories this token "
                            "reaches (%s); 'wk doctor' asks again."
                            % (GITHUB_API, e))
    if extra:
        return BAD, ("this token reaches %d repositories beyond the %d forks wk "
                     "pushes to (%s).\n    %s"
                     % (len(extra), len(repos), _some(extra),
                        "; ".join(facts)))
    return OK, ("a fine-grained token: it can open a pull request on the %d "
                "forks and on none of the %d other repositories under this "
                "account.\n    %s" % (len(repos), len(others), "; ".join(facts)))


# `valid_login` answers false for another account's key and error 306 for one it does not know.
def _bugzilla_api_key(value, repos, path, evidence):
    key = value.strip()
    if not key or len(key.split()) != 1:
        return BAD, "there is no key there, or it is not one line."
    login = evidence.get("login", "")
    if not login:
        return UNVERIFIED, ("no Bugzilla login to check it against: the login is "
                            "the first email of this GitHub account's entry in "
                            "WebKit's metadata/contributors.json, read from the "
                            "mirror here ('wk sync' makes one).")
    url = BUGZILLA_API + "/rest/valid_login?" + urllib.parse.urlencode(
        {"login": login, "api_key": key})
    try:
        status, _headers, raw = _http("GET", url, None)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s), so whether it accepts this "
                            "key is not known here; 'wk doctor' asks again."
                            % (BUGZILLA_API, e))
    doc = _json(raw)
    if status == 400 and doc.get("code") == 306:
        return BAD, ("%s does not accept this key (error 306): revoked, mistyped "
                     "or never valid." % BUGZILLA_API)
    if status != 200:
        return UNVERIFIED, ("GET /rest/valid_login at %s answered HTTP %d rather "
                            "than 200 or 400, so nothing about this key was "
                            "established." % (BUGZILLA_API, status))
    if doc.get("result") is not True:
        return BAD, ("%s accepts this key, but not as %s: it belongs to another "
                     "account, and `git-webkit pr` would file and assign as that "
                     "one." % (BUGZILLA_API, login))
    # Bugzilla discloses no group membership to a caller without editusers, so editbugs is not knowable here.
    return OK, ("%s accepts it as %s; whether that account has editbugs is not "
                "knowable from here.\n    spent on every bugs.webkit.org request "
                "a workspace makes while push is on" % (BUGZILLA_API, login))


def _some(names, n=3):
    return ", ".join(sorted(names)[:n]) + (", ..." if len(names) > n else "")


def _github_account_repos(token):
    """`permissions` here is the account's, so what the token reaches is measured per repository by the probe."""
    names, page = [], 1
    while True:
        status, _headers, body = _http(
            "GET", "%s/user/repos?per_page=%d&page=%d" % (GITHUB_API, PER_PAGE,
                                                          page), token,
            headers=GITHUB_HEADERS)
        if status != 200:
            raise Unreachable("GET /user/repos answered HTTP %d" % status)
        try:
            batch = json.loads(body)
            names += [r["full_name"] for r in batch]
        except (ValueError, TypeError, KeyError) as e:
            raise Unreachable("GET /user/repos answered no repository list (%s)"
                              % e.__class__.__name__)
        if len(batch) < PER_PAGE:
            return names
        page += 1


def _pull_request_probe(token, repo):
    """An empty body names no branch, so an authorised call is 422 and an unauthorised one 403 (measured 2026-09-11)."""
    status, _headers, body = _http("POST", "%s/repos/%s/pulls"
                                   % (GITHUB_API, repo), token, body=b"{}",
                                   headers=GITHUB_HEADERS)
    return status, _json(body).get("message") or ""


def _github_pr_bases(token, repos):
    """`git-webkit pr` posts to each fork's `parent`, so that project has to accept the token too."""
    bases = []
    for repo in repos:
        status, _headers, body = _http("GET", "%s/repos/%s" % (GITHUB_API, repo),
                                       token, headers=GITHUB_HEADERS)
        if status != 200:
            raise Unreachable("GET /repos/%s answered HTTP %d" % (repo, status))
        parent = (_json(body).get("parent") or {}).get("full_name") or ""
        if parent and parent not in bases and parent not in repos:
            bases.append(parent)
    return bases


def _github_pr_verdict(token, kind, repo, project):
    """A project's two 403s differ only by GitHub's message: an organization's token policy, or a fine-grained token
    outside its resource owner."""
    try:
        status, message = _pull_request_probe(token, repo)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s) to ask whether a pull "
                            "request can be opened on %s." % (GITHUB_API, e, repo))
    if status == 422:
        return OK, ""
    if status == 403 and project:
        why = ("%s refuses this token a pull request (HTTP 403). GitHub says: %s"
               % (repo, message or "nothing at all."))
        if kind == "fine-grained":
            why += ("\n    A fine-grained token reaches only repositories owned "
                    "by the account that owns it, and %s belongs to another "
                    "organization, so no fine-grained token of this account can "
                    "open one there -- whatever it is granted on the forks. A "
                    "classic one is what can, and the link below mints it."
                    % repo)
        return BAD, why
    if status == 403:
        return BAD, ("GitHub refused it: no 'Pull requests: write' on %s, so "
                     "'git-webkit pr' in a workspace cannot open one (HTTP 403): "
                     "the token was not granted that repository, or was granted "
                     "it without that permission." % repo)
    if status == 404:
        return BAD, ("this token cannot see %s at all (HTTP 404): no repository "
                     "of that name is visible to it." % repo)
    if status == 401:
        return BAD, "GitHub does not accept this token (HTTP 401) for %s." % repo
    return UNVERIFIED, ("POST /repos/%s/pulls answered HTTP %d rather than 422 "
                        "or 403, so whether a pull request can be opened on %s "
                        "is not known." % (repo, status, repo))


ANTHROPIC_API = api_base("WK_ANTHROPIC_API", "https://api.anthropic.com")

# Measured 2026-09-10: an accepted `Bearer sk-ant-oat` token answers 200, a refused one 401, and no model is inferred.
def _claude_token_accepted(token):
    url = "%s/v1/models?limit=1" % ANTHROPIC_API
    try:
        status, _headers, _body = _http("GET", url, token,
                                        headers=ANTHROPIC_HEADERS)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s) to ask whether this token "
                            "is still accepted." % (ANTHROPIC_API, e))
    if status == 200:
        return OK, ""
    if status == 401:
        return BAD, ("Anthropic does not accept this token (HTTP 401): it is "
                     "spent, revoked or expired, and every workspace holding it "
                     "asks for /login. Replace it with `claude setup-token` and "
                     "'wk key set claude --replace'.")
    return UNVERIFIED, ("GET /v1/models answered HTTP %d rather than 200 or "
                        "401, so whether this token is accepted is not known."
                        % status)


def _claude_token(value, repos, path, evidence):
    token = value.strip()
    if token.startswith("{"):
        return BAD, ("that is a login document, not a token: `claude "
                     "setup-token` prints the one this row takes.")
    if token.startswith("sk-ant-api"):
        return BAD, ("that is an Anthropic Console API key: it bills the "
                     "organization, is not restricted to Claude Code, and every "
                     "workspace this machine makes would hold it.")
    if not token.startswith("sk-ant-oat"):
        return BAD, ("a `claude setup-token` credential starts 'sk-ant-oat'; "
                     "that does not.")
    verdict, why = _claude_token_accepted(token)
    if verdict != OK:
        return verdict, why
    return OK, ("a Claude Code OAuth token, which is inference-only: it cannot "
                "read the account or mint anything.\n    Anthropic accepts it "
                "(GET /v1/models, HTTP 200).")


def _litellm_key(value, repos, path, evidence):
    key = value.strip()
    if not key:
        return BAD, "there is nothing there."
    if key.startswith("sk-ant-"):
        return BAD, ("that is an Anthropic key, not a LiteLLM virtual key: it "
                     "reaches the upstream account directly, and this one is "
                     "handed to every workspace.")
    try:
        status, _headers, raw = _http("GET", LITELLM_ENDPOINT + "/models", key)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s) to ask whether it accepts "
                            "this key." % (LITELLM_API, e))
    if status == 401:
        return BAD, ("%s does not accept this key (HTTP 401): revoked, expired "
                     "or never valid, so `wk ai pi` answers 401 on its first "
                     "request." % LITELLM_API)
    if status != 200:
        return UNVERIFIED, ("GET /v1/models at %s answered HTTP %d rather than "
                            "200 or 401, so whether it accepts this key is not "
                            "known." % (LITELLM_API, status))
    served = len(_json(raw).get("data") or [])
    facts = ["%s accepts it and serves it %d model(s)" % (LITELLM_API, served)]
    # LiteLLM answers 403 on this management route for a key restricted to the LLM API routes.
    try:
        status, _headers, raw = _http("GET", LITELLM_API + "/key/info", key)
    except Unreachable as e:
        return UNVERIFIED, ("%s, then stopped answering (%s), so what else the "
                            "key reaches is not known." % (facts[0], e))
    if status == 200:
        info = _json(raw).get("info") or {}
        return WIDE, ("%s, and it reaches the key-management routes: /key/info "
                      "answers 200 (alias %s, expires %s, budget %s). A key "
                      "restricted to the LLM API routes cannot."
                      % (facts[0], info.get("key_alias") or "none",
                         info.get("expires") or "never",
                         info.get("max_budget") or "none"))
    if status != 403:
        return UNVERIFIED, ("%s; /key/info answered HTTP %d rather than 200 or "
                            "403, so what else the key reaches is not known."
                            % (facts[0], status))
    facts.append("restricted to the LLM API routes, so it cannot read or mint keys")
    return OK, ("%s.\n    `wk new` writes it into a workspace's "
                "~/.pi/agent/models.json for %s." % ("; ".join(facts),
                                                      LITELLM_ENDPOINT))


def chat_model_ids(doc):
    """/models also lists an embedding model and answers no mode."""
    return [m.get("model_name") for m in doc.get("data") or []
            if m.get("model_name") and (m.get("model_info") or {}).get("mode") in (None, "chat")]


def litellm_models(key):
    status, _headers, raw = _http("GET", LITELLM_ENDPOINT + "/model/info", key.strip())
    return chat_model_ids(_json(raw)) if status == 200 else []


def litellm_callable(key, ids):
    """The first of `ids` a one-token completion reaches, or None: the endpoint lists models that answer 404 (measured 2026-09-27)."""
    for model in ids:
        body = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode()
        status, _headers, _raw = _http("POST", LITELLM_ENDPOINT + "/chat/completions", key.strip(), body)
        if status == 200:
            return model
    return None


def _tailnet_authkey(value, repos, path, evidence):
    key = value.strip()
    if key.startswith("tskey-auth-") and len(key) > len("tskey-auth-"):
        return OK, (
            "an auth key, so it can enroll a node and nothing else.\n    Tagged "
            "tag:wk, reusable, NOT ephemeral and its expiry cannot be read from "
            "the key; check those at " + TAILSCALE_KEYS)
    return BAD, _tailscale_wrong(key, "an auth key",
                                 "tskey-auth-<id>-<secret>")


def _tailnet_api(value, repos, path, evidence):
    key = value.strip()
    if not (key.startswith("tskey-api-") and len(key) > len("tskey-api-")):
        return BAD, _tailscale_wrong(key, "an API access token",
                                     "tskey-api-<id>-<secret>")
    if not path:
        return OK, ("an API access token; whether the tailnet still accepts it "
                    "is asked as soon as it is stored.")
    probe = Local().run(["env", "WK_TS_API_SECRET_FILE=" + path, "PYTHONPATH=" + os.path.dirname(os.path.abspath(__file__)),
                         sys.executable, "-m", "wk.tailnet", "check"])
    detail = (probe.out + probe.err).strip().splitlines()
    detail = detail[-1] if detail else "no answer"
    if probe.rc == 0:
        return OK, ("the tailnet accepts it: %s.\n    It is never written to a "
                    "card -- it administers the whole tailnet." % detail)
    if probe.rc == 6:
        return UNVERIFIED, "could not ask the tailnet: %s" % detail
    return BAD, "the tailnet refused it: %s" % detail


def _tailscale_wrong(key, wanted, shape):
    if not key:
        return "there is nothing there."
    if key.startswith("tskey-api-"):
        return ("that is an API access token (tskey-api-...), which administers "
                "the whole tailnet, not %s." % wanted)
    if key.startswith("tskey-auth-"):
        return ("that is a node auth key (tskey-auth-...), which enrolls a "
                "node, not %s." % wanted)
    if key.startswith("tskey-client-") or key.startswith("tskey-oauth-"):
        return ("that is an OAuth client secret, which mints keys of both "
                "kinds, not %s." % wanted)
    if key.startswith("tskey-"):
        return "that starts 'tskey-' but is not one: they are '%s'." % shape
    return "that does not look like a tailscale key at all (they start 'tskey-')."


# The evidence is gathered where the key is (`wk key sshtest`); the verdict is reached here.
def _deploy_key(value, repos, path, evidence):
    repo = repos[0] if repos else "?"
    ssh = evidence.get("ssh", "")
    read_only = evidence.get("read_only", "")
    if not ssh or ssh.startswith("no key"):
        return BAD, "there is no key for %s on that machine." % repo
    if "Hi %s!" % repo not in ssh:
        if "Permission denied" in ssh:
            return BAD, ("GitHub does not know this key: it is not registered "
                         "on %s." % repo)
        if "successfully authenticated" in ssh:
            return BAD, ("this key authenticates, but as an account key rather "
                         "than a deploy key on %s -- it reaches every "
                         "repository that account can, which is the reach a "
                         "deploy key exists to avoid." % repo)
        return UNVERIFIED, ("ssh to github.com answered '%s', so nothing about "
                            "this key was established."
                            % ssh.strip().splitlines()[0][:120])
    if read_only == "false":
        return OK, "registered on %s with write access, and on no other." % repo
    if read_only == "true":
        return BAD, ("registered on %s READ-ONLY, so a push fails at the door."
                     % repo)
    return UNVERIFIED, ("registered on %s and scoped to it; write access is "
                        "unconfirmed because the key list could not be read."
                        % repo)


def _ntfy_mint():
    from wk import notify
    return notify.mint()


def _ntfy_topic(value, repos, path, evidence):
    from wk import notify
    code, detail = notify.check(value.strip())
    if code == 0:
        return OK, ("%s\n    It is never written to a card and no workspace "
                    "holds it: a notification a person acts on must not be "
                    "forgeable from inside one." % detail)
    if code == 3:
        return WIDE, detail
    if code == 6:
        return UNVERIFIED, "could not ask ntfy.sh: %s" % detail
    return BAD, detail


RULES = collections.OrderedDict((
    ("github-pat", Rule(
        needs="open a pull request from each fork wk pushes to, on the "
              "project it is a fork of",
        forbids="delete a repository, or administer a repository, an "
                "organization or the site",
        what="a GitHub personal access token, so `git-webkit pr` in a "
             "workspace can open a pull request",
        url=CLASSIC_TOKEN_PAGE,
        remedy=("the two fields that link cannot carry: tick nothing but the "
                "'public_repo' it preselects ('repo' adds every private "
                "repository this account can write), and set an expiry of at "
                "most %d days -- an organization refuses a token that outlives "
                "its policy" % MAX_PAT_DAYS),
        store_with="wk key set github-pat",
        check=_github_pat)),
    ("bugzilla-api-key", Rule(
        needs="be accepted by bugs.webkit.org as the login WebKit's "
              "metadata/contributors.json gives this GitHub account",
        forbids="rest where a workspace reads, or be spent while push is off: "
                "a Bugzilla key is the whole account",
        what="a bugs.webkit.org API key, so `git-webkit pr` in a workspace can "
             "file the bug and post the pull request to it",
        url=BUGZILLA_KEYS,
        remedy="'New API key' there, described as this machine; the key is "
               "shown once",
        store_with="wk key set bugzilla-api-key",
        check=_bugzilla_api_key)),
    ("claude", Rule(
        needs="authenticate Claude Code for inference",
        forbids="read the account, bill the organization, or mint further "
                "credentials",
        what="a Claude Code token, so a guest or a build box starts "
             "authenticated instead of asking for /login",
        url="",
        remedy="run `claude setup-token` here and paste what it prints",
        store_with="wk key set claude",
        check=_claude_token)),
    ("litellm", Rule(
        needs="reach your own LiteLLM endpoint",
        forbids="reach the upstream provider account directly",
        what="your LiteLLM API key, so `wk ai pi` in a workspace can reach "
             "that endpoint",
        url=LITELLM_KEYS,
        remedy="'+ Create New Key' there; the key is shown once",
        store_with="wk key set litellm",
        check=_litellm_key)),
    ("tailnet", Rule(
        needs="enroll a node on the tailnet",
        forbids="administer the tailnet or mint further keys",
        what="the fleet's tailnet auth key, so a card written here boots onto "
             "the tailnet under its own name",
        url=TAILSCALE_KEYS,
        remedy="Generate auth key: tagged tag:wk, Reusable on, Ephemeral OFF, "
               "longest expiry",
        store_with="wk key set tailnet",
        check=_tailnet_authkey)),
    ("tailnet-api", Rule(
        needs="list and delete devices on this tailnet",
        forbids="be written to a card or reach a workspace: it administers "
                "the whole tailnet",
        what="the tailnet API access token a workstation retires a stale fleet "
             "node with",
        url=TAILSCALE_KEYS,
        remedy="Generate access token: the tag:wk devices scope is enough",
        store_with="wk key set tailnet-api",
        check=_tailnet_api)),
    ("deploy-key", Rule(
        needs="push to exactly one fork",
        forbids="reach any other repository, or be read-only",
        what="an ed25519 key per fork, generated here and never pasted",
        url="",
        remedy="wk key deploy  (it registers with read_only=false)",
        store_with="wk key deploy",
        check=_deploy_key)),
    ("ntfy", Rule(
        needs="publish a notification a person sees",
        forbids="be a name someone could arrive at by guessing: the topic is "
                "the whole credential, so anyone holding it reads every "
                "notification and can send one",
        what="the ntfy.sh topic this machine's notifications go to, so the "
             "fleet can tell you it wants you",
        url="https://ntfy.sh/",
        remedy="subscribe ntfy's iOS or Android app to that topic URL",
        store_with="wk key set ntfy",
        check=_ntfy_topic,
        mint=_ntfy_mint)),
))


def check(name, repos, path, evidence):
    rule = RULES.get(name)
    if rule is None:
        sys.stderr.write("credcheck: no rule for '%s'; there are: %s\n"
                         % (name, " ".join(RULES)))
        return 2
    value = sys.stdin.read()
    if path and not value.strip():
        sys.stdout.write("%s\tnothing stored -- %s\n" % (ABSENT, rule.store_with))
        return 0
    verdict, detail = rule.check(value, repos, path, evidence)
    if verdict == BAD:
        detail = ("%s\n    it must %s, and must not %s\n    fix: %s -- then: %s%s"
                  % (detail, rule.needs, rule.forbids, fix_of(rule),
                     rule.store_with, " --replace" if path else ""))
    sys.stdout.write("%s\t%s\n" % (verdict, detail))
    return 0


def mint(name):
    r = RULES.get(name)
    if r is None or not r.mint:
        sys.stderr.write("credcheck: nothing here mints a '%s' credential; "
                         "wk mints: %s\n" % (name, " ".join(_minted())))
        return 2
    sys.stdout.write(r.mint() + "\n")
    return 0


def _minted():
    return [n for n, r in RULES.items() if r.mint]


def rule(name):
    r = RULES.get(name)
    if r is None:
        return 2
    for field in FIELDS:
        sys.stdout.write("%s\t%s\n" % (field, getattr(r, field)))
    sys.stdout.write("fix\t%s\n" % fix_of(r))
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="credcheck.py", description="The value arrives on stdin.")
    verbs = p.add_subparsers(dest="verb", required=True)
    verbs.add_parser("names", help="every credential wk holds")
    verbs.add_parser("minted", help="the ones wk mints itself")
    verbs.add_parser("mint", help="mint one").add_argument("name")
    verbs.add_parser("rule", help="the rule for <name>").add_argument("name")
    v = verbs.add_parser("check", help="the verdict on the value")
    v.add_argument("name")
    v.add_argument("--repos", default="", help="the forks, as '<owner/repo> ...'")
    v.add_argument("--path", default="", help="where it is kept: an empty value is then absent")
    v.add_argument("--evidence", action="append", default=[], metavar="KEY=VALUE")
    a = p.parse_args(argv)
    if a.verb in ("names", "minted"):
        sys.stdout.write("".join(n + "\n" for n in (RULES if a.verb == "names" else _minted())))
        return 0
    if a.verb == "mint":
        return mint(a.name)
    if a.verb == "rule":
        return rule(a.name)
    return check(a.name, a.repos.split(), a.path, dict(e.partition("=")[::2] for e in a.evidence))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
