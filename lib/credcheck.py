#!/usr/bin/env python3
"""Every credential wk holds, what it must be able to do, and what it must not.

    credcheck.py names
    credcheck.py minted
    credcheck.py mint  <name>
    credcheck.py rule  <name> [--repos "<owner/repo> ..."]
    credcheck.py check <name> [--repos "<owner/repo> ..."] [--path <file>]
                              [--evidence <key>=<value>]...

The value arrives on stdin, always; reading a stored one is lib/secretfile.py's discipline. --path names where it is kept, for the rule that hands the file to another tool, and makes an empty value `absent` rather than malformed.
One verdict comes back, first line `<verdict>\\t<summary>`, further lines the detail:

    absent      nothing is stored -- a state, not a fault, when optional
    ok          it can do the job and reaches no further
    wide        it can do the job and reaches further than wk spends it; stored
    bad         it cannot do the job, is malformed, or carries a power wk refuses to hold; nothing is stored
    unverified  the answer needs a network that did not answer; stored, and re-asked by every reader -- no verdict is ever written down"""
import collections
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OK, WIDE, BAD, UNVERIFIED, ABSENT = ("ok", "wide", "bad",
                                    "unverified", "absent")

def _api_base(var, default):
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


GITHUB_API = _api_base("WK_GITHUB_API", "https://api.github.com")
TIMEOUT = 20
PER_PAGE = 100

# `url` is the page that mints one with everything a link can carry already filled in, `remedy` what is left to choose there; either may be a function of the fork list.
Rule = collections.namedtuple(
    "Rule", "spent_by needs forbids what url remedy store_with check mint",
    defaults=(None,))

FIELDS = tuple(f for f in Rule._fields if f not in ("check", "mint"))

WKNOTIFY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "wknotify.py")

TAILSCALE_KEYS = "https://login.tailscale.com/admin/settings/keys"
LITELLM_KEYS = "https://ai.igalia.com/ui/api-keys/"
LITELLM_ENDPOINT = "https://ai.igalia.com/v1"  # `wk ai pi` -- pi's OpenAI-compatible endpoint for the key above


def _resolved(value, repos):
    return value(repos) if callable(value) else value


def fix_of(rule, repos):
    return " -- ".join(x for x in (_resolved(rule.url, repos),
                                   _resolved(rule.remedy, repos)) if x)


# GitHub takes the name, the expiry and every permission as a query parameter, and the repository list as none.
def _github_pat_url(repos):
    q = [("name", "wk"),
         ("description", "opens pull requests from a wk workspace")]
    if repos:
        q.append(("target_name", repos[0].split("/")[0]))
    q += [("expires_in", "none"), ("contents", "write"),
          ("pull_requests", "write")]
    return ("https://github.com/settings/personal-access-tokens/new?"
            + urllib.parse.urlencode(q))


class Unreachable(Exception):
    pass


GITHUB_HEADERS = (("Accept", "application/vnd.github+json"),)
ANTHROPIC_HEADERS = (("anthropic-version", "2023-06-01"),)


