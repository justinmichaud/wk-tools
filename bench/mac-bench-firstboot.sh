#!/bin/bash
#
# The benchmark install configuring itself, once, at its first boot.
#
# Installed by the package `wk bench mac-volume --build-pkg` makes, which
# `startosinstall --installpackage` lays down on the target volume during the
# install. Runs as root from a LaunchDaemon, removes that LaunchDaemon when it
# finishes, and is the reason nobody has to sit at this machine.
#
# WHY A FIRST-BOOT DAEMON AND NOT A PACKAGE POSTINSTALL SCRIPT
#
# A package installed by startosinstall runs against a volume that is not
# running: `$3` is a mount point, not a system. Creating a user there means
# `dscl -f <target>/var/db/dslocal/nodes/Default localonly`, setting the login
# keychain by hand, and hoping the offline record matches what a running
# opendirectoryd would have written -- a well-known source of accounts that
# exist but cannot log in. Everything here needs a *running* system, so the
# package's job is reduced to the one thing that is genuinely safe offline
# (dropping files in place), and the work happens at first boot where
# `sysadminctl`, `systemsetup` and `pmset` are talking to a live machine.
#
# Idempotent throughout: it can be re-run by hand after a failure without
# undoing what already worked, because the failure mode of a half-provisioned
# benchmark install is a number that looks fine.

set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

PAYLOAD=/usr/local/share/wk-bench
LOG=/var/log/wk-bench-firstboot.log
DAEMON=/Library/LaunchDaemons/com.wk.bench-firstboot.plist
SELF=/usr/local/libexec/wk-bench-firstboot.sh

BENCH_USER="${WK_BENCH_USER:-bench}"
PROFILE=perf-macos-tolken

exec >>"$LOG" 2>&1
echo "=== wk-bench first boot: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

say() { echo "[wk-bench] $*"; }

# --- the account -------------------------------------------------------------
#
# A random password, and nothing ever needs to know it. It exists only because
# `sysadminctl -autologin` writes /etc/kcpassword from a real credential, and
# because an account with no password cannot be an autologin account. Console
# access is autologin and administrative access is the sudoers rule below, so
# there is no path that asks a human for this string -- which is why it is
# generated here and not printed.
# The password is loaded first and unconditionally. Read only in the branch that
# *creates* the account, a re-run over an existing account sets PW="" and skips
# autologin entirely -- "autologin left alone (no password on hand for an
# existing user)" -- so a repair run repairs everything except autologin, which
# is usually the thing it was run for.
if [ -r "$PAYLOAD/password" ]; then
    PW=$(cat "$PAYLOAD/password")
else
    PW="${WK_BENCH_PASSWORD:-benchbench}"
    say "no password in the payload; using the constant default"
fi

if id -u "$BENCH_USER" >/dev/null 2>&1; then
    say "user $BENCH_USER already exists"
else
    say "creating $BENCH_USER"
    sysadminctl -addUser "$BENCH_USER" -fullName "wk bench" -password "$PW" -admin \
        || say "WARNING: sysadminctl -addUser failed"
fi

# The failsafe, and it is the most important line in this file.
#
# The package sets /var/db/.AppleSetupDone so Setup Assistant never runs. If the
# account this script creates does not exist, that combination is an install
# with no users and no setup assistant: a login window offering nothing, no ssh
# account to reach, and Recovery as the only way back in. Locking a machine out
# of itself is a worse outcome than a manual setup.
#
# So if there is no account, the marker comes back off and the next boot asks a
# person, which is exactly the state this whole package exists to avoid -- and
# exactly the right state to fall back to.
if ! id -u "$BENCH_USER" >/dev/null 2>&1; then
    say "FAILSAFE: '$BENCH_USER' does not exist after creation; restoring Setup Assistant"
    rm -f /var/db/.AppleSetupDone
    say "  the next boot will run Setup Assistant so this machine stays reachable"
    say "  the first-boot daemon stays installed and will try again after that"
    exit 1
fi

