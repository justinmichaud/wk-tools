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
import tempfile
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
BUGZILLA_API = _api_base("WK_BUGZILLA_API", "https://bugs.webkit.org")
BUGZILLA_KEYS = "https://bugs.webkit.org/userprefs.cgi?tab=apikey"
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
LITELLM_API = _api_base("WK_LITELLM_API", "https://ai.igalia.com")
LITELLM_KEYS = "https://ai.igalia.com/ui/api-keys/"
LITELLM_ENDPOINT = LITELLM_API + "/v1"  # `wk ai pi` -- pi's OpenAI-compatible endpoint for the key above


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
    if token:
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
    want = [r.lower() for r in repos]
    try:
        others = [r for r in _github_account_repos(token)
                  if r.lower() not in want]
        extra = [r for r in others if _pull_request_probe(token, r) == 422]
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


# A Bugzilla key is the account: nothing narrower exists, so the rule asks only whether bugs.webkit.org takes it for the login this GitHub account maps to. `valid_login` answers false for a key of another account and error 306 for one it does not know.
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
    return OK, ("%s accepts it as %s.\n    spent on every bugs.webkit.org request "
                "a workspace makes while push is on, and on none while it is off"
                % (BUGZILLA_API, login))


def _some(names, n=3):
    return ", ".join(sorted(names)[:n]) + (", ..." if len(names) > n else "")


def _github_account_repos(token):
    """Every repository the account owns or collaborates on that the token can read: the public ones whatever the token was granted, the private ones only when granted. It answers for the account (`permissions` is the account's), so what the token reaches is measured per repository by the probe below."""
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
    """The write-shaped probe that creates nothing: an empty body names no head or base branch, so an authorised call is 422 and an unauthorised one 403 (measured 2026-09-11: a fine-grained token answers 422 on exactly the repositories it was granted with 'Pull requests: write', 403 on every other repository the account has)."""
    status, _headers, _body = _http("POST", "%s/repos/%s/pulls"
                                    % (GITHUB_API, repo), token, body=b"{}",
                                    headers=GITHUB_HEADERS)
    return status


def _github_pat_can_open_a_pr(token, repo):
    try:
        status = _pull_request_probe(token, repo)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s) to ask whether a pull "
                            "request can be opened on %s." % (GITHUB_API, e, repo))
    if status == 422:
        return OK, ""
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
                        "or 403, so whether a pull request can be opened is "
                        "not known." % (repo, status))


LOGIN_SCOPES = ("user:profile", "user:inference")

CLAUDE_OAUTH = _api_base("WK_CLAUDE_OAUTH", "https://platform.claude.com")
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # the CLI's own OAuth client, which a refresh names
# The CLI holds its refresh lock as a directory beside the credential and treats one older than a minute as abandoned; held the same way here, the two never spend one refresh token at once.
REFRESH_LOCK = ".oauth_refresh.lock"
REFRESH_LOCK_STALE = 60
REFRESH_AHEAD = 60
RC_FACT = "remote-control"


def _claude_login(value, repos, path, evidence):
    try:
        doc = json.loads(value)
    except Exception as e:
        return BAD, "that is not JSON at all (%s)." % e.__class__.__name__
    oauth = doc.get("claudeAiOauth") if isinstance(doc, dict) else None
    verdict, why = _login_shape(oauth)
    if verdict != OK:
        return verdict, why
    if not path:
        return OK, "; ".join(_login_facts(oauth)) + "."
    org = _login_organization(path)
    if not org:
        return BAD, ("no account record beside it: remote control reads "
                     "the organization from the CLI's own config file "
                     "(oauthAccount.organizationUuid in %s), which `claude "
                     "auth login` writes into the directory CLAUDE_CONFIG_DIR "
                     "names, and this login was made without pointing it "
                     "there." % _login_record_path(path))
    verdict, why, oauth = _login_renewed(path, doc)
    if verdict != OK:
        return verdict, why + _rc_line("unverified")
    return _login_measured(oauth, org)


def _login_shape(oauth):
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
    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_expiry, (int, float)) and refresh_expiry < time.time() * 1000:
        return BAD, ("the refresh token expired %s, so this login cannot be "
                     "renewed and no workspace can use it."
                     % _when(refresh_expiry))
    return OK, ""


def _login_facts(oauth):
    facts = ["scopes: %s" % " ".join(str(s) for s in oauth["scopes"])]
    if oauth.get("subscriptionType"):
        facts.append("subscription: %s" % oauth["subscriptionType"])
    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_expiry, (int, float)):
        facts.append("renewable until %s" % _when(refresh_expiry))
    return facts


