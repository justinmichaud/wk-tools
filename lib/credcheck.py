"""Every credential wk holds and `verdict`, one line on a value: absent, ok (it does its job and no more), wide (more;
kept), bad (it cannot, or carries a power wk refuses; never stored) or unverified (no issuer answered; asked again)."""
import collections
import http.client as http_client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from wk import claudelogin, project
from wk.machine import Local

OK, WIDE, BAD, UNVERIFIED, ABSENT = ("ok", "wide", "bad", "unverified", "absent")


def api_base(var, default):
    value = os.environ.get(var)
    if not value:
        return default
    parts = urllib.parse.urlsplit(value)
    if parts.scheme == "https" or parts.hostname in ("127.0.0.1", "localhost"):
        return value
    raise SystemExit("%s=%s: a credential is sent there in an Authorization header, so it has to be https, or http on "
                     "127.0.0.1 or localhost. Unset %s to use %s." % (var, value, var, default))


GITHUB_API = api_base("WK_GITHUB_API", "https://api.github.com")
BUGZILLA_API = api_base("WK_BUGZILLA_API", project.get("BUGZILLA"))
ANTHROPIC_API = api_base("WK_ANTHROPIC_API", "https://api.anthropic.com")
LITELLM_API = api_base("WK_LITELLM_API", "https://ai.igalia.com")
LITELLM_ENDPOINT = LITELLM_API + "/v1"
LITELLM_KEYS = "https://ai.igalia.com/ui/api-keys/"
TAILSCALE_KEYS = "https://login.tailscale.com/admin/settings/keys"
TAG_PROBE = "wk-credcheck"
TIMEOUT = 20
PER_PAGE = 100
# The longest lifetime an organization's token policy allows; a token minted to never expire is refused every call to every repository the organization owns (measured against the upstream project, 2026-09-15).
MAX_PAT_DAYS = 365
# Classic: a fine-grained token reaches only its owner's repositories, so it opens a pull request on no upstream project.
CLASSIC_TOKEN_PAGE = "https://github.com/settings/tokens/new?" + urllib.parse.urlencode(
    [("scopes", "public_repo"), ("description", "wk -- opens pull requests from a wk workspace")])
CLASSIC_REFUSED = ("delete_repo", "site_admin")
KEYCHAIN_LOGIN = "Claude Code-credentials-"   # on macOS the CLI keeps a login in the keychain, named after its config dir

# `remedy` is what the `url` link cannot fill in; an `alone` credential is never copied to another machine.
Rule = collections.namedtuple("Rule", "needs forbids what url remedy store_with check mint alone", defaults=(None, False))


class Unreachable(Exception):
    pass


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
    try:
        with urllib.request.build_opener(*(() if follow else (_NoRedirect,))).open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except (OSError, http_client.HTTPException) as e:
        raise Unreachable("%s: %s" % (e.__class__.__name__, e))


def _http(method, url, token, body=None, headers=()):
    return http(method, url, ((("Authorization", "Bearer " + token),) if token else ()) + tuple(headers), body)


def _github(method, path, token, body=None):
    return _http(method, GITHUB_API + path, token, body, (("Accept", "application/vnd.github+json"),))


def _json(raw):
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def bugzilla_host():
    return urllib.parse.urlsplit(project.get("BUGZILLA")).hostname


def _pr(token, repo):
    """An empty body names no branch, so an authorised call is 422 and an unauthorised one 403 (measured 2026-09-11)."""
    status, _h, body = _github("POST", "/repos/%s/pulls" % repo, token, b"{}")
    return status, _json(body).get("message") or ""


def _pr_verdict(token, kind, repo, upstream):
    status, message = _pr(token, repo)
    if status == 422:
        return OK, ""
    if status == 403 and upstream:
        return BAD, "%s refuses this token a pull request (HTTP 403; GitHub says: %s)%s" % (
            repo, message or "nothing", "; a fine-grained token reaches only its own account's repositories, so only a "
            "classic one can" if kind == "fine-grained" else "")
    if status == 403:
        return BAD, "no 'Pull requests: write' on %s (HTTP 403), so `%s pr` cannot open one there" % (repo, project.get("PR_TOOL"))
    if status in (401, 404):
        return BAD, ("GitHub does not accept this token for %s (HTTP 401)" if status == 401 else "this token cannot see %s (HTTP 404)") % repo
    return UNVERIFIED, "POST /repos/%s/pulls answered HTTP %d rather than 422 or 403" % (repo, status)