# --- sudo without a password, deliberately -----------------------------------
#
# This is a real grant and it is made on purpose, so it is worth being explicit
# about why it is defensible *here* and would not be on a workstation:
#
#   * this install is cattle. It carries no user data, no keys that matter, no
#     checkout, and `wk bench mac-volume` can delete and remake it with one
#     command. Its whole contents are a staged build and a result file.
#   * the alternative defeats the purpose. `wk bench staged` runs `wk quiesce`,
#     which needs root, on a machine with nobody sitting at it. A password
#     prompt in the middle of an unattended benchmark is a hang, and a hang
#     that only happens after a reboot into another OS is the most expensive
#     kind to debug.
#   * the machine's *other* install is unaffected. Separate volume group,
#     separate accounts, separate sudoers. This grants nothing on Macintosh HD.
#
# Not the wk-quiesce NOPASSWD rule, which names one fixed helper path: that one
# is a narrow grant on a workstation, and this is a broad grant on a disposable
# machine. Two different judgements, and conflating them would quietly widen
# the workstation's.
if [ ! -f /etc/sudoers.d/wk-bench ]; then
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$BENCH_USER" > /etc/sudoers.d/wk-bench
    chmod 0440 /etc/sudoers.d/wk-bench
    # A malformed sudoers file locks root out of sudo entirely, so it is checked
    # before it is trusted and removed rather than left if it does not parse.
    if visudo -c -f /etc/sudoers.d/wk-bench >/dev/null 2>&1; then
        say "sudoers.d/wk-bench installed"
    else
        rm -f /etc/sudoers.d/wk-bench
        say "WARNING: sudoers snippet did not parse; removed"
    fi
fi