def _rc_line(state):
    return "\n    %s: %s" % (RC_FACT, state)


def _login_current(oauth):
    expiry = oauth.get("expiresAt")
    return (isinstance(expiry, (int, float))
            and expiry > (time.time() + REFRESH_AHEAD) * 1000)


# The login as a session would spend it: renewed through its refresh token once the access token has run out, and written back over the one file every holder reads.
def _login_renewed(path, doc):
    oauth = doc["claudeAiOauth"]
    if _login_current(oauth):
        return OK, "", oauth
    lock = os.path.join(os.path.dirname(path), REFRESH_LOCK)
    held = _take_refresh_lock(lock)
    if held is not True:
        return UNVERIFIED, ("its access token has expired and another process "
                            "holds the refresh lock (%s, %ds old), so it was "
                            "neither renewed nor asked about; the next check "
                            "asks again." % (lock, held)), None
    try:
        # Re-read under the lock: a session may have renewed it since the value was read.
        try:
            with open(path) as f:
                current = json.load(f)
        except (OSError, ValueError):
            current = doc
        oauth = current.get("claudeAiOauth") if isinstance(current, dict) else None
        if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
            return BAD, "the file no longer holds a login.", None
        if _login_current(oauth):
            return OK, "", oauth
        verdict, why, fresh = _refresh(oauth)
        if verdict != OK:
            return verdict, why, None
        current["claudeAiOauth"] = fresh
        _write_login(path, current)
        return OK, "", fresh
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _take_refresh_lock(lock):
    """True once taken; otherwise the age in seconds of the lock another process holds."""
    for _ in range(2):
        try:
            os.mkdir(lock)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.stat(lock).st_mtime
            except OSError:
                continue
            if age < REFRESH_LOCK_STALE:
                return int(age)
            try:
                os.rmdir(lock)
            except OSError:
                pass
    return 0


def _refresh(oauth):
    body = json.dumps({"grant_type": "refresh_token",
                       "refresh_token": oauth["refreshToken"],
                       "client_id": CLAUDE_CLIENT_ID,
                       "scope": " ".join(str(s) for s in oauth["scopes"])}).encode()
    try:
        status, _headers, raw = _http("POST", CLAUDE_OAUTH + "/v1/oauth/token",
                                      None, body=body)
    except Unreachable as e:
        return UNVERIFIED, ("its access token has expired and %s (%s) did not "
                            "answer, so it was neither renewed nor asked about."
                            % (CLAUDE_OAUTH, e)), None
    if status in (400, 401, 403):
        return BAD, ("its access token has expired and Anthropic refuses to "
                     "renew it (HTTP %d%s): the login is revoked, or a copy of "
                     "it refreshed first and rotated the refresh token out from "
                     "under this one. Every session holding it stops at /login "
                     "and remote control cannot start."
                     % (status, _oauth_error(raw))), None
    if status != 200:
        return UNVERIFIED, ("its access token has expired and the token endpoint "
                            "answered HTTP %d rather than 200 or 401, so it was "
                            "not renewed." % status), None
    answer = _json(raw)
    try:
        access, expires_in = answer["access_token"], int(answer["expires_in"])
    except (KeyError, TypeError, ValueError):
        return UNVERIFIED, ("the token endpoint answered 200 with something "
                            "that is not a token."), None
    now = int(time.time() * 1000)
    fresh = dict(oauth)
    fresh["accessToken"] = access
    fresh["refreshToken"] = answer.get("refresh_token") or oauth["refreshToken"]
    fresh["expiresAt"] = now + expires_in * 1000
    if isinstance(answer.get("refresh_token_expires_in"), (int, float)):
        fresh["refreshTokenExpiresAt"] = now + int(answer["refresh_token_expires_in"]) * 1000
    if isinstance(answer.get("scope"), str) and answer["scope"].split():
        fresh["scopes"] = answer["scope"].split()
    return OK, "", fresh


def _oauth_error(raw):
    error = _json(raw).get("error")
    return ", %s" % error if isinstance(error, str) and error else ""


def _json(raw):
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return doc if isinstance(doc, dict) else {}


