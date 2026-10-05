import os
import signal
import sys

from wk import act, guest, images, job, secrets, store
from wk.act import Refused, changed, die, info, log, warn

ROOT = images.root()

UNASKED, NO_KEYS, NO_SWITCH = 3, 4, 5

# The process is `claude`, `node` or a version string, but its exe resolves under the install; a macOS guest has no /proc.
AGENT_PID_SCAN = '''if [ -d /proc/self ]; then
    for e in /proc/[0-9]*/exe; do
        case "$(readlink "$e" 2>/dev/null)" in
            */claude/versions/*|*/.local/bin/claude) p=${e#/proc/}; printf "%s\\n" "${p%/exe}" ;;
        esac
    done
else
    ps -Ao pid=,comm= | while read -r p c; do
        case "$c" in */claude/versions/*|*/.local/bin/claude|claude) printf "%s\\n" "$p" ;; esac
    done
fi'''

NOT_RUNNING = ("absent", "created", "configured", "exited", "stopped")


class Push:
    def __init__(self, reg, sec, clock, out=None):
        self.reg, self.sec, self.clock = reg, sec, clock
        self.out = out or sys.stdout
        self.target_name = reg.default()
        try:
            self.target = reg.load(self.target_name)
        except LookupError:
            self.target = None
        self.ws_sock = self.target.agent_sock() if self.target else None
        self.sock, self.pat, self.bz = sec.machine_sock(), sec.machine_pat(), sec.machine_bugzilla_key()
        self.guest_live = False
        self.in_vm = store.in_vm(reg.env) and not sec.macos

    def say(self, what, text):
        self.out.write("%-10s %s\n" % (what, text))

    def run(self, action):
        rc = getattr(self, "switch_" + action)()
        if action != "status":
            rc = max(rc, self.converge_guests(action))
        return rc

    def session_targets(self):
        """Every target this machine holds workspaces on, each of which `on` would hand the keys: the macOS guests too."""
        out = [self.target] if self.target else []
        if self.sec.macos and self.reg.vm_listed():
            try:
                out.append(self.reg.load("vm"))
            except LookupError:
                pass
        return out

    def agent_pids(self, t, ws):
        """The claude pids in `ws`, or None: its scan failed and it may be running one."""
        r = t.exec(ws, ["sh", "-c", AGENT_PID_SCAN])
        if not r.ok:
            return [] if t.info(ws) in NOT_RUNNING else None
        return [p for p in r.out.replace("\r", "").split() if p.isdigit()]

    def agent_sessions(self):
        """(target, workspace, pids) for each workspace with a claude process in it, pids None where it could not be asked."""
        found = [(t, ws, self.agent_pids(t, ws)) for t in self.session_targets() for ws, _ in t.list() if ws]
        return [(t, ws, pids) for t, ws, pids in found if pids is None or pids]

    def end_agent_sessions(self, sessions):
        """False when one outlived the KILL, or could no longer be asked."""
        ok = True
        for t, ws, _ in sessions:
            pids = self.agent_pids(t, ws)
            if pids is None:
                ok = False
                continue
            if not pids:
                continue
            info("ending the claude session(s) in '%s' (pid %s)" % (ws, " ".join(pids)))
            seen, grace = {"pids": pids}, job.kill_wait(self.reg.env)

            def send(s, t=t, ws=ws, seen=seen, grace=grace):
                if s == signal.SIGKILL:
                    warn("pid %s did not stop on TERM after %ds -- killing" % (" ".join(seen["pids"]), grace))
                t.act_exec(ws, ["sh", "-c", "kill %s%s 2>/dev/null; exit 0"
                                % ("-KILL " if s == signal.SIGKILL else "", " ".join(seen["pids"]))])

            def gone(t=t, ws=ws, seen=seen):
                seen["pids"] = self.agent_pids(t, ws)
                return not seen["pids"]
            if act.dry_run():
                send(signal.SIGTERM)
                continue
            job.terminate(send, gone, self.clock, grace, 5)
            ok = ok and seen["pids"] == []
        return ok

    def end_sessions_first(self, sessions):
        unknown = " ".join(ws for _, ws, pids in sessions if pids is None)
        if unknown:
            act.barrier("could not ask %s whether a claude session runs in it, and it may be running one --\n    loading the "
                        "deploy keys would hand that session a working push. 'wk status' names\n    what is wrong with it." % unknown)
        sessions = [(t, ws, pids) for t, ws, pids in sessions if pids]
        names = " ".join(ws for _, ws, _ in sessions)
        if sessions and act.forced():
            act.barrier("a claude session is running in %s -- loading the deploy keys hands it a\n    working push for as long "
                        "as it runs, and its commit wall stays until it exits." % names)
        elif sessions:
            warn("a claude session is running in %s -- loading the keys\n    would hand it a working push, and its commit wall "
                 "stays until it exits, so\n    it is ended first. Whatever it has not written down cannot be undone." % names)
            if not act.confirm("end the claude session(s) in %s?" % names):
                die("push stays off -- nothing was loaded and no session was touched")
            if not self.end_agent_sessions(sessions):
                die("a claude session outlived a KILL, so the keys stay out of the agent.\n    Find it:  wk enter <workspace>")
            return
        # Ending a session is `on`'s one destructive effect, and past this gate none is left to end.
        act.nothing_to_ask()

    def require_agent_target(self):
        if self.ws_sock:
            return
        warn("'%s' is a build box: it holds no deploy key, so there is no switch here.\n    A push is made from the workstation, "
             "with the switch there:  wk pr open <workspace>" % self.target_name)
        raise Refused(NO_SWITCH)

    def switch_on(self):
        sec = self.sec
        self.require_agent_target()
        self.end_sessions_first(self.agent_sessions())
        if not sec.agent_answers(self.sock):
            warn("no ssh-agent answers at %s on the machine that runs the\n    workspaces, so there is nothing to load the deploy "
                 "keys into" % self.sock)
            log("  ./setup installs and starts wk-ssh-agent.service there")
            return 1
        # Before the keys, since `wk key deploy` may have minted public halves since the store was made.
        sec.publish()
        rows = [state for _, state in sec.agent_load(self.sock)]
        if rows.count("FAILED"):
            warn("%d deploy key(s) would not load into the agent, so push is not fully on" % rows.count("FAILED"))
            log("  the agent is at %s; 'wk push on' again once it answers" % self.sock)
            return 1
        if not rows.count("loaded"):
            warn("no deploy key here to load -- 'wk key deploy' makes them")
            return NO_KEYS
        changed("push is ON -- %d deploy key(s) in the agent outside every workspace" % rows.count("loaded"))
        if rows.count("no-key"):
            log("  %d fork(s) have no key here ('wk key deploy')" % rows.count("no-key"))
        log("  every workspace can push to the forks now, including any agent in one.")
        log("  'wk push off' when you are done; 'wk ai claude' turns it off by itself.")
        if sec.cred_write(self.pat, "github-pat"):
            changed("the GitHub API token is where the injector reads it (%s)" % sec.github_user())
            log("  'git-webkit pr' in a workspace posts as that account; the token stays here")
        else:
            sec.cred_clear(self.pat)
            warn("there is no GitHub API token here, so 'git-webkit pr' in a workspace gets 401")
            log("  'wk key set github-pat' stores one (repo scope; it never enters a workspace)")
        if sec.cred_write(self.bz, "bugzilla-api-key"):
            changed("the Bugzilla API key is where the injector reads it (%s)" % (sec.bugzilla_user() or "no login in the mirror"))
            log("  'git-webkit pr' in a workspace files and updates the bug as that login; the key stays here")
        else:
            sec.cred_clear(self.bz)
            warn("there is no Bugzilla API key here, so 'git-webkit pr' in a workspace cannot file or update a bug")
            log("  'wk key set bugzilla-api-key' stores one (it never enters a workspace)")
        log("  a push also needs this machine's key registered:  wk key check")
        return 0

    def switch_off(self):
        sec = self.sec
        self.require_agent_target()
        sec.agent_clear(self.sock)
        sec.cred_clear(self.pat)
        sec.cred_clear(self.bz)
        # It names public halves and a socket, not a credential, and an unresolvable `github-webkit` fails with a hostname error.
        sec.publish()
        if act.dry_run():
            changed("push is OFF -- nothing was cleared, so nothing is read back")
            return 0
        # Evidence, not the exit status of the clear: an agent that ignored `ssh-add -D` is one a workspace can still push with.
        left = sec.agent_list(self.sock)
        if left:
            warn("the agent at %s still holds %d identity/identities" % (self.sock, len(left)))
            log("  push is NOT off; 'wk push off' again, or restart wk-ssh-agent.service there")
            return 1
        for path, what in ((self.pat, "GitHub API token"), (self.bz, "Bugzilla API key")):
            if sec.cred_present(path):
                warn("the %s is still at %s on that machine" % (what, path))
                log("  push is NOT off; 'wk push off' again")
                return 1
        changed("push is OFF -- the agent is empty and the injector has no token and no Bugzilla key")
        log("  workspaces keep their remotes and can fetch; a push and an API call are")
        log("  both refused at the door. The keys never left %s." % sec.held_dir())
        return 0

    def where(self, fork, in_agent):
        """loaded | held | live | absent | forwarded; `live` is a key at rest on a build box, which holds none (remote/provision.sh)."""
        pub = self.sec.pub_path(fork)
        if self.sec.machine.exists(pub):
            fp = self.sec.machine.run(["ssh-keygen", "-lf", pub], input="").out.split()
            if len(fp) > 1 and fp[1] in in_agent:
                return "loaded"
        if self.sec.key_at_rest(fork):
            return "held" if self.ws_sock else "live"
        return "absent" if self.ws_sock else "forwarded"

    def exposed_private_keys(self):
        d = self.sec.secrets_dir()
        m = self.sec.machine
        names = m.listdir(d) if m.isdir(d) else []
        return [f for f in names if f.startswith("build_key") and not f.endswith(".pub") and not m.isdir(os.path.join(d, f))]

    def switch_status(self):
        sec = self.sec
        held_dir = sec.held_dir()
        in_agent = {line.split()[1] for line in sec.agent_list(self.sock) if len(line.split()) > 1}
        words = {"loaded": "push allowed (in the agent)", "held": "held back (%s)" % held_dir,
                 "live": "at rest (%s) -- a build box holds none; 'wk machine setup' removes it" % held_dir,
                 "absent": "no key ('wk key deploy')", "forwarded": "no key at rest; a push is made from the workstation"}
        count = {w: 0 for w in words}
        for fork in [f[0] for f in sec.forks()]:
            w = self.where(fork, in_agent)
            count[w] += 1
            self.say(fork, words[w])

        cred_live = False
        for what, path, name, allowed, held_path in (
                ("api", self.pat, "github-pat", "'git-webkit pr' allowed (token at %s)", sec.github_pat_path()),
                ("bugzilla", self.bz, "bugzilla-api-key", "'git-webkit pr' can file the bug (key at %s)", sec.bugzilla_key_path())):
            if sec.cred_present(path):
                cred_live = True
                self.say(what, allowed % path)
            elif secrets.first_line(sec.cred_read(name)):
                self.say(what, "held back (%s)" % held_path)
            else:
                self.say(what, "no %s ('wk key set %s')" % ("token" if name == "github-pat" else "key", name))

        exposed = self.exposed_private_keys()
        if exposed:
            warn("private key(s) in the mounted secrets directory, readable by every workspace:\n    %s" % " ".join(exposed))
            log("  move them to %s, which nothing mounts anywhere" % held_dir)

        if (count["held"] or count["loaded"]) and not sec.agent_answers(self.sock):
            log("  no ssh-agent answers at %s ('./setup' installs it there)" % self.sock)

        self.guest_agent_row()
        self.guest_rows()

        if count["live"]:
            info("push is ON here and cannot be switched off -- %d key(s) are live at rest" % count["live"])
            return 0
        if count["loaded"] or self.guest_live:
            info("push is ON")
            return 0
        # The injector's write token and Bugzilla key are credentials an agent could spend on a comment or a bug.
        if cred_live:
            info("push is ON -- no deploy key is loaded, but the injector has a write credential")
            return 0
        if count["held"]:
            info("push is OFF -- 'wk push on' to allow it")
            return 1
        if count["forwarded"]:
            info("push is OFF here -- no key at rest, and an agent session is forwarded none")
            return 1
        warn("there are no deploy keys here at all -- 'wk key deploy' makes them")
        return NO_KEYS

    # -- a macOS host's guests: an ssh-agent here and an `ssh -N -R` per guest (lib/wk/guest.py)

    def converge_guests(self, action):
        if self.in_vm:
            warn("this is the podman machine's half of the switch: the host's agent for its macOS\n    guests is not reached "
                 "from here, so push is not %s everywhere. On the host:  wk push %s" % (action, action))
            return UNASKED
        if not self.sec.macos or guest.vm_push_keys_converge(ROOT, self.sec.machine, action):
            return 0
        warn("the guest(s) named above were not converged and may still reach the agent;\n    'wk push %s' again once each one "
             "answers" % action)
        return 1

    def guest_agent_row(self):
        """A key in this host's agent is a push from here and from every guest holding a forward, whether or not one is up now."""
        if self.in_vm:
            self.say("guests", "not read from the podman machine -- 'wk push status' on the host")
            return
        if not self.sec.macos:
            return
        n = guest.vm_push_agent_keys(ROOT, self.sec.machine)
        self.guest_live = self.guest_live or n > 0
        self.say("guests", "%d key(s) in the agent this host runs for them and its own pushes" % n if n
                 else "the agent this host runs for them and its own pushes holds nothing")

    def guest_rows(self):
        for g, state, forks in guest.vm_push_keys_state(ROOT, self.sec.machine) if self.sec.macos else []:
            if state != "running":
                self.out.write("guest %-10s %s\n" % (g, "%s -- not read; 'wk start %s' converges it" % (state, g)))
            elif forks:
                self.guest_live = True
                self.out.write("guest %-10s %s\n" % (g, forks))
            else:
                self.out.write("guest %-10s %s\n" % (g, "no agent socket -- a push from in there is refused"))
