#!/usr/bin/env python3
"""Every credential wk holds, what it must be able to do, and what it must not.

    credcheck.py names
    credcheck.py rule  <name>
    credcheck.py check <name> [--repos "<owner/repo> ..."] [--path <file>]
                              [--evidence <key>=<value>]...

The value arrives on stdin, always: reading a stored credential is one
discipline (lib/secretfile.py, which refuses a link or a shared inode) and
it lives in one place. --path names where that value is kept, for the one
rule that hands the file to another tool, and makes an empty value `absent`
rather than malformed. One verdict comes back, its
first line `<verdict>\\t<summary>` and any further lines the detail:

    absent      nothing is stored, which for an optional credential is a
                state and not a fault
    ok          it can do the job and reaches no further
    wide        it can do the job and reaches further than wk ever spends it;
                stored, with the extra reach named
    bad         it cannot do the job, is malformed, or carries a power wk
                refuses to hold; nothing is stored
    unverified  the answer needs a network that did not answer; stored, and
                re-asked by every reader -- no verdict is ever written down

Both directions are the point. A credential that cannot do the job fails hours
later at the one moment it is needed (a `git-webkit pr` answered 403), and one
that can do far more than the job turns any escape from the boundary into the
blast radius of the whole account. So each rule names what wk spends it on,
what it must be able to do, what it must not, and the exact page or command
that reissues it.

The rules are data because the same questions are asked in three places -- when
a credential is admitted (`wk key set`) and by the two commands that report on
one (`wk doctor`, `wk key check`) -- and a rule written three times is three
rules that drift.

What GitHub will and will not answer about a token bounds the github-pat rule.
It answers: whether the token is live and as whom (`GET /user`), a classic
token's whole scope list (the `x-oauth-scopes` response header), an expiry when
the token has one (`github-authentication-token-expiration`), and whether a
pull request can be opened on a given repository -- a `POST /repos/<r>/pulls`
with an empty body is 422 (validation failed, nothing created) when the token
carries the permission and 403 when it does not, which is the same write-shaped
probe `wk verify` runs from inside a workspace. It does not answer what a
fine-grained token's permission set is: there is no endpoint that enumerates
one, so a fine-grained token's breadth is established by construction (it
reaches only the repositories selected for it) rather than by asking.
"""
import collections
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

OK, WIDE, BAD, UNVERIFIED, ABSENT = ("ok", "wide", "bad",
                                    "unverified", "absent")

GITHUB_API = os.environ.get("WK_GITHUB_API", "https://api.github.com")
TIMEOUT = 20

Rule = collections.namedtuple(
    "Rule", "spent_by needs forbids remedy store_with check")


class Unreachable(Exception):
    pass


def _http(method, url, token, body=None):
    """(status, lower-cased headers, body). Raises Unreachable when nothing
    answered, which is a state and not a fault."""
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "wk-credcheck")
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


# --- github-pat ---------------------------------------------------------------
# Powers no wk code path spends and that a token reachable from the boundary
# must not carry: deleting a repository, or administering one, an org or the
# site. Every other classic scope is merely broader than needed.
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
        status, headers, body = _http("GET", GITHUB_API + "/user", token)
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
        return BAD, ("this is a classic token carrying %s -- powers no part of "
                     "wk ever spends, on every repository the account can "
                     "reach.\n    %s" % (", ".join(refused), "; ".join(facts)))

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
    return OK, ("a fine-grained token, so it reaches only the repositories "
                "selected for it.\n    %s" % "; ".join(facts))


def _github_pat_can_open_a_pr(token, repo):
    """The write-shaped probe that creates nothing: an empty body names no head
    or base branch, so an authorised call is 422 and an unauthorised one 403."""
    url = "%s/repos/%s/pulls" % (GITHUB_API, repo)
    try:
        status, _headers, _body = _http("POST", url, token, body=b"{}")
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


# --- the claude.ai login ------------------------------------------------------
# What an agent in a workspace spends this on: inference, and the profile fetch
# remote control decides eligibility from.
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


# --- the two pasted API keys ---------------------------------------------------
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
    return OK, ("a Claude Code OAuth token, which is inference-only: it cannot "
                "read the account or mint anything.\n    Whether Claude still "
                "accepts it is one request away and is not asked here; a "
                "workspace reporting 'Not logged in' is the evidence that it "
                "does not.")


def _litellm_key(value, repos, path, evidence):
    key = value.strip()
    if not key:
        return BAD, "there is nothing there."
    if key.startswith("sk-ant-"):
        return BAD, ("that is an Anthropic key, not a LiteLLM virtual key: it "
                     "reaches the upstream account directly, and this one is "
                     "handed to every workspace.")
    return OK, (
        "a LiteLLM virtual key.\n    Which endpoint it belongs to is not known "
        "here -- a workspace's own ~/.pi/agent/models.json names that -- so "
        "nothing was asked of it.")


# --- the two tailscale credentials ---------------------------------------------
# Tailscale spells three very different powers with one prefix: an auth key
# enrolls a node, an API access token administers the tailnet, an OAuth client
# secret mints both. The auth key is copied onto every card written from here.
def _tailnet_authkey(value, repos, path, evidence):
    key = value.strip()
    if key.startswith("tskey-auth-") and len(key) > len("tskey-auth-"):
        return OK, (
            "an auth key, so it can enroll a node and nothing else.\n    Tagged "
            "tag:wk, reusable, NOT ephemeral and its expiry cannot be read from "
            "the key; check those at "
            "https://login.tailscale.com/admin/settings/keys")
    return BAD, _tailscale_wrong(key, "an auth key",
                                 "an auth key can only enroll a node.",
                                 "tskey-auth-<id>-<secret>")