# A new file renamed over the old one: the same move the CLI makes, so a reader sees either login whole and a planted link at the path is replaced rather than followed.
def _write_login(path, doc):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=".credentials.json.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _login_measured(oauth, org):
    token = oauth["accessToken"]
    facts = _login_facts(oauth)
    try:
        status, _headers, raw = _http("GET", ANTHROPIC_API + "/api/oauth/profile",
                                      token)
    except Unreachable as e:
        return UNVERIFIED, ("could not reach %s (%s) to ask whether Anthropic "
                            "still accepts this login.\n    %s."
                            % (ANTHROPIC_API, e, "; ".join(facts))
                            + _rc_line("unverified"))
    if status == 401:
        return BAD, ("Anthropic does not accept this login (HTTP 401 on the "
                     "profile its scope grants): revoked, or signed out "
                     "elsewhere. Every session holding it stops at /login and "
                     "remote control cannot start.")
    if status != 200:
        return UNVERIFIED, ("GET /api/oauth/profile answered HTTP %d rather "
                            "than 200 or 401, so whether Anthropic accepts this "
                            "login is not known." % status
                            + _rc_line("unverified"))
    organization = _json(raw).get("organization") or {}
    facts.append("organization: %s" % (organization.get("name") or org))
    if organization.get("subscription_status"):
        facts.append("subscription %s" % organization["subscription_status"])
    state, why = _remote_control_policy(token)
    if state == "allowed":
        facts.append("remote control allowed by the organization's policy")
        return OK, "; ".join(facts) + "." + _rc_line(state)
    if state == "denied":
        return OK, ("%s; remote control DENIED: %s." % ("; ".join(facts), why)
                    + _rc_line(state)
                    + "\n    fix: an owner of the %s organization turns Remote "
                      "Control on in its Claude Code policy "
                      "(allow_remote_control); until then WK_NO_CLAUDE_RC=1 "
                      "makes workspaces without it" % (organization.get("name") or org))
    return OK, ("%s; remote control unverified: %s." % ("; ".join(facts), why)
                + _rc_line(state))


# What the CLI reads before it starts remote control (measured in 2.1.270): an `allow_remote_control` restriction, a HIPAA taint, or -- when the document cannot be loaded at all -- a refusal worded as the organization's policy.
def _remote_control_policy(token):
    try:
        status, _headers, raw = _http("GET", ANTHROPIC_API
                                      + "/api/claude_code/policy_limits", token)
    except Unreachable as e:
        return "unverified", ("could not reach %s (%s) for the organization's "
                              "policy" % (ANTHROPIC_API, e))
    if status == 404:
        return "unverified", ("the request for /api/claude_code/policy_limits "
                              "got a 404 -- a proxy between here and the API not "
                              "forwarding that path -- and the CLI refuses remote "
                              "control on the same answer")
    if status != 200:
        return "unverified", ("GET /api/claude_code/policy_limits answered HTTP "
                              "%d" % status)
    policy = _json(raw)
    restriction = (policy.get("restrictions") or {}).get("allow_remote_control")
    if isinstance(restriction, dict) and restriction.get("allowed") is False:
        return "denied", ("allow_remote_control is off in the organization's "
                          "Claude Code policy")
    if "hipaa" in (policy.get("compliance_taints") or []):
        return "denied", ("the organization is HIPAA-regulated, under which the "
                          "CLI refuses remote control")
    return "allowed", ""


def _login_record_path(path):
    return os.path.join(os.path.dirname(path), ".claude.json")


# Measured 2026-09-11 in Claude Code 2.1.269: `claude remote-control` refuses with "Unable to determine your organization" unless its config file's oauthAccount carries organizationUuid; nothing fetches one at start-up.
def _login_organization(path):
    try:
        with open(_login_record_path(path)) as f:
            account = (json.load(f) or {}).get("oauthAccount") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    if not isinstance(account, dict) or not account.get("organizationUuid"):
        return ""
    return account.get("organizationName") or account["organizationUuid"]


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
    # The key's own record is a management route, which a key every workspace holds must be shut out of; LiteLLM answers 403 for one restricted to the LLM API routes.
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
    return OK, ("%s.\n    `wk ai pi` writes it into a workspace's "
                "~/.pi/agent/models.json for %s." % ("; ".join(facts),
                                                      LITELLM_ENDPOINT))


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
    ("bugzilla-api-key", Rule(
        spent_by="container/proxy/github-inject.py -- the api_key query "
                 "parameter a workspace's bugs.webkit.org request is forwarded "
                 "with while push is on",
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
        remedy="it is `claude auth login` in a browser, run with the directory "
               "the containers share as the CLI's config home rather than this "
               "machine's, so the credential and the account record land beside "
               "each other; nothing is pasted",
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
        detail = ("%s\n    it must %s, and must not %s\n    fix: %s -- then: %s%s"
                  % (detail, rule.needs, rule.forbids, fix_of(rule, repos),
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