def _github_pat(value, repos, path, evidence):
    token = value.strip()
    kind = "" if not token or len(token.split()) != 1 else "fine-grained" if token.startswith("github_pat_") else \
        "classic" if token.startswith("ghp_") else "other" if token[:4] in ("gho_", "ghu_", "ghs_", "ghr_") else ""
    if not kind:
        return BAD, "that is not one GitHub personal access token ('github_pat_' fine-grained, 'ghp_' classic)"
    if kind == "other":
        return BAD, "that is an OAuth, app or refresh token, not a personal access token"
    try:
        status, headers, body = _github("GET", "/user", token)
        if status in (401, 403):
            return BAD, "GitHub does not accept this token (HTTP %d): revoked, expired or mistyped" % status
        if status != 200:
            return UNVERIFIED, "GET /user answered HTTP %d rather than 200 or 401" % status
        scopes = [s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip()]
        expiry = headers.get("github-authentication-token-expiration")
        who = "as %s%s" % (_json(body).get("login") or "?", ", expires " + expiry if expiry else "")
        refused = [s for s in scopes if s in CLASSIC_REFUSED or s.startswith("admin:")]
        if refused:
            return BAD, "a classic token carrying %s on every repository this account reaches (%s)" % (", ".join(refused), who)
        for repo in repos:
            verdict, why = _pr_verdict(token, kind, repo, False)
            if verdict != OK:
                return verdict, why
        bases = []
        for repo in repos:   # the PR tool posts to each fork's parent, so that project has to accept the token too
            status, _h, body = _github("GET", "/repos/%s" % repo, token)
            if status != 200:
                return UNVERIFIED, "GET /repos/%s answered HTTP %d, so which project each fork belongs to is not known" % (repo, status)
            parent = (_json(body).get("parent") or {}).get("full_name")
            if parent and parent not in bases + list(repos):
                bases.append(parent)
        for repo in bases:
            verdict, why = _pr_verdict(token, kind, repo, True)
            if verdict != OK:
                return verdict, why
        if kind == "classic":
            return WIDE, "a classic token %s (scopes: %s): it reaches every %srepository this account can write, the narrowest " \
                "that opens a pull request on the projects" % (who, ", ".join(scopes) or "none", "" if "repo" in scopes else "public ")
        want, others, page = {r.lower() for r in repos}, [], 1
        while True:   # `permissions` here is the account's, so each repository is probed like the forks
            status, _h, body = _github("GET", "/user/repos?per_page=%d&page=%d" % (PER_PAGE, page), token)
            try:
                batch = [r["full_name"] for r in json.loads(body)] if status == 200 else None
            except (ValueError, TypeError, KeyError):
                batch = None
            if batch is None:
                return UNVERIFIED, "GET /user/repos answered HTTP %d and no list, so which repositories this token reaches is not known" % status
            others += [r for r in batch if r.lower() not in want]
            if len(batch) < PER_PAGE:
                break
            page += 1
        extra = sorted(r for r in others if _pr(token, r)[0] == 422)
        if extra:
            return BAD, "it reaches %d repositories beyond the %d forks wk pushes to (%s%s)" % (
                len(extra), len(repos), ", ".join(extra[:3]), ", ..." if len(extra) > 3 else "")
        return OK, "a fine-grained token %s: it opens a pull request on the %d forks and their projects, and on none of the " \
            "%d other repositories under this account" % (who, len(repos), len(others))
    except Unreachable as e:
        return UNVERIFIED, "could not reach %s (%s); 'wk doctor' asks again" % (GITHUB_API, e)


def _bugzilla_api_key(value, repos, path, evidence):
    """`valid_login` answers false for another account's key and error 306 for one it does not know."""
    key, login = value.strip(), evidence.get("login", "")
    if not key or len(key.split()) != 1:
        return BAD, "there is no key there, or it is not one line"
    if not login:
        return UNVERIFIED, ("no Bugzilla login to check it against: the first email of this GitHub account in %s's "
                            "metadata/contributors.json, read from the mirror ('wk sync' makes one)" % project.get("CHECKOUT"))
    try:
        status, _h, raw = _http("GET", BUGZILLA_API + "/rest/valid_login?" + urllib.parse.urlencode({"login": login, "api_key": key}), None)
    except Unreachable as e:
        return UNVERIFIED, "could not reach %s (%s)" % (BUGZILLA_API, e)
    doc = _json(raw)
    if status == 400 and doc.get("code") == 306:
        return BAD, "%s does not accept this key (error 306): revoked, mistyped or never valid" % BUGZILLA_API
    if status != 200:
        return UNVERIFIED, "GET /rest/valid_login answered HTTP %d rather than 200 or 400" % status
    if doc.get("result") is not True:
        return BAD, "%s takes this key for another account than %s, which `%s pr` would file as" % (BUGZILLA_API, login, project.get("PR_TOOL"))
    return OK, "%s accepts it as %s, spent on every request a person's shell in a workspace makes; %s" % (
        bugzilla_host(), login, _bugzilla_tagger(key))