def _http(method, url, token, body=None, headers=()):
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "wk-credcheck")
    for name, value in headers:
        req.add_header(name, value)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, _lower(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower(e.headers), e.read()
    except Exception as e:
        raise Unreachable("%s: %s" % (e.__class__.__name__, e))


def _lower(headers):
    return dict((k.lower(), v) for k, v in headers.items())


# Powers no wk code path spends and a token reachable from the boundary must not carry; every other classic scope is merely broader than needed.
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
        verdict, why = _github_pat_can_open_a_pr(token, repo)
        if verdict != OK:
            return verdict, why
        facts.append("can open a pull request on %s" % repo)
    if kind == "classic":
        return WIDE, ("a classic token (scopes: %s): its 'repo' scope reaches "
                      "every repository this account can write, not only the "
                      "forks.\n    %s"
                      % (", ".join(scopes) or "none", "; ".join(facts)))
    try:
        reached = _github_pat_reaches(token)
    except Unreachable as e:
        return UNVERIFIED, ("could not ask %s which repositories this token "
                            "reaches (%s); 'wk doctor' asks again."
                            % (GITHUB_API, e))
    want = [r.lower() for r in repos]
    got = [r.lower() for r in reached]
    missing = [r for r in repos if r.lower() not in got]
    extra = [r for r in reached if r.lower() not in want]
    if missing:
        return BAD, ("this token does not reach %s.\n    %s"
                     % (", ".join(missing), "; ".join(facts)))
    if extra:
        return BAD, ("this token reaches %d repositories beyond the %d forks wk "
                     "pushes to (%s).\n    %s"
                     % (len(extra), len(repos), _some(extra),
                        "; ".join(facts)))
    return OK, ("a fine-grained token, and GitHub lists exactly the %d forks "
                "under it.\n    %s" % (len(repos), "; ".join(facts)))


def _some(names, n=3):
    return ", ".join(sorted(names)[:n]) + (", ..." if len(names) > n else "")


def _github_pat_reaches(token):
    """Every repository the token reaches: GET /user/repos answers for the token rather than the account, so a fine-grained one lists what it was granted and one on 'All repositories' lists them all."""
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


def _github_pat_can_open_a_pr(token, repo):
    """The write-shaped probe that creates nothing: an empty body names no head or base branch, so an authorised call is 422 and an unauthorised one 403."""
    url = "%s/repos/%s/pulls" % (GITHUB_API, repo)
    try:
        status, _headers, _body = _http("POST", url, token, body=b"{}",
                                        headers=GITHUB_HEADERS)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s) to ask whether a pull "
                            "request can be opened on %s." % (GITHUB_API, e, repo))
    if status == 422:
        return OK, ""
    if status == 403:
        return BAD, ("GitHub refused it: no 'Pull requests: write' on %s, so "
                     "'git-webkit pr' in a workspace cannot open one (HTTP 403, "
                     "the failure measured 2026-09-04)." % repo)
    if status == 404:
        return BAD, ("this token cannot see %s at all (HTTP 404): a "
                     "fine-grained token reaches only the repositories selected "
                     "for it, and that fork is not one of them." % repo)
    if status == 401:
        return BAD, "GitHub does not accept this token (HTTP 401) for %s." % repo
    return UNVERIFIED, ("POST /repos/%s/pulls answered HTTP %d rather than 422 "
                        "or 403, so whether a pull request can be opened is "
                        "not known." % (repo, status))


LOGIN_SCOPES = ("user:profile", "user:inference")


def _claude_login(value, repos, path, evidence):
    try:
        doc = json.loads(value)
    except Exception as e:
        return BAD, "that is not JSON at all (%s)." % e.__class__.__name__
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    if not isinstance(oauth, dict):
        return BAD, "no claudeAiOauth object -- not a claude.ai login credential."
    if not oauth.get("accessToken"):
        return BAD, "no accessToken."
    if not oauth.get("refreshToken"):
        return BAD, ("no refreshToken -- that is a `claude setup-token` "
                     "credential, which is inference-only and cannot refresh "
                     "the profile remote control reads.")
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list):
        return BAD, "no scopes list."
    missing = [s for s in LOGIN_SCOPES if s not in scopes]
    if missing:
        return BAD, ("the login is missing %s; it carries %s."
                     % (", ".join(missing), " ".join(str(s) for s in scopes)))
    now = time.time() * 1000
    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_expiry, (int, float)) and refresh_expiry < now:
        return BAD, ("the refresh token expired %s, so this login cannot be "
                     "renewed and no workspace can use it."
                     % _when(refresh_expiry))
    facts = ["scopes: %s" % " ".join(str(s) for s in scopes)]
    subscription = oauth.get("subscriptionType")
    if subscription:
        facts.append("subscription: %s" % subscription)
    if isinstance(refresh_expiry, (int, float)):
        facts.append("renewable until %s" % _when(refresh_expiry))
    access_expiry = oauth.get("expiresAt")
    if isinstance(access_expiry, (int, float)) and access_expiry < now:
        facts.append("the access token has expired and the CLI will refresh it")
    return OK, (
        "%s.\n    Remote Control eligibility is the server's answer, not a "
        "field in here: this document carries no organization, and the CLI "
        "fetches one with user:profile at start-up. 'wk ai claude <ws> --rc' "
        "surviving is the evidence." % "; ".join(facts))