def _tailnet_api(value, repos, path, evidence):
    key = value.strip()
    if not (key.startswith("tskey-api-") and len(key) > len("tskey-api-")):
        return BAD, _tailscale_wrong(
            key, "an API access token",
            "retiring a node is an administrative act on the tailnet.",
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


def _tailscale_wrong(key, wanted, because, shape):
    if not key:
        return "there is nothing there."
    if key.startswith("tskey-api-"):
        return ("that is an API access token (tskey-api-...), not %s. It "
                "administers the whole tailnet -- it can add and delete "
                "devices, rewrite the ACLs and mint further keys -- and %s"
                % (wanted, because))
    if key.startswith("tskey-auth-"):
        return ("that is a node auth key (tskey-auth-...), not %s. An auth key "
                "enrolls a node and can do nothing else; %s"
                % (wanted, because))
    if key.startswith("tskey-client-") or key.startswith("tskey-oauth-"):
        return ("that is an OAuth client secret. It mints auth keys and API "
                "tokens of its own, so whatever holds it holds everything they "
                "can do.")
    if key.startswith("tskey-"):
        return "that starts 'tskey-' but is not one: they are '%s'." % shape
    return "that does not look like a tailscale key at all (they start 'tskey-')."


# --- a deploy key -------------------------------------------------------------
# Repo-scoped by construction: GitHub refuses the same key on a second
# repository, so the only questions left are whether it authenticates as this
# fork and whether it was registered with write access. The evidence is
# gathered where the key is (`wk key sshtest` runs on that machine) and the
# verdict is reached here, so both halves are one rule.
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


RULES = collections.OrderedDict((
    ("github-pat", Rule(
        spent_by="container/proxy/github-inject.py -- the Authorization header "
                 "a workspace's `git-webkit pr` request is forwarded with",
        needs="open a pull request on each fork wk pushes to",
        forbids="delete a repository, or administer a repository, an "
                "organization or the site",
        remedy="https://github.com/settings/personal-access-tokens/new -- a "
               "fine-grained token, Repository access: only the forks above, "
               "Permissions: Contents read and write, Pull requests read and "
               "write. Then: wk key set github-pat --replace",
        store_with="wk key set github-pat",
        check=_github_pat)),
    ("claude", Rule(
        spent_by="shell/bashrc -- exported as $CLAUDE_CODE_OAUTH_TOKEN into "
                 "every workspace this machine makes",
        needs="authenticate Claude Code for inference",
        forbids="read the account, bill the organization, or mint further "
                "credentials",
        remedy="run `claude setup-token` here and paste what it prints",
        store_with="wk key set claude",
        check=_claude_token)),
    ("litellm", Rule(
        spent_by="shell/bashrc -- exported as $LITELLM_API_KEY, which "
                 "`wk ai pi` reaches your endpoint with",
        needs="reach your own LiteLLM endpoint",
        forbids="reach the upstream provider account directly",
        remedy="a virtual key from your LiteLLM deployment (its web UI, or "
               "POST /key/generate)",
        store_with="wk key set litellm",
        check=_litellm_key)),
    ("claude-login", Rule(
        spent_by="cmd/ai -- what `wk ai claude <ws> --rc` refuses to start "
                 "remote control without",
        needs="run inference and fetch the account profile (user:inference, "
              "user:profile), and still be renewable",
        forbids="be an inference-only setup token, which cannot fetch a profile",
        remedy="run `claude auth login` here, then: wk key set claude-login "
               "--replace",
        store_with="wk key set claude-login",
        check=_claude_login)),
    ("tailnet", Rule(
        spent_by="cmd/sysimage -- seeded onto every card written from here",
        needs="enroll a node on the tailnet",
        forbids="administer the tailnet or mint further keys",
        remedy="https://login.tailscale.com/admin/settings/keys -- tagged "
               "tag:wk, reusable, NOT ephemeral, longest expiry",
        store_with="wk key tailnet",
        check=_tailnet_authkey)),
    ("tailnet-api", Rule(
        spent_by="lib/tailnet.py -- retiring the offline fleet node whose name "
                 "a new card needs",
        needs="list and delete devices on this tailnet",
        forbids="leave this machine: it is never written to a card",
        remedy="https://login.tailscale.com/admin/settings/keys -- an access "
               "token, tag:wk devices scope is enough",
        store_with="wk key tailnet-api",
        check=_tailnet_api)),
    ("deploy-key", Rule(
        spent_by="lib/store.sh push_agent_load -- loaded into the ssh-agent a "
                 "workspace reaches while `wk push` is on",
        needs="push to exactly one fork",
        forbids="reach any other repository, or be read-only",
        remedy="wk key register  (it registers with read_only=false)",
        store_with="wk key register",
        check=_deploy_key)),
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
        detail = ("%s\n    it must be able to: %s\n    it must not be able to: "
                  "%s\n    fix: %s\n    then: %s"
                  % (detail, rule.needs, rule.forbids, rule.remedy,
                     rule.store_with))
    sys.stdout.write("%s\t%s\n" % (verdict, detail))
    return 0


def rule(name):
    r = RULES.get(name)
    if r is None:
        return 2
    for field in r._fields:
        if field != "check":
            sys.stdout.write("%s\t%s\n" % (field, getattr(r, field)))
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "names":
        sys.stdout.write("".join(n + "\n" for n in RULES))
        return 0
    if len(argv) == 3 and argv[1] == "rule":
        return rule(argv[2])
    if len(argv) >= 3 and argv[1] == "check":
        name, repos, path, evidence = argv[2], [], "", {}
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
        return check(name, repos, path, evidence)
    sys.stderr.write(__doc__.split("\n\n")[1] + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