def _bugzilla_tagger(key):
    """Bugzilla 5.0 gates comment-tag search on editbugs by default (error 304; 125 is tagging off); it writes nothing."""
    try:
        status, _h, raw = _http("GET", "%s/rest/bug/comment/tags/%s?%s" % (BUGZILLA_API, TAG_PROBE, urllib.parse.urlencode({"api_key": key})), None)
    except Unreachable as e:
        return "its comment-tagging right is not known (%s)" % e
    code = _json(raw).get("code")
    if status == 200 and raw.lstrip().startswith(b"["):
        return "it can tag comments (editbugs on a default install)"
    if code == 304 or status in (401, 403):
        return "it cannot tag comments (no editbugs on a default install)"
    return "comment tagging is off there" if code == 125 else "its comment-tagging right is not known (HTTP %d)" % status


def _anthropic_accepts(token):
    """Measured 2026-09-10: an accepted `Bearer sk-ant-oat` token answers 200, a refused one 401, and no model is inferred."""
    try:
        status, _h, _b = _http("GET", "%s/v1/models?limit=1" % ANTHROPIC_API, token, headers=(("anthropic-version", "2023-06-01"),))
    except Unreachable as e:
        return UNVERIFIED, "could not reach %s (%s)" % (ANTHROPIC_API, e)
    if status in (200, 401):
        return (OK, "Anthropic accepts it") if status == 200 else (BAD, "Anthropic does not accept it (HTTP 401)")
    return UNVERIFIED, "GET /v1/models answered HTTP %d rather than 200 or 401" % status


def _claude_token(value, repos, path, evidence):
    token = value.strip()
    if token.startswith("{"):
        return BAD, "that is a login document, not a token: `claude setup-token` prints the one this row takes"
    if token.startswith("sk-ant-api"):
        return BAD, "that is an Anthropic Console API key: it bills the organization and is not restricted to Claude Code"
    if not token.startswith("sk-ant-oat"):
        return BAD, "a `claude setup-token` credential starts 'sk-ant-oat'; that does not"
    verdict, why = _anthropic_accepts(token)
    if verdict == BAD:
        return BAD, why + ": it is spent, revoked or expired, so every workspace holding it asks for /login"
    return verdict, why + (", and it is inference-only: it cannot read the account or mint anything" if verdict == OK else "")


def _claude_login(value, repos, path, evidence):
    try:
        oauth = claudelogin.parse(value)
    except ValueError as e:
        return BAD, "that is not a claude.ai login document: %s" % e
    if oauth["expiresAt"] <= time.time() * 1000:
        return UNVERIFIED, "its access token has expired; the injector refreshes it on the next request, which shows whether it still works"
    verdict, why = _anthropic_accepts(oauth["accessToken"])
    if verdict == BAD:
        return BAD, why + ": the login was revoked, or another holder refreshed it and spent this one"
    return verdict, why + ("; the injector holds and refreshes it, every workspace a placeholder" if verdict == OK else "")


def _litellm_key(value, repos, path, evidence):
    key = value.strip()
    if not key or key.startswith("sk-ant-"):
        return BAD, "that is an Anthropic key, which reaches the upstream account directly" if key else "there is nothing there"
    try:
        status, _h, raw = _http("GET", LITELLM_ENDPOINT + "/models", key)
        if status == 401:
            return BAD, "%s does not accept this key (HTTP 401), so `wk ai pi` answers 401" % LITELLM_API
        if status != 200:
            return UNVERIFIED, "GET /v1/models at %s answered HTTP %d rather than 200 or 401" % (LITELLM_API, status)
        served = "%s accepts it and serves it %d model(s)" % (LITELLM_API, len(_json(raw).get("data") or []))
        # LiteLLM answers 403 on this management route for a key restricted to the LLM API routes.
        status, _h, raw = _http("GET", LITELLM_API + "/key/info", key)
    except Unreachable as e:
        return UNVERIFIED, "could not reach %s (%s)" % (LITELLM_API, e)
    if status == 200:
        return WIDE, "%s, and it reaches the key-management routes (alias %s)" % (served, (_json(raw).get("info") or {}).get("key_alias") or "none")
    if status != 403:
        return UNVERIFIED, "%s; /key/info answered HTTP %d rather than 200 or 403" % (served, status)
    return OK, "%s, restricted to the LLM API routes; each `wk new` recomputes the machine's pi models.json for %s" % (served, LITELLM_ENDPOINT)


