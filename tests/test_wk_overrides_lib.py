"""WK_* override audit -- lib/, boot/ and targets/ files owned by this
agent (see the wk-overrides audit: every WK_* read with a default is
documented where the user meets it and covered by a test, or removed).

Each test below either (a) drives a real override end to end through the
function that reads it, with no hardware and no real machine touched -- a
fake `lsappinfo`/PATH stub, a stubbed driver function, a scratch file -- or
(b) is a cheap regression guard on a source-level fact (two files must not
disagree on one name's default). Vars this agent decided to REMOVE
(WK_IMAGE_ARMHF, WK_DETACH_POLL_SECONDS, WK_SWEEP_TIMEOUT, WK_RPI3_SSH,
WK_RPI4_SSH, WK_MAC_SSH, WK_MAC_BENCH_SSH) are checked absent, so a later
re-add is a decision, not a drift.

Run: python3 -m unittest tests.test_wk_overrides_lib -v
"""
import os
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, fake_workspace, stub_path


def _src(*parts):
    return (REPO.joinpath(*parts)).read_text()


class TestRemovedOverridesStayRemoved(unittest.TestCase):
    """Source-level regression guards: an override this agent removed because
    nothing used it should not silently come back."""

    def test_wk_image_armhf_is_pinned_not_overridable(self):
        self.assertNotIn("WK_IMAGE_ARMHF:-", _src("lib", "arch.sh"))

    def test_wk_detach_poll_seconds_removed(self):
        self.assertNotIn("WK_DETACH_POLL_SECONDS", _src("lib", "detach.sh"))

    def test_wk_sweep_timeout_removed(self):
        self.assertNotIn("WK_SWEEP_TIMEOUT", _src("lib", "reach.sh"))

    def test_fleet_conf_ssh_names_not_overridable(self):
        """boot/machines/*.conf: a fleet device is renamed by editing the
        conf, not by an environment variable nothing sets."""
        self.assertNotIn("WK_RPI3_SSH", _src("boot", "machines", "rpi3.conf"))
        self.assertNotIn("WK_RPI4_SSH", _src("boot", "machines", "rpi4.conf"))
        mbp = _src("boot", "machines", "mbp.conf")
        self.assertNotIn("WK_MAC_SSH", mbp)
        self.assertNotIn("WK_MAC_BENCH_SSH", mbp)
        # WK_BENCH_VOLUME survives: tests/test_quick.py drives it.
        self.assertIn("WK_BENCH_VOLUME", mbp)


class TestSharedTimingDefaultsAgree(unittest.TestCase):
    """CLAUDE.md: 'same name read in several files: one default.' lib/detach.sh
    and lib/watchdog.sh both read WK_HEARTBEAT_SECONDS and WK_STALL_SECONDS,
    and must agree on the default."""

    def test_stall_and_heartbeat_seconds_share_one_default(self):
        detach = _src("lib", "detach.sh")
        watchdog = _src("lib", "watchdog.sh")
        self.assertIn("WK_STALL_SECONDS:-300", detach)
        self.assertIn("WK_STALL_SECONDS:-300", watchdog)
        self.assertIn("WK_HEARTBEAT_SECONDS:-300", detach)
        self.assertIn("WK_HEARTBEAT_SECONDS:-300", watchdog)


class TestSshTimeoutReadInOnePlace(unittest.TestCase):
    """lib/common.sh's wk_ssh_timeout() is the one place the
    WK_SSH_TIMEOUT default lives; every caller reads it through that
    function instead of repeating `${WK_SSH_TIMEOUT:-10}`."""

    def test_no_other_file_reads_the_default_inline(self):
        owner = REPO / "lib" / "common.sh"
        offenders = []
        for top in ("cmd", "lib", "boot", "image", "host", "targets", "bench"):
            d = REPO / top
            if not d.is_dir():
                continue
            for path in d.rglob("*"):
                if not path.is_file() or path == owner:
                    continue
                if "WK_SSH_TIMEOUT:-" in path.read_text(errors="ignore"):
                    offenders.append(str(path.relative_to(REPO)))
        wk = REPO / "wk"
        if wk.is_file() and "WK_SSH_TIMEOUT:-" in wk.read_text(errors="ignore"):
            offenders.append("wk")
        self.assertEqual(offenders, [], f"WK_SSH_TIMEOUT:- read inline outside lib/common.sh: {offenders}")