def _when(millis):
    return time.strftime("%Y-%m-%d", time.localtime(millis / 1000.0))


ANTHROPIC_API = _api_base("WK_ANTHROPIC_API", "https://api.anthropic.com")

# The read-only request that answers whether Anthropic still accepts a token,
# measured 2026-09-10 against api.anthropic.com: a `Bearer sk-ant-oat` token
# with the version header answers 200, one Anthropic refuses answers 401
# (whether or not the header is there), and no model is inferred either way.
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
        return BAD, ("that is a login document, not a token: 'wk key set "
                     "claude-login' is the row that takes one of those.")
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
    return OK, (
        "a LiteLLM virtual key for " + LITELLM_ENDPOINT + ", which is what\n"
        "    `wk ai pi` writes into a workspace's ~/.pi/agent/models.json. "
        "Nothing was asked of\n    that endpoint, so whether the key is live "
        "there is unmeasured.")


# Tailscale spells three very different powers with one prefix: an auth key enrolls a node, an API access token administers the tailnet, an OAuth client secret mints both.
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
    probe = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "tailnet.py"), "check"],
        env=dict(os.environ, WK_TS_API_SECRET_FILE=path),
        capture_output=True, text=True)
    detail = (probe.stdout + probe.stderr).strip().splitlines()
    detail = detail[-1] if detail else "no answer"
    if probe.returncode == 0:
        return OK, ("the tailnet accepts it: %s.\n    It is never written to a "
                    "card -- it administers the whole tailnet." % detail)
    if probe.returncode == 6:
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