def chat_model_ids(doc):
    return [m.get("model_name") for m in doc.get("data") or []
            if m.get("model_name") and (m.get("model_info") or {}).get("mode") in (None, "chat")]


def litellm_models(key):
    status, _headers, raw = _http("GET", LITELLM_ENDPOINT + "/model/info", key.strip())
    return chat_model_ids(_json(raw)) if status == 200 else []


def litellm_callable(key, ids):
    """The first of `ids` a one-token completion reaches, or None: the endpoint lists models that answer 404 (measured 2026-09-27)."""
    for model in ids:
        body = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode()
        if _http("POST", LITELLM_ENDPOINT + "/chat/completions", key.strip(), body)[0] == 200:
            return model
    return None


def _tailscale_wrong(key, wanted):
    for prefix, what in (("tskey-api-", "an API access token, which administers the whole tailnet"),
                         ("tskey-auth-", "a node auth key, which enrolls a node"),
                         ("tskey-client-", "an OAuth client secret, which mints keys of both kinds"),
                         ("tskey-oauth-", "an OAuth client secret, which mints keys of both kinds")):
        if key.startswith(prefix):
            return "that is %s, not %s" % (what, wanted)
    return "that is not %s: they start 'tskey-'" % wanted if key else "there is nothing there"


def _tailnet_authkey(value, repos, path, evidence):
    key = value.strip()
    if key.startswith("tskey-auth-") and len(key) > len("tskey-auth-"):
        return OK, "an auth key, so it can enroll a node and nothing else; whether it is tag:wk, reusable and NOT ephemeral is at " + TAILSCALE_KEYS
    return BAD, _tailscale_wrong(key, "an auth key")


def _tailnet_api(value, repos, path, evidence):
    key = value.strip()
    if not (key.startswith("tskey-api-") and len(key) > len("tskey-api-")):
        return BAD, _tailscale_wrong(key, "an API access token")
    if not path:
        return OK, "an API access token; the tailnet is asked whether it accepts it once it is stored"
    from wk import tailnet
    try:
        n = len(tailnet.Api(key, tailnet.api_url(os.environ)).devices())
    except tailnet.Failed as e:
        return (UNVERIFIED, "could not ask the tailnet: %s" % e) if e.code == 6 else (BAD, "the tailnet refused it: %s" % e)
    return OK, "the tailnet accepts it (%d devices); it is never written to a card" % n


def _deploy_key(value, repos, path, evidence):
    """The evidence is gathered where the key is (`wk key sshtest`); the verdict is reached here."""
    repo, ssh, read_only = repos[0] if repos else "?", evidence.get("ssh", ""), evidence.get("read_only", "")
    if not ssh or ssh.startswith("no key"):
        return BAD, "there is no key for %s on that machine" % repo
    if "Hi %s!" % repo not in ssh:
        if "Permission denied" in ssh:
            return BAD, "GitHub does not know this key: it is not registered on %s" % repo
        if "successfully authenticated" in ssh:
            return BAD, "this key authenticates as an account key, not a deploy key on %s, so it reaches every repository that account can" % repo
        return UNVERIFIED, "ssh to github.com answered '%s'" % ssh.strip().splitlines()[0][:120]
    return {"false": (OK, "registered on %s with write access, and on no other" % repo),
            "true": (BAD, "registered on %s READ-ONLY, so a push fails at the door" % repo)}.get(
        read_only, (UNVERIFIED, "registered on %s and scoped to it; whether it may write is unconfirmed" % repo))


def _keychain_logins(here):
    if sys.platform != "darwin":
        return set()
    return {m for m in re.findall(r'"svce"<blob>="([^"]+)"', here.run(["security", "dump-keychain"]).out) if m.startswith(KEYCHAIN_LOGIN)}


def _claude_login_mint():
    """`claude auth login` in a scratch config dir; what it wrote, file or keychain item, is read and removed: one holder."""
    here, d = Local(), tempfile.mkdtemp(prefix="wk-claude-login-")
    before, made = _keychain_logins(here), []
    try:
        r = here.run_tty(["env", "CLAUDE_CONFIG_DIR=" + d, "CLAUDE_SECURESTORAGE_CONFIG_DIR=" + d, "claude", "auth", "login", "--claudeai"])
        made = sorted(_keychain_logins(here) - before)
        doc = os.path.join(d, ".credentials.json")
        text = here.read(doc) if here.exists(doc) else ""
        if not text and len(made) == 1:
            text = here.run(["security", "find-generic-password", "-s", made[0], "-w"]).out
        if not (r.ok and text.strip()):
            sys.stderr.write("credcheck: `claude auth login` %s, so no claude.ai login was minted\n"
                             % ("exited %d" % r.rc if not r.ok else "left no login in %s or one new keychain item (%d)" % (d, len(made))))
            return ""
        return json.dumps(json.loads(text), separators=(",", ":"))
    finally:
        for service in made:
            here.run(["security", "delete-generic-password", "-s", service])
        here.remove_now(d)