class TestCommonLib(WkTest):
    def test_wk_ssh_timeout_default_and_override(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
[ "$(wk_ssh_timeout)" = 10 ] || { echo "default: got $(wk_ssh_timeout)"; exit 1; }
WK_SSH_TIMEOUT=42
[ "$(wk_ssh_timeout)" = 42 ] || { echo "override: got $(wk_ssh_timeout)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_image_marker_overrides_the_marker_file(self):
        marker = self.tmp / "wk-image"
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
printf 'id=bench-2026-01\\nprofile=webkit-2.52\\n' > "$WK_IMAGE_MARKER"
[ "$(wk_image_id)" = "bench-2026-01" ] || { echo "id: $(wk_image_id)"; exit 1; }
[ "$(wk_image_profile)" = "webkit-2.52" ] || { echo "profile: $(wk_image_profile)"; exit 1; }
in_bench_mode || { echo "in_bench_mode should be true"; exit 1; }
echo PASS
''',
            env={"WK_IMAGE_MARKER": str(marker)},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_session_mode_file_overrides_the_marker_file(self):
        marker = self.tmp / "session-mode"
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
[ "$(session_mode)" = none ] || { echo "absent: $(session_mode)"; exit 1; }
printf 'gpu\\n' > "$WK_SESSION_MODE_FILE"
[ "$(session_mode)" = gpu ] || { echo "present: $(session_mode)"; exit 1; }
echo PASS
''',
            env={"WK_SESSION_MODE_FILE": str(marker)},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_lock_dir_override_is_where_locks_actually_go(self):
        lockdir = self.tmp / "locks"
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
[ "$(wk_lock_dir)" = "$WK_LOCK_DIR" ] || exit 1
hold_lock testresource
# -L, not -e: the lock is a symlink whose *target* is a payload string
# (pid=... tok=...), not a real path, so -e (which follows the link) is
# always false for one of these.
[ -L "$(wk_lock_dir)/testresource@"*.lock ] || { echo "no lock file in $(wk_lock_dir)"; exit 1; }
echo PASS
''',
            env={"WK_LOCK_DIR": str(lockdir)},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestQuietLib(WkTest):
    def test_wk_screen_blockers_matches_the_pipe_delimited_list(self):
        with stub_path({
            "lsappinfo": (
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  front) printf "ASN:0x0-0x1:12345\\n" ;;\n'
                '  info)  printf \'"LSDisplayName"="Software Update"\\n\' ;;\n'
                "esac\n"
            )
        }) as binp:
            cp = self.bash(
                '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/quiet.sh"
WK_SCREEN_BLOCKERS="Foo|Software Update|Bar"
out=$(screen_blocker)
[ "$out" = "Software Update" ] || { echo "matched: got '$out'"; exit 1; }
WK_SCREEN_BLOCKERS="Foo|Bar"
out2=$(screen_blocker)
[ -z "$out2" ] || { echo "unmatched: got '$out2'"; exit 1; }
echo PASS
''',
                env={"PATH": f"{binp}:{os.environ['PATH']}"},
            )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestReachLib(WkTest):
    def test_wk_tailscale_timeout_bounds_a_wedged_cli(self):
        """Mirrors tests/test_quick.py's own check of this override, for the
        function this agent's audit entry covers (wk_tailscale_peers)."""
        with stub_path({"tailscale": "#!/bin/sh\nsleep 30\n"}) as binp:
            cp = self.bash(
                '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/reach.sh"
t0=$(date +%s)
WK_TAILSCALE_TIMEOUT=1 wk_tailscale_peers >/dev/null 2>&1 || true
d=$(( $(date +%s) - t0 ))
[ "$d" -le 8 ] || { echo "a wedged tailscale CLI held the walk ${d}s"; exit 1; }
echo PASS
''',
                env={"PATH": f"{binp}:{os.environ['PATH']}"},
                timeout=20,
            )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestResourcesLib(WkTest):
    def test_wk_mb_per_job_sizes_by_mem_directly(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
WK_AVAIL_MB=5000
WK_CGROUP_CORES=100
WK_MB_PER_JOB=1000
[ "$(build_jobs)" = 5 ] || { echo "got $(build_jobs)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_cgroup_mb_clamps_available_memory(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
unset WK_AVAIL_MB
WK_CGROUP_MB=1
[ "$(avail_mem_mb)" = 1 ] || { echo "got $(avail_mem_mb)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_reserve_cores_and_mb_shrink_the_envelope(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
WK_RESERVE_CORES=3
hc=$(host_cores)
want=$(( hc - 3 )); [ "$want" -lt 1 ] && want=1
got=$(envelope_cores)
[ "$got" = "$want" ] || { echo "cores: got $got want $want"; exit 1; }

WK_RESERVE_MB=4096
hm=$(host_mem_mb)
want=$(( hm - 4096 )); [ "$want" -lt 2048 ] && want=$(( hm / 2 ))
got=$(envelope_mem_mb)
[ "$got" = "$want" ] || { echo "mb: got $got want $want"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_headless_reserve_cores_and_mb_apply_when_headless(self):
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
mkdir -p "$WK_STORE"
touch "$WK_STORE/.headless"
WK_HEADLESS_RESERVE_CORES=0
WK_HEADLESS_RESERVE_MB=111
[ "$(reserve_cores)" = 0 ] || { echo "cores: $(reserve_cores)"; exit 1; }
[ "$(reserve_mb)" = 111 ] || { echo "mb: $(reserve_mb)"; exit 1; }
echo PASS
''',
            env={"WK_STORE": str(self.tmp / "store")},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestStoreLib(WkTest):
    def test_wk_ccache_maxsize_renders_into_the_conf(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
WK_CCACHE_MAXSIZE=12G
[ "$(ccache_conf_render)" = "max_size = 12G" ] || { echo "got: $(ccache_conf_render)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_mirror_branches_overrides_the_default_main_only(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
[ "$(wk_mirror_branches)" = main ] || { echo "default: $(wk_mirror_branches)"; exit 1; }
WK_MIRROR_BRANCHES="main release/1.0"
[ "$(wk_mirror_branches)" = "main release/1.0" ] || { echo "override: $(wk_mirror_branches)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestTargetLib(WkTest):
    def test_wk_no_delegate_stops_a_fleet_walk_asking_a_remote_target(self):
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
target_all() { printf 'container\\nvm\\nbuildbox1\\n'; }
target_kind() { [ "$1" = buildbox1 ] && echo remote || echo "$1"; }
load_target() { :; }

unset WK_NO_DELEGATE WK_TARGET
out_all=$(walk_targets)
want_all=$(printf 'container\\nvm\\nbuildbox1\\n')
[ "$out_all" = "$want_all" ] || { echo "unfiltered: got [$out_all]"; exit 1; }

WK_NO_DELEGATE=1
out_nd=$(walk_targets)
want_nd=$(printf 'container\\nvm\\n')
[ "$out_nd" = "$want_nd" ] || { echo "delegate: got [$out_nd]"; exit 1; }
echo PASS
''',
            env={"WK_MARKER": str(self.tmp / "no-such-marker")},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_remote_marker_overrides_the_remote_host_marker(self):
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
in_remote_host && { echo "should be false before the marker exists"; exit 1; }
printf 'target=devbox\\nroot=/home/x/wk\\n' > "$WK_REMOTE_MARKER"
in_remote_host || { echo "should be true once the marker exists"; exit 1; }
[ "$(wk_remote_field target)" = devbox ] || { echo "field: $(wk_remote_field target)"; exit 1; }
echo PASS
''',
            env={"WK_REMOTE_MARKER": str(self.tmp / "remote-marker")},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_ready_timeout_bounds_t_ready_polling(self):
        # t_info is called inside a `$(...)` -- its own subshell -- so a
        # plain shell variable it increments never reaches the parent; a
        # file is the one counter that survives the subshell.
        counter = self.tmp / "t_info_calls"
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
WK_READY_TIMEOUT=2
t_info() { printf x >> "$COUNTER"; echo creating; }
t_ready somews && rc=0 || rc=$?
[ "$rc" = 1 ] || { echo "rc: $rc"; exit 1; }
calls=$(wc -c < "$COUNTER" | tr -d ' ')
[ "$calls" = 2 ] || { echo "calls: $calls"; exit 1; }
echo PASS
''',
            env={"COUNTER": str(counter)},
            timeout=15,
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)

    def test_wk_ready_wait_bounds_wait_ready_polling(self):
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
WK_READY_WAIT=1
ws_state() { echo creating; }
ws_status_file() { echo /nonexistent-wk-test-status; }
detach_alive() { return 0; }
out=$(wait_ready somews 2>&1) && rc=0 || rc=$?
[ "$rc" != 0 ] || { echo "expected nonzero rc, got 0: $out"; exit 1; }
printf '%s' "$out" | grep -q "after 1s" || { echo "no WK_READY_WAIT in the message: $out"; exit 1; }
echo PASS
''',
            timeout=15,
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestBootMachines(WkTest):
    def test_wk_image_host_overrides_fleet_address_resolution(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/reach.sh"
. "$WK_ROOT/boot/machines.sh"
WK_IMAGE_HOST=192.0.2.5
MACH_NAME=testmach
MACH_SSH=""
MACH_BENCH_SSH=""
MACH_MAC=""
[ "$(image_addr)" = 192.0.2.5 ] || { echo "got $(image_addr)"; exit 1; }
echo PASS
''')
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestBootMacGuest(WkTest):
    def test_wk_bench_guest_overrides_the_guest_workspace_name(self):
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/boot/mac-guest.sh"
[ "$MACH_GUEST" = my-custom-guest ] || { echo "got $MACH_GUEST"; exit 1; }
echo PASS
''',
            env={"WK_BENCH_GUEST": "my-custom-guest"},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestTargetsContainer(WkTest):
    def test_wk_container_user_overrides_the_workspace_owner(self):
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/targets/container.sh"
[ "$WKDEV_CONTAINER_USER" = customuser ] || { echo "got $WKDEV_CONTAINER_USER"; exit 1; }
echo PASS
''',
            env={"WK_CONTAINER_USER": "customuser"},
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


class TestTargetsLocal(WkTest):
    def test_wk_local_store_overrides_the_bind_mounted_store(self):
        with fake_workspace() as ws:
            store = self.tmp / "customstore"
            env = ws.env({"WK_LOCAL_STORE": str(store)})
            cp = self.bash(
                '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
. "$WK_ROOT/targets/local.sh"
echo "$WK_STORE"
''',
                env=env,
            )
        self.assertEqual(cp.stdout.strip(), str(store), cp.stdout + cp.stderr)


class TestTargetsVm(WkTest):
    """One process, one big script: every top-level WK_VM_*/WK_HOST_* default
    in targets/vm.sh is a plain `${VAR:-default}` assignment or a pure
    function, so sourcing the file (no tart, no VM, no network) is enough to
    prove every override reaches the variable it names."""

    def test_vm_driver_overrides(self):
        store = self.tmp / "vmstore"
        cp = self.bash(
            '''
. "$WK_ROOT/lib/common.sh"
WK_VM_IMAGE=custom-image:1
WK_VM_BASE=custom-base
WK_VM_MAX=5
WK_VM_USER=customuser
WK_VM_SUBNET=10.0.0
WK_VM_PROXY_PORT=9999
WK_VM_BASE_PREBUILD=jsc-release
WK_VM_DISK_GB=111
WK_VM_DISPLAY=800x600
WK_HOST_FREE_WARN_GB=50
WK_HOST_FREE_MIN_GB=10
. "$WK_ROOT/targets/vm.sh"

chk() { [ "$1" = "$2" ] || { echo "FAIL $3: got [$1] want [$2]"; exit 1; }; }
chk "$WK_VM_IMAGE" custom-image:1 WK_VM_IMAGE
chk "$WK_VM_BASE" custom-base WK_VM_BASE
chk "$WK_VM_MAX" 5 WK_VM_MAX
chk "$WK_VM_USER" customuser WK_VM_USER
chk "$WK_VM_SUBNET" 10.0.0 WK_VM_SUBNET
chk "$WK_VM_PROXY_PORT" 9999 WK_VM_PROXY_PORT
chk "$WK_VM_BASE_PREBUILD" jsc-release WK_VM_BASE_PREBUILD
chk "$WK_VM_DISK_GB" 111 WK_VM_DISK_GB
chk "$WK_VM_DISPLAY" 800x600 WK_VM_DISPLAY
chk "$WK_HOST_FREE_WARN_GB" 50 WK_HOST_FREE_WARN_GB
chk "$WK_HOST_FREE_MIN_GB" 10 WK_HOST_FREE_MIN_GB
chk "$WK_STORE" "$WK_VM_STORE" WK_VM_STORE

WK_VM_CPUS=7;      chk "$(_vm_cpus)" 7 WK_VM_CPUS
WK_VM_MEM_MB=2222; chk "$(_vm_mem_mb)" 2222 WK_VM_MEM_MB
WK_VM_BASE_CPUS=3;      chk "$(_base_cpus)" 3 WK_VM_BASE_CPUS
WK_VM_BASE_MEM_MB=4444; chk "$(_base_mem_mb)" 4444 WK_VM_BASE_MEM_MB
WK_VM_PROXY_ADDR=203.0.113.9; chk "$(_proxy_addr)" 203.0.113.9 WK_VM_PROXY_ADDR

WK_VM_UNFILTERED=1 _softnet_flags >/dev/null 2>&1
[ $? = 0 ] || { echo "FAIL WK_VM_UNFILTERED: nonzero exit"; exit 1; }

# A refusal is `die`, which is a bare `exit` -- run each one in a subshell
# so that exit ends the subshell, not this whole test script.

# WK_VM_MAX: a guest limit of 0 refuses (this host may have tart installed,
# but 0 refuses regardless of how many are actually running).
if ( WK_VM_MAX=0 _check_guest_limit ) >/dev/null 2>&1; then
  echo "FAIL WK_VM_MAX: expected refusal at 0"; exit 1
fi

# WK_HOST_FREE_MIN_GB: stub the disk-free probe so this is deterministic.
_host_free_gb() { echo 5; }
if ( WK_HOST_FREE_MIN_GB=10 _check_host_disk ) >/dev/null 2>&1; then
  echo "FAIL WK_HOST_FREE_MIN_GB: expected refusal at 5GB free / 10GB min"; exit 1
fi
_host_free_gb() { echo 999; }
( WK_HOST_FREE_MIN_GB=10 _check_host_disk ) >/dev/null 2>&1 \\
  || { echo "FAIL WK_HOST_FREE_MIN_GB: expected pass at 999GB free"; exit 1; }

# WK_VM_SHARE: bypasses the memory-budget refusal; without it the same call
# (an impossible allocation) refuses.
if ( _check_memory_budget testws 99999999 ) >/dev/null 2>&1; then
  echo "FAIL WK_VM_SHARE: expected refusal without it"; exit 1
fi
( WK_VM_SHARE=1 _check_memory_budget testws 99999999 ) >/dev/null 2>&1 \\
  || { echo "FAIL WK_VM_SHARE: expected bypass with it"; exit 1; }

echo PASS
''',
            env={"WK_VM_STORE": str(store)},
            timeout=30,
        )
        self.assertIn("PASS", cp.stdout, cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