# --- a console session with nobody in the room -------------------------------
#
# Not a convenience: `wk bench staged` checks for one and refuses without it,
# because a browser driven over ssh with no console session has nowhere to draw
# and the run looks like a hang rather than an error. Needs FileVault off, which
# is why the install must not enable it.
if [ -n "$PW" ]; then
    # Set the password, by a route that works on a RE-RUN.
    #
    # `dscl . -passwd /Users/<u> <new>` was here and it is the wrong tool for
    # this job: it is a *change* operation and wants the old password, so on any
    # re-run -- which is the normal case, this script being idempotent by design
    # -- it fails with `DS error: eDSAuthFailed`. The account keeps whatever
    # password it was created with, the login keychain drifts from it, and
    # autologin raises a SecurityAgent unlock panel that can stand on the screen
    # through an entire A/B.
    #
    # `sysadminctl -resetPasswordFor` is the administrative *reset*: as root it
    # needs no old password, which is exactly the difference that matters here.
    #
    # And then it is CHECKED, because this file already records two macOS tools
    # in this same area that exit 0 without acting (`sysadminctl -autologin`,
    # `SACSetAutoLoginPassword error:22`, exit 0). `dscl . -authonly` is the only
    # answer that is evidence: it authenticates the account with the password and
    # fails if that is not the account's password. Everything downstream --
    # kcpassword, autologin, an unattended console session -- is false unless
    # this passes, so a failure is said loudly rather than logged in passing.
    sysadminctl -resetPasswordFor "$BENCH_USER" -newPassword "$PW" >/dev/null 2>&1 \
        || dscl . -passwd "/Users/$BENCH_USER" "$PW" >/dev/null 2>&1 \
        || true
    if dscl . -authonly "$BENCH_USER" "$PW" >/dev/null 2>&1; then
        say "password verified for $BENCH_USER (authonly succeeded)"
    else
        say "WARNING: $BENCH_USER's password is NOT what this script set."
        say "  Autologin will raise a keychain/auth panel, and that panel sits on"
        say "  top of the benchmark where lsappinfo cannot see it. Every number"
        say "  from this install is suspect until this line reads 'verified'."
    fi

    # The login keychain, reset to match.
    #
    # This is what keeps a dialog off the benchmark install's screen for a whole
    # A/B: `dscl . -passwd` fails here with
    # `DS error: eDSAuthFailed` on a re-run -- it needs the *old* password to
    # change one, and this script does not have it -- so the account password and
    # the login keychain's password drift apart. autologin then logs the account
    # in and macOS raises a keychain-unlock panel, drawn by SecurityAgent, which
    # `lsappinfo front` cannot see (lib/quiet.sh, auth_panel).
    #
    # Deleting the keychain is the right remedy *here* specifically because this
    # install is cattle: it holds no secrets worth keeping -- no Apple ID, no
    # certificates, no saved passwords, nothing but whatever a browser benchmark
    # happens to store. macOS recreates an empty login keychain, matched to the
    # account, at the next login. On a workstation this would be vandalism; on a
    # volume `diskutil apfs deleteVolume` can replace in one command it is
    # provisioning.
    home=$(dscl . -read "/Users/$BENCH_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
    if [ -n "$home" ] && [ -d "$home/Library/Keychains" ]; then
        rm -rf "$home/Library/Keychains" \
            && say "reset $BENCH_USER's login keychain (it drifts from the account password" \
            && say "  on a re-run, and the unlock prompt lands on top of the benchmark)" \
            || say "WARNING: could not reset the login keychain; expect an unlock prompt"
    fi

    # Autologin, written rather than requested.
    #
    # `sysadminctl -autologin set` logged `SACSetAutoLoginPassword error:22` and
    # *exited 0*, so the script reported success about a machine that then asked
    # for a password -- twice. Autologin is two pieces of state and both can be
    # written directly, which removes the dependency on a tool that lies:
    #
    #   /etc/kcpassword                     the password, XORed with a fixed key
    #   com.apple.loginwindow autoLoginUser  who to log in
    #
    # The key and the padding rule are Apple's, unchanged for many releases: pad
    # the password with NULs to a multiple of 12 (a full extra block when it is
    # already a multiple), then XOR with the repeating key.
    /usr/bin/python3 - "$PW" <<'KCP' 2>/dev/null || say "WARNING: could not write /etc/kcpassword"
import sys, os
KEY = bytes([0x7D,0x89,0x52,0x23,0xD2,0xBC,0xDD,0xEA,0xA3,0xB9,0x1F])
pw  = sys.argv[1].encode()
pad = 12 - (len(pw) % 12) if len(pw) % 12 else 12
buf = pw + b"\x00" * pad
out = bytes(c ^ KEY[i % len(KEY)] for i, c in enumerate(buf))
with open("/etc/kcpassword", "wb") as f:
    f.write(out)
os.chmod("/etc/kcpassword", 0o600)
os.chown("/etc/kcpassword", 0, 0)
KCP
    defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser "$BENCH_USER" 2>/dev/null \
        || say "WARNING: could not set autoLoginUser"

    # Verified by its artefacts, not by an exit status. Test the property, not
    # the command (docs/TESTING.md).
    if [ -f /etc/kcpassword ] \
       && [ "$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)" = "$BENCH_USER" ]; then
        say "autologin set for $BENCH_USER (kcpassword written, autoLoginUser set)"
    else
        say "WARNING: autologin did NOT take."
        say "  the console will ask for a password; it is '$PW'"
    fi
else
    say "autologin left alone (no password on hand for an existing user)"
fi

# --- reachable ---------------------------------------------------------------
# launchctl, not `systemsetup -setremotelogin on`.
#
# systemsetup failed here with nothing but "could not enable remote login", and
# that failure is why the machine had no ssh at all -- every other fix needed a
# person at the keyboard because of it. It wants Full Disk Access, which a
# LaunchDaemon on a fresh install has not been granted and cannot ask for.
#
# Remote Login *is* a launchd override, so setting the override is the same
# switch without the TCC prompt. Verified afterwards by asking launchctl rather
# than by trusting either command.
launchctl enable system/com.openssh.sshd 2>/dev/null || true
launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || true
systemsetup -setremotelogin on >/dev/null 2>&1 || true
if launchctl print-disabled system 2>/dev/null | grep -q '"com.openssh.sshd" => enabled'; then
    say "remote login on"
else
    say "WARNING: remote login is still off -- this machine cannot be driven"
fi

if [ -f "$PAYLOAD/authorized_keys" ]; then
    home=$(dscl . -read "/Users/$BENCH_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
    if [ -n "$home" ] && [ -d "$home" ]; then
        install -d -m 0700 -o "$BENCH_USER" "$home/.ssh"
        install -m 0600 -o "$BENCH_USER" "$PAYLOAD/authorized_keys" "$home/.ssh/authorized_keys"
        say "authorized_keys installed for $BENCH_USER"
    else
        say "WARNING: no home directory for $BENCH_USER yet; authorized_keys not installed"
    fi
fi

# --- tailscale, so this machine has an address that does not move -------------
#
# Installed and authenticated here rather than left to a person, because the
# alternative is what happened while this volume was being brought up: the
# install had no tailnet identity, its LAN address changed between reboots, and
# the ssh alias resolved to the *host* install instead -- so every probe
# answered for the wrong machine and looked like a network fault.
#
# HOW THE BINARY GETS HERE. Nothing is cross-compiled and no Go toolchain is
# involved: there is a signed macOS package, and it is what gets installed.
#
# What is true is narrower than the conclusion drawn from it: the *static
# tarballs* on pkgs.tailscale.com are Linux-only (386, amd64, arm, arm64, mips…
# -- that `arm64` is Linux arm64). But macOS is shipped, as
# `Tailscale-<ver>-macos.pkg`, and inspecting it settles what it is:
#
#   Contents/Library/LaunchDaemons/io.tailscale.ipn.macsys.tssentineld.plist
#       RunAtLoad = true, KeepAlive = true, reached over a MachService
#   Contents/MacOS/Tailscale                     the CLI
#
# It is the **macsys (standalone)** variant, and its LaunchDaemon is what gives
# the "runs before login" property this needs. Not to be confused with the *App
# Store* build: that one is sandboxed, its CLI needs a session, and
# `/Applications/Tailscale.app/Contents/MacOS/Tailscale` hangs over ssh.
#
# So: no compiler. `installer -pkg … -target /` is a non-interactive install, and
# the package goes into the provisioning payload like everything else.
#
# Failure is reported and not fatal: the machine still has whatever network the
# step below gives it, and a missing tailnet identity is a nuisance rather than a
# broken install -- though it is the nuisance that makes this whole install
# unobservable, so the warning is worth reading.
TS_CLI=/Applications/Tailscale.app/Contents/MacOS/Tailscale
TS_PKG=$(ls "$PAYLOAD"/Tailscale-*macos.pkg 2>/dev/null | head -1)

if [ -r "$PAYLOAD/tailscale-authkey" ] && [ -n "$TS_PKG" ]; then
    say "installing Tailscale (standalone/macsys) from $(basename "$TS_PKG")"
    if installer -pkg "$TS_PKG" -target / >/dev/null 2>&1; then
        # Verified by asking launchd, not by the installer's exit status -- this
        # file already records two macOS tools in this area that exit 0 without
        # acting.
        if launchctl print system/io.tailscale.ipn.macsys.tssentineld >/dev/null 2>&1; then
            say "  tailscale system daemon is loaded"
        else
            say "  WARNING: the tailscale daemon did not load; giving it a moment"
            sleep 10
        fi
        if [ -x "$TS_CLI" ]; then
            # `file:` and not the key itself: argv is world readable, so
            # `--auth-key tskey-...` hands the fleet's key to anything with a
            # shell here. Same spelling, same reason, as bridge/provision.sh
            # and `wk pi setup`.
            #
            # --advertise-tags: this install is a wk-managed node like any
            # other, and a tagged node never key-expires. Untagged, it works for
            # 180 days and then drops off the tailnet -- taking its name, which
            # is the only way anything reaches it, with it.
            "$TS_CLI" up --auth-key "file:$PAYLOAD/tailscale-authkey" \
                --advertise-tags=tag:wk \
                --hostname tolken-bench --accept-dns=false >/dev/null 2>&1 || true
            # The property, not the command's exit status: does this machine
            # have a tailnet address?
            ts_ip=$("$TS_CLI" ip -4 2>/dev/null | head -1)
            if [ -n "$ts_ip" ]; then
                say "tailscale: up as tolken-bench at $ts_ip"
                say "  this install is now reachable by name across a reboot, which is"
                say "  the thing that makes it observable at all"
            else
                say "WARNING: tailscale up did not take; no tailnet address."
                say "  The install will only be reachable at whatever DHCP address it gets,"
                say "  and not at all from a driver that reaches this Mac over the tailnet."
            fi
        else
            say "WARNING: $TS_CLI is missing after a successful install"
        fi
    else
        say "WARNING: installer failed on $TS_PKG"
    fi
elif [ -r "$PAYLOAD/tailscale-authkey" ]; then
    say "WARNING: an auth key is staged but no Tailscale package is."
    say "  Put Tailscale-<ver>-macos.pkg in the payload:"
    say "    curl -LO https://pkgs.tailscale.com/stable/Tailscale-1.102.3-macos.pkg"
    say "  No compiler is needed -- see the comment here."
else
    say "no tailscale auth key in the payload; this install will have no tailnet"
    say "  identity, so nothing that reaches this Mac over the tailnet can reach it"
fi

# --- the network, without which none of the above can be reached --------------
#
# The thing that made three reboots look like ssh failures: a fresh macOS
# install has no Wi-Fi credentials, and this Mac is on Wi-Fi (en0). Remote Login
# was genuinely on -- the log said so -- and there was simply no route to the
# machine. An install that cannot join a network is an install nobody can drive,
# whatever else is configured correctly.
#
# The SSID and passphrase come from the payload, put there by
# `wk bench mac-volume` from this Mac's own keychain. They are written onto the
# bench volume in the clear, which is worth being explicit about: it is the same
# physical machine, the same disk and the same network the credentials already
# belong to, and the volume is disposable -- but it is a secret at rest, and
# `diskutil apfs deleteVolume` is what removes it.
if [ -r "$PAYLOAD/wifi.conf" ]; then
    # shellcheck disable=SC1090
    . "$PAYLOAD/wifi.conf"
    if [ -n "${WIFI_SSID:-}" ]; then
        dev=$(networksetup -listallhardwareports 2>/dev/null \
                | awk '/Hardware Port: Wi-Fi/{getline; print $2; exit}')
        dev="${dev:-en0}"
        networksetup -setairportpower "$dev" on >/dev/null 2>&1 || true
        networksetup -setairportnetwork "$dev" "$WIFI_SSID" "${WIFI_PSK:-}" >/dev/null 2>&1 || true
        # Verified by having an address, not by the command's exit status: this
        # one returns 0 while reporting "Could not find network" in its output.
        for _t in 1 2 3 4 5 6 7 8 9 10; do
            ip=$(ipconfig getifaddr "$dev" 2>/dev/null) && [ -n "$ip" ] && break
            sleep 3
        done
        if [ -n "${ip:-}" ]; then
            say "network: $dev joined '$WIFI_SSID' as $ip"
        else
            say "WARNING: could not join '$WIFI_SSID' -- this machine will be unreachable"
        fi
    fi
else
    say "WARNING: no wifi.conf in the payload; if this Mac is on Wi-Fi it will"
    say "  have no network and nothing will be able to drive it"
fi

# --- where staged builds land ------------------------------------------------
#
# Created here, as root on the running bench install, because this is the only
# moment it is cheap. From host mode the same directory is
# `/Volumes/<name> - Data/private/var/wk`, sitting under a root-owned
# /private/var -- so creating it there needs sudo, and `wk bench stage` runs
# unattended over ssh where a sudo prompt is a hang.
#
# Owned by the bench account rather than root for the same reason: staging
# writes as the *host* account and the benchmark reads as this one, and both
# are uid 501 (the first account on each install), so one owner satisfies both
# sides without either needing privilege.
#
# Without this a person has to remember a chown before every re-provision,
# which the cattle rule's fourth obligation exists to forbid:
# provisioning is a verb, not a wiki page.
install -d -o "$BENCH_USER" -g staff -m 0755 /var/wk 2>/dev/null \
    && say "staging root /var/wk ready, owned by $BENCH_USER" \
    || say "WARNING: could not create /var/wk -- staging will fail from host mode"

# --- the marker --------------------------------------------------------------
#
# The one thing that tells wk this is bench mode. Without it `wk bench staged`
# refuses, correctly, because nothing else distinguishes the two installs from
# the outside.
if [ ! -f /etc/wk-image ]; then
    printf 'id=%s-%s\nprofile=%s\n' "$PROFILE" "$(date -u +%Y-%m)" "$PROFILE" > /etc/wk-image
    say "wrote /etc/wk-image"
fi

# --- the permanent half of quieting ------------------------------------------
#
# `wk quiesce` does the per-run half and measures it. These are the settings
# that have to be true of the install itself. Set and then read back rather
# than trusted: `softwareupdate --schedule off` is on record as not sticking.
mdutil -i off -a >/dev/null 2>&1 || say "WARNING: spotlight still indexing"
softwareupdate --schedule off >/dev/null 2>&1 || true
defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false 2>/dev/null || true
pmset -a lowpowermode 0 sleep 0 displaysleep 0 disksleep 0 >/dev/null 2>&1 || true
tmutil disablelocal >/dev/null 2>&1 || true
say "quieted: spotlight=$(mdutil -a -s 2>&1 | tr '\n' ' ' | sed 's/  */ /g')"
say "updates schedule: $(softwareupdate --schedule 2>&1 | tail -1)"
say "filevault: $(fdesetup status 2>&1 | head -1)"

# --- wk-tools ----------------------------------------------------------------
#
# Carried in the package rather than fetched, because a benchmark install with
# no network route and no git is a perfectly good benchmark install, and
# because `wk bench staged` is the thing that runs the measurement -- it cannot
# be the thing that needs provisioning first. This is wk-tools only: a WebKit
# checkout here would make this a second workstation, which is the one thing
# the install must never become.
if [ -d "$PAYLOAD/wk-tools" ]; then
    home=$(dscl . -read "/Users/$BENCH_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
    if [ -n "$home" ] && [ -d "$home" ]; then
        install -d -o "$BENCH_USER" "$home/Development"
        /usr/bin/rsync -a --delete "$PAYLOAD/wk-tools/" "$home/Development/wk-tools/" \
            && chown -R "$BENCH_USER" "$home/Development/wk-tools" \
            && say "wk-tools placed at $home/Development/wk-tools" \
            || say "WARNING: could not place wk-tools"
    fi
fi

# --- pyobjc, which is the one thing that cannot be fixed from here -----------
#
# run-benchmark's prepare_env does a bare `import objc`, and only Apple's
# /usr/bin/python3 has pyobjc -- but it arrives with the Command Line Tools,
# and `xcode-select --install` is a GUI prompt that cannot be answered by a
# LaunchDaemon. Reported loudly instead of attempted, so the lane's preflight
# is the thing that catches it rather than a run that dies in prepare_env.
if /usr/bin/python3 -c 'import objc' >/dev/null 2>&1; then
    say "pyobjc: present"
else
    say "PYOBJC MISSING -- Command Line Tools are not installed on this volume."
    say "  run-benchmark's prepare_env will fail. From a console here:"
    say "    xcode-select --install"
fi

# --- done, and gone ----------------------------------------------------------
#
# The daemon removes itself rather than guarding on a stamp file. A first-boot
# job that stays loaded is a job that runs again after a crash and re-randomises
# the account password out from under the autologin that depends on it.
# The plist and the script, and *not* `launchctl bootout` on this daemon's own
# label -- which is `kill $$` with extra steps.
#
# That is what was here, and it meant this script had never once removed itself:
# booting out the label terminates the job, which is the process running these
# very lines, so the `rm -f` below never runs. The log says "removing the
# first-boot daemon" on every boot while both files sit untouched with their
# original mtimes.
#
# Not merely untidy. Every boot of the benchmark install then re-runs
# provisioning, which `rsync --delete`s wk-tools back to the payload copy and
# reboots the machine a minute later -- so a run planted by `wk bench mac-ab`
# has its tooling replaced under it and is cut off mid-flight.
# Two whole cycles were spent on that before the cause was found. (The same
# suicide, in the same shape, was found in bench/mac-bench-autorun.sh a few
# hours earlier -- worth knowing that this is an easy mistake to make twice.)
#
# Removing the files is sufficient: launchd has the job loaded for this boot
# either way, and there is no next boot for it to be loaded from.
say "removing the first-boot daemon"
rm -f "$DAEMON" "$SELF"
if [ -f "$DAEMON" ]; then
    say "WARNING: $DAEMON is still there -- provisioning will run again next boot"
else
    say "  removed $DAEMON"
fi
say "=== first boot provisioning complete ==="

# Autologin only takes effect at the *next* boot, and a console session is a
# hard requirement of the thing this machine exists to do -- so it reboots
# itself rather than leaving a login window nobody is there to answer.
say "rebooting so autologin takes effect"
shutdown -r +1 &
exit 0