def _row(name, check, url="", store_with=None, **kw):
    return name, Rule(url=url, store_with=store_with or "wk key set " + name, check=check, **kw)


def rules():
    tool, bz = project.get("PR_TOOL"), bugzilla_host()
    return collections.OrderedDict((
        _row("github-pat", _github_pat, CLASSIC_TOKEN_PAGE,
             needs="open a pull request from each fork wk pushes to, on the project it is a fork of",
             forbids="delete a repository, or administer a repository, an organization or the site",
             what="a GitHub personal access token, so `%s pr` in a workspace can open a pull request" % tool,
             remedy=("the two fields that link cannot carry: tick nothing but the 'public_repo' it preselects ('repo' "
                     "adds every private repository this account can write), and set an expiry of at most %d days -- an "
                     "organization refuses a token that outlives its policy" % MAX_PAT_DAYS)),
        _row("bugzilla-api-key", _bugzilla_api_key, project.get("BUGZILLA") + "/userprefs.cgi?tab=apikey",
             needs="be accepted by %s as the login %s's metadata/contributors.json gives this GitHub account" % (bz, project.get("CHECKOUT")),
             forbids="rest where a workspace reads, or be spent while push is off: a Bugzilla key is the whole account",
             what="a %s API key, so `%s pr` in a workspace can file the bug and post the pull request to it" % (bz, tool),
             remedy="'New API key' there, described as this machine; the key is shown once"),
        _row("claude", _claude_token, needs="authenticate Claude Code for inference",
             forbids="read the account, bill the organization, or mint further credentials",
             what="a Claude Code token, so a guest or a build box starts authenticated instead of asking for /login",
             remedy="run `claude setup-token` here and paste what it prints"),
        _row("claude-login", _claude_login, mint=_claude_login_mint, alone=True,
             needs="authenticate Claude Code as your claude.ai account, Remote Control included",
             forbids="have a second holder: a refresh spends the refresh token, so whichever holder refreshes second is logged out",
             what="your claude.ai login, which the credential injector holds and refreshes for every workspace here",
             remedy="wk key set claude-login runs `claude auth login` in a scratch directory: approve it in the browser"),
        _row("litellm", _litellm_key, LITELLM_KEYS, needs="reach your own LiteLLM endpoint",
             forbids="reach the upstream provider account directly",
             what="your LiteLLM API key, so `wk ai pi` in a workspace can reach that endpoint",
             remedy="'+ Create New Key' there; the key is shown once"),
        _row("tailnet", _tailnet_authkey, TAILSCALE_KEYS, needs="enroll a node on the tailnet",
             forbids="administer the tailnet or mint further keys",
             what="the fleet's tailnet auth key, so a card written here boots onto the tailnet under its own name",
             remedy="Generate auth key: tagged tag:wk, Reusable on, Ephemeral OFF, longest expiry"),
        _row("tailnet-api", _tailnet_api, TAILSCALE_KEYS, needs="list and delete devices on this tailnet",
             forbids="be written to a card or reach a workspace: it administers the whole tailnet",
             what="the tailnet API access token a workstation retires a stale fleet node with",
             remedy="Generate access token: the tag:wk devices scope is enough"),
        _row("deploy-key", _deploy_key, store_with="wk key deploy", needs="push to exactly one fork",
             forbids="reach any other repository, or be read-only", what="an ed25519 key per fork, generated here and never pasted",
             remedy="wk key deploy  (it registers with read_only=false)"),
    ))


def fix_of(rule):
    return " -- ".join(x for x in (rule.url, rule.remedy) if x)


def verdict(name, value, repos=(), path="", evidence=None):
    """`<verdict>\\t<one line>`, then for a bad one `    fix: <where> -- then: <command>`; with `path`, empty is absent."""
    rule = rules()[name]
    if path and not value.strip():
        return "%s\tnothing stored -- %s" % (ABSENT, rule.store_with)
    v, why = rule.check(value, list(repos), path, evidence or {})
    fix = "\n    fix: %s -- then: %s%s" % (fix_of(rule), rule.store_with, " --replace" if path else "") if v == BAD else ""
    return "%s\t%s%s" % (v, why, fix)