# Repo-scoped by construction: GitHub refuses the same key on a second repository. The evidence is gathered where the key is (`wk key sshtest`) and the verdict reached here, so both halves are one rule.
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
    return subprocess.run([sys.executable, WKNOTIFY, "mint"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _ntfy_topic(value, repos, path, evidence):
    topic = value.strip()
    if not topic:
        return BAD, "there is nothing there."
    probe = subprocess.run([sys.executable, WKNOTIFY, "check"],
                           input=topic, capture_output=True, text=True)
    detail = (probe.stdout + probe.stderr).strip().splitlines()
    detail = detail[-1] if detail else "no answer"
    if probe.returncode == 0:
        return OK, ("%s\n    It is never written to a card and no workspace "
                    "holds it: a notification a person acts on must not be "
                    "forgeable from inside one." % detail)
    if probe.returncode == 3:
        return WIDE, detail
    if probe.returncode == 6:
        return UNVERIFIED, "could not ask ntfy.sh: %s" % detail
    return BAD, detail


RULES = collections.OrderedDict((
    ("github-pat", Rule(
        spent_by="container/proxy/github-inject.py -- the Authorization header "
                 "a workspace's `git-webkit pr` request is forwarded with",
        needs="open a pull request on each fork wk pushes to",
        forbids="delete a repository, or administer a repository, an "
                "organization or the site",
        what="a GitHub personal access token, so `git-webkit pr` in a "
             "workspace can open a pull request",
        url=_github_pat_url,
        remedy=lambda repos: (
            "the one field that link cannot carry: choose 'Only select "
            "repositories' and pick exactly %s"
            % (", ".join(repos) or "the forks wk pushes to")),
        store_with="wk key set github-pat",
        check=_github_pat)),
    ("claude", Rule(
        spent_by="shell/bashrc -- exported as $CLAUDE_CODE_OAUTH_TOKEN in a "
                 "macOS guest and on a build box, the two kinds of target the "
                 "delivery column sends it to",
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
        spent_by="shell/bashrc -- exported as $LITELLM_API_KEY, which "
                 "`wk ai pi` reaches your endpoint with",
        needs="reach your own LiteLLM endpoint",
        forbids="reach the upstream provider account directly",
        what="your LiteLLM API key, so `wk ai pi` in a workspace can reach "
             "that endpoint",
        url=LITELLM_KEYS,
        remedy="'+ Create New Key' there; the key is shown once",
        store_with="wk key set litellm",
        check=_litellm_key)),
    ("claude-login", Rule(
        spent_by="targets/container.sh -- mounted into every container as the "
                 "one Claude credential it is given, and what `wk ai claude "
                 "<ws> --rc` refuses to start remote control without",
        needs="run inference and fetch the account profile (user:inference, "
              "user:profile), and still be renewable",
        forbids="be an inference-only setup token, which cannot fetch a profile",
        what="your claude.ai login credential, so a container authenticates "
             "and remote control works in it",
        url="",
        remedy="it is `claude auth login` in a browser, run for the directory "
               "the containers share rather than for this machine; nothing is "
               "pasted",
        store_with="wk key set claude-login",
        check=_claude_login)),
    ("tailnet", Rule(
        spent_by="cmd/sysimage -- seeded onto every card written from here",
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
        spent_by="lib/tailnet.py -- retiring the offline fleet node whose name "
                 "a new card needs",
        needs="list and delete devices on this tailnet",
        forbids="leave this machine: it is never written to a card",
        what="the tailnet API access token this machine retires a stale fleet "
             "node with",
        url=TAILSCALE_KEYS,
        remedy="Generate access token: the tag:wk devices scope is enough",
        store_with="wk key set tailnet-api",
        check=_tailnet_api)),
    ("deploy-key", Rule(
        spent_by="lib/store.sh push_agent_load -- loaded into the ssh-agent a "
                 "workspace reaches while `wk push` is on",
        needs="push to exactly one fork",
        forbids="reach any other repository, or be read-only",
        what="an ed25519 key per fork, generated here and never pasted",
        url="",
        remedy="wk key deploy  (it registers with read_only=false)",
        store_with="wk key deploy",
        check=_deploy_key)),
    ("ntfy", Rule(
        spent_by="lib/wknotify.py -- the topic `wk notify` publishes a "
                 "headline to",
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
        detail = ("%s\n    it must %s, and must not %s\n    fix: %s -- then: %s"
                  % (detail, rule.needs, rule.forbids, fix_of(rule, repos),
                     rule.store_with))
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


def rule(name, repos):
    r = RULES.get(name)
    if r is None:
        return 2
    for field in FIELDS:
        sys.stdout.write("%s\t%s\n" % (field, _resolved(getattr(r, field),
                                                         repos)))
    sys.stdout.write("fix\t%s\n" % fix_of(r, repos))
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "names":
        sys.stdout.write("".join(n + "\n" for n in RULES))
        return 0
    if len(argv) == 2 and argv[1] == "minted":
        sys.stdout.write("".join(n + "\n" for n in _minted()))
        return 0
    if len(argv) == 3 and argv[1] == "mint":
        return mint(argv[2])
    if len(argv) >= 3 and argv[1] in ("rule", "check"):
        verb, name, repos, path, evidence = argv[1], argv[2], [], "", {}
        rest = argv[3:]
        while rest:
            flag, rest = rest[0], rest[1:]
            if not rest:
                sys.stderr.write("credcheck: %s takes a value\n" % flag)
                return 2
            value, rest = rest[0], rest[1:]
            if flag == "--repos":
                repos = value.split()
            elif flag == "--path":
                path = value
            elif flag == "--evidence":
                k, _, v = value.partition("=")
                evidence[k] = v
            else:
                sys.stderr.write("credcheck: unknown option %s\n" % flag)
                return 2
        if verb == "rule":
            return rule(name, repos)
        return check(name, repos, path, evidence)
    sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
