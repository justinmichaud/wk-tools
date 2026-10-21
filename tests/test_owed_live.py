"""Owed behaviours docs/PLAN.md lists and nothing has proven yet: one stub per row, marked owed, so the runner counts
each and fails when one passes without its mark removed. A live row runs in the live tier; a unit row in the unit tier.
Proving a row means writing its body in the module its name says (tests/test_<x>.py) and deleting the stub here."""
import unittest

from tests import support


class TestOwedLiveRows(unittest.TestCase):
    wk_tier = "live"

    @support.owed('live killpoints[setup]: ./setup killed after any effect and rerun converges')
    def test_killpoints_setup(self):
        """`live killpoints[setup]`"""
        self.fail("not yet proven: ./setup killed after any effect and rerun converges")

    @support.owed('live profile.modes[<mode>]: wk run --profile in every mode, and --fetch')
    def test_profile_modes_mode(self):
        """`live profile.modes[<mode>]`"""
        self.fail("not yet proven: wk run --profile in every mode, and --fetch")

    @support.owed('live profile.modes[sysprof]: wk run --profile=sysprof in a container, JS frames named from the JIT dump')
    def test_profile_modes_sysprof(self):
        """`live profile.modes[sysprof]`"""
        self.fail("not yet proven: wk run --profile=sysprof in a container, JS frames named from the JIT dump")

    @support.owed('live run.rr_record_replay: wk run --rr, wk gui --rr and wk run --replay in a container')
    def test_run_rr_record_replay(self):
        """`live run.rr_record_replay`"""
        self.fail("not yet proven: wk run --rr, wk gui --rr and wk run --replay in a container")

    @support.owed('live bench.options_ab: wk bench run --a-args/--b-args in alternating rounds, compared per subtest')
    def test_bench_options_ab(self):
        """`live bench.options_ab`"""
        self.fail("not yet proven: wk bench run --a-args/--b-args in alternating rounds, compared per subtest")

    @support.owed('live pr.report: wk pr report against the real GitHub API')
    def test_pr_report(self):
        """`live pr.report`"""
        self.fail("not yet proven: wk pr report against the real GitHub API")

    @support.owed('live boot[rpi4]: rpi4 boots its bench system reliably and --back reaches the rescue')
    def test_boot_rpi4(self):
        """`live boot[rpi4]`"""
        self.fail("not yet proven: rpi4 boots its bench system reliably and --back reaches the rescue")

    @support.owed("live bench[mbp]: the Mac's bench volume runs the whole lifecycle")
    def test_bench_mbp(self):
        """`live bench[mbp]`"""
        self.fail("not yet proven: the Mac's bench volume runs the whole lifecycle")

    @support.owed('live bench.evidence[<board>]: a failed leg on a board leaves its evidence readable')
    def test_bench_evidence_board(self):
        """`live bench.evidence[<board>]`"""
        self.fail("not yet proven: a failed leg on a board leaves its evidence readable")

    @support.owed('live logs.follow[<place>]: wk status <ws> --log -f follows a live build on any place')
    def test_logs_follow_place(self):
        """`live logs.follow[<place>]`"""
        self.fail("not yet proven: wk status <ws> --log -f follows a live build on any place")

    @support.owed('live start.roundtrip: bare wk stop then wk start returns every workspace to running')
    def test_start_roundtrip(self):
        """`live start.roundtrip`"""
        self.fail("not yet proven: bare wk stop then wk start returns every workspace to running")

    @support.owed('live doctor.fix_clears_line: wk doctor on a fresh machine reports ok and each fix clears its line')
    def test_doctor_fix_clears_line(self):
        """`live doctor.fix_clears_line`"""
        self.fail("not yet proven: wk doctor on a fresh machine reports ok and each fix clears its line")

    @support.owed("live doctor.bench_readiness[mbp]: wk doctor reports a bench machine's SIP and quieting as wk quiesce status does")
    def test_doctor_bench_readiness_mbp(self):
        """`live doctor.bench_readiness[mbp]`"""
        self.fail("not yet proven: wk doctor reports a bench machine's SIP and quieting as wk quiesce status does")

    @support.owed('live doctor.reprovision[<machine>]: a machine rebuilt from the repo alone sees the whole fleet')
    def test_doctor_reprovision_machine(self):
        """`live doctor.reprovision[<machine>]`"""
        self.fail("not yet proven: a machine rebuilt from the repo alone sees the whole fleet")

    @support.owed('live setup.completes[<host>]: ./setup completes on every host OS and installs every helper')
    def test_setup_completes_host(self):
        """`live setup.completes[<host>]`"""
        self.fail("not yet proven: ./setup completes on every host OS and installs every helper")

    @support.owed("live rm.peer[<machine>]: a peer's workspace is refused here for creation and removed from here")
    def test_rm_peer_machine(self):
        """`live rm.peer[<machine>]`"""
        self.fail("not yet proven: a peer's workspace is refused here for creation and removed from here")

    @support.owed('live build.box_record: a build on a box keeps its record there and every workstation reads it')
    def test_build_box_record(self):
        """`live build.box_record`"""
        self.fail("not yet proven: a build on a box keeps its record there and every workstation reads it")

    @support.owed('live build.babysit_e2e: wk build --babysit as a task, each end by name')
    def test_build_babysit_e2e(self):
        """`live build.babysit_e2e`"""
        self.fail("not yet proven: wk build --babysit as a task, each end by name")

    @support.owed('live build.preset[<preset>]: every declared build preset builds on its place')
    def test_build_preset_preset(self):
        """`live build.preset[<preset>]`"""
        self.fail("not yet proven: every declared build preset builds on its place")

    @support.owed('live test.suite[<place>]: wk test runs the JSC suite and --layout on every place')
    def test_test_suite_place(self):
        """`live test.suite[<place>]`"""
        self.fail("not yet proven: wk test runs the JSC suite and --layout on every place")

    @support.owed('live run.lldb_tty: wk run --lldb gets a pty on every place')
    def test_run_lldb_tty(self):
        """`live run.lldb_tty`"""
        self.fail("not yet proven: wk run --lldb gets a pty on every place")

    @support.owed('live enter.shell: wk enter lands in a shell and --zed against a broken workspace refuses')
    def test_enter_shell(self):
        """`live enter.shell`"""
        self.fail("not yet proven: wk enter lands in a shell and --zed against a broken workspace refuses")

    @support.owed('live sync.fleet: wk sync bare, --all, --tools and WK_MIRROR_BRANCHES across the fleet')
    def test_sync_fleet(self):
        """`live sync.fleet`"""
        self.fail("not yet proven: wk sync bare, --all, --tools and WK_MIRROR_BRANCHES across the fleet")

    @support.owed('live pr.workflow: the PR workflow as one flow through the push service')
    def test_pr_workflow(self):
        """`live pr.workflow`"""
        self.fail("not yet proven: the PR workflow as one flow through the push service")

    @support.owed('live ai.walled_session: wk ai claude refuses a stopped proxy and a tool wanting the network is refused')
    def test_ai_walled_session(self):
        """`live ai.walled_session`"""
        self.fail("not yet proven: wk ai claude refuses a stopped proxy and a tool wanting the network is refused")

    @support.owed('live ai.commit_wall: git commit and git push in an agent session name the rule')
    def test_ai_commit_wall(self):
        """`live ai.commit_wall`"""
        self.fail("not yet proven: git commit and git push in an agent session name the rule")

    @support.owed("live ai.remote_control: wk ai claude starts a session Remote Control shows under the workspace's name")
    def test_ai_remote_control(self):
        """`live ai.remote_control`"""
        self.fail("not yet proven: wk ai claude starts a session Remote Control shows under the workspace's name")

    @support.owed("live zed.peer: wk zed reaches a peer's workspace one hop further")
    def test_zed_peer(self):
        """`live zed.peer`"""
        self.fail("not yet proven: wk zed reaches a peer's workspace one hop further")

    @support.owed('live key.election[<peer>]: wk key setup elects across workstations and a second run moves nothing')
    def test_key_election_peer(self):
        """`live key.election[<peer>]`"""
        self.fail("not yet proven: wk key setup elects across workstations and a second run moves nothing")

    @support.owed("live push.from_box: a push for a box's workspace goes through this machine's mirror and deploy key")
    def test_push_from_box(self):
        """`live push.from_box`"""
        self.fail("not yet proven: a push for a box's workspace goes through this machine's mirror and deploy key")

    @support.owed('live new.box_record[buildbox4]: wk new --on buildbox4 keeps the creation record on the box')
    def test_new_box_record_buildbox4(self):
        """`live new.box_record[buildbox4]`"""
        self.fail("not yet proven: wk new --on buildbox4 keeps the creation record on the box")

    @support.owed('live handover.tools_level[buildbox4]: a hand-over refused at a differing wk-tools commit, --force crossing it')
    def test_handover_tools_level_buildbox4(self):
        """`live handover.tools_level[buildbox4]`"""
        self.fail("not yet proven: a hand-over refused at a differing wk-tools commit, --force crossing it")

    @support.owed('live backup.roundtrip: wk key backup then ./setup round-trips with no spurious change')
    def test_backup_roundtrip(self):
        """`live backup.roundtrip`"""
        self.fail("not yet proven: wk key backup then ./setup round-trips with no spurious change")

    @support.owed('live sudo.require[<machine>]: wk key sudo setup installs its rule and no machine holds a wider grant')
    def test_sudo_require_machine(self):
        """`live sudo.require[<machine>]`"""
        self.fail("not yet proven: wk key sudo setup installs its rule and no machine holds a wider grant")

    @support.owed('live session.modes[moose]: wk quiesce session reaches the asked mode from any half-state on moose')
    def test_session_modes_moose(self):
        """`live session.modes[moose]`"""
        self.fail("not yet proven: wk quiesce session reaches the asked mode from any half-state on moose")

    @support.owed('live bench.pins_cores[<place>]: a bench run pins the cores it records in a container and a guest')
    def test_bench_pins_cores_place(self):
        """`live bench.pins_cores[<place>]`"""
        self.fail("not yet proven: a bench run pins the cores it records in a container and a guest")

    @support.owed('live bench.compare: wk bench compare gives per-subtest confidence intervals')
    def test_bench_compare(self):
        """`live bench.compare`"""
        self.fail("not yet proven: wk bench compare gives per-subtest confidence intervals")

    @support.owed('live bench.first_run_after_stage[mbp]: a first run after a stage is no slower than the second')
    def test_bench_first_run_after_stage_mbp(self):
        """`live bench.first_run_after_stage[mbp]`"""
        self.fail("not yet proven: a first run after a stage is no slower than the second")

    @support.owed('live bench.leg_completes[<board>]: a board leg starts from a cleared cache and reads its warmup probes')
    def test_bench_leg_completes_board(self):
        """`live bench.leg_completes[<board>]`"""
        self.fail("not yet proven: a board leg starts from a cleared cache and reads its warmup probes")

    @support.owed("live bench.routed_deploy: a deploy routed through a macOS workstation's podman VM reaches a board")
    def test_bench_routed_deploy(self):
        """`live bench.routed_deploy`"""
        self.fail("not yet proven: a deploy routed through a macOS workstation's podman VM reaches a board")

    @support.owed('live bench.pgo_collection[<board>]: a board PGO collection records its cost, coverage and rendering facts')
    def test_bench_pgo_collection_board(self):
        """`live bench.pgo_collection[<board>]`"""
        self.fail("not yet proven: a board PGO collection records its cost, coverage and rendering facts")

    @support.owed('live sysimage.sstate_reuse: a second image build reuses sstate and --keep-work leaves the kernel tree')
    def test_sysimage_sstate_reuse(self):
        """`live sysimage.sstate_reuse`"""
        self.fail("not yet proven: a second image build reuses sstate and --keep-work leaves the kernel tree")

    @support.owed("live boot.firstboot[<board>]: a board's first boot proven on hardware")
    def test_boot_firstboot_board(self):
        """`live boot.firstboot[<board>]`"""
        self.fail("not yet proven: a board's first boot proven on hardware")

    @support.owed('live boot[rpi5]: rpi5 boots a bench system from its stick and hands itself back')
    def test_boot_rpi5(self):
        """`live boot[rpi5]`"""
        self.fail("not yet proven: rpi5 boots a bench system from its stick and hands itself back")

    @support.owed('live boot[rpi3]: rpi3 runs the shared-card two-system layout')
    def test_boot_rpi3(self):
        """`live boot[rpi3]`"""
        self.fail("not yet proven: rpi3 runs the shared-card two-system layout")

    @support.owed("live boot[rpi4].kms: rpi4's 2.52 system brings up KMS on every boot")
    def test_boot_rpi4_kms(self):
        """`live boot[rpi4].kms`"""
        self.fail("not yet proven: rpi4's 2.52 system brings up KMS on every boot")

    @support.owed("live boot[rpi4].armhf: rpi4's 32-bit buildroot system reaches userspace")
    def test_boot_rpi4_armhf(self):
        """`live boot[rpi4].armhf`"""
        self.fail("not yet proven: rpi4's 32-bit buildroot system reaches userspace")

    @support.owed('live bench.speedometer3_zram[rpi3]: rpi3 finishes Speedometer 3 with zram as its only swap')
    def test_bench_speedometer3_zram_rpi3(self):
        """`live bench.speedometer3_zram[rpi3]`"""
        self.fail("not yet proven: rpi3 finishes Speedometer 3 with zram as its only swap")

    @support.owed('live bench.clear_foreign[rpi3]: a board run stops what another login left running')
    def test_bench_clear_foreign_rpi3(self):
        """`live bench.clear_foreign[rpi3]`"""
        self.fail("not yet proven: a board run stops what another login left running")

    @support.owed('live sysimage.firmware[rpi4]: the 2.38 buildroot rpi4 image boots on its pinned firmware')
    def test_sysimage_firmware_rpi4(self):
        """`live sysimage.firmware[rpi4]`"""
        self.fail("not yet proven: the 2.38 buildroot rpi4 image boots on its pinned firmware")

    @support.owed('live sysimage.webkit_lto[<board>]: a board PGO cycle builds thin then full LTO and both run')
    def test_sysimage_webkit_lto_board(self):
        """`live sysimage.webkit_lto[<board>]`"""
        self.fail("not yet proven: a board PGO cycle builds thin then full LTO and both run")

    @support.owed('live sysimage.reflash[rpi5]: a board re-flashed from nothing by the machine holding its reader')
    def test_sysimage_reflash_rpi5(self):
        """`live sysimage.reflash[rpi5]`"""
        self.fail("not yet proven: a board re-flashed from nothing by the machine holding its reader")

    @support.owed('live pi.setup[<board>]: wk machine setup <board> from a blank card to an answering tailnet name')
    def test_pi_setup_board(self):
        """`live pi.setup[<board>]`"""
        self.fail("not yet proven: wk machine setup <board> from a blank card to an answering tailnet name")

    @support.owed('live machine_cmd.setup[<box>]: wk machine setup <box> leaves a build box in one shape')
    def test_machine_cmd_setup_box(self):
        """`live machine_cmd.setup[<box>]`"""
        self.fail("not yet proven: wk machine setup <box> leaves a build box in one shape")

    @support.owed('live doctor.device[<board>]: wk doctor <board> against a live board prints its device rows')
    def test_doctor_device_board(self):
        """`live doctor.device[<board>]`"""
        self.fail("not yet proven: wk doctor <board> against a live board prints its device rows")

    @support.owed("live bench.vm_reaches_boards: a board A/B's servers in the podman VM reach the boards")
    def test_bench_vm_reaches_boards(self):
        """`live bench.vm_reaches_boards`"""
        self.fail("not yet proven: a board A/B's servers in the podman VM reach the boards")

    @support.owed('live setup.git_lfs[macos]: ./setup --stage tools fetches git-lfs at its pin and checks its sha256')
    def test_setup_git_lfs_macos(self):
        """`live setup.git_lfs[macos]`"""
        self.fail("not yet proven: ./setup --stage tools fetches git-lfs at its pin and checks its sha256")

    @support.owed('live bench.direct_page_server[<board>]: a board reaches the page server through the tag:wk grant')
    def test_bench_direct_page_server_board(self):
        """`live bench.direct_page_server[<board>]`"""
        self.fail("not yet proven: a board reaches the page server through the tag:wk grant")

    @support.owed("live push.guest_forward: a macOS guest's git push reaches the Mac's push service")
    def test_push_guest_forward(self):
        """`live push.guest_forward`"""
        self.fail("not yet proven: a macOS guest's git push reaches the Mac's push service")

    @support.owed('live bench.speedometer3_wakelock[mbp]: Speedometer 3 under MiniBrowser runs without its wakeLock error')
    def test_bench_speedometer3_wakelock_mbp(self):
        """`live bench.speedometer3_wakelock[mbp]`"""
        self.fail("not yet proven: Speedometer 3 under MiniBrowser runs without its wakeLock error")

    @support.owed('live run.browser_profile: wk run --profile --browser --process attaches to a live MiniBrowser')
    def test_run_browser_profile(self):
        """`live run.browser_profile`"""
        self.fail("not yet proven: wk run --profile --browser --process attaches to a live MiniBrowser")

    @support.owed('live run.debug_symbols: wk run --lldb symbolicates glibc, Mesa and GTK frames in a fresh workspace')
    def test_run_debug_symbols(self):
        """`live run.debug_symbols`"""
        self.fail("not yet proven: wk run --lldb symbolicates glibc, Mesa and GTK frames in a fresh workspace")

    @support.owed('live bench.mem: wk bench mem builds with the memory patches and plots the WebProcess logs')
    def test_bench_mem(self):
        """`live bench.mem`"""
        self.fail("not yet proven: wk bench mem builds with the memory patches and plots the WebProcess logs")

    @support.owed('live mya.checkpoint_restore: a container workspace holds CAP_CHECKPOINT_RESTORE for mya and its suites pass')
    def test_mya_checkpoint_restore(self):
        """`live mya.checkpoint_restore`"""
        self.fail("not yet proven: a container workspace holds CAP_CHECKPOINT_RESTORE for mya and its suites pass")

    @support.owed('live boot[moose]: moose boots a bench system through --bootnext with the BMC reachable')
    def test_boot_moose(self):
        """`live boot[moose]`"""
        self.fail("not yet proven: moose boots a bench system through --bootnext with the BMC reachable")

    @support.owed('live bench.mac_charge_cap: the Mac in bench mode caps its battery charge')
    def test_bench_mac_charge_cap(self):
        """`live bench.mac_charge_cap`"""
        self.fail("not yet proven: the Mac in bench mode caps its battery charge")

    @support.owed('live bench.mac_screen: the Mac turns its display off between legs and on for one, reported by quiesce status')
    def test_bench_mac_screen(self):
        """`live bench.mac_screen`"""
        self.fail("not yet proven: the Mac turns its display off between legs and on for one, reported by quiesce status")

    @support.owed('live bench.linux_variance: the variance factors cross and pair on Linux as on the Mac')
    def test_bench_linux_variance(self):
        """`live bench.linux_variance`"""
        self.fail("not yet proven: the variance factors cross and pair on Linux as on the Mac")

    @support.owed('live bench.binary_identifier: both arms carry an identifier read back from the running binary')
    def test_bench_binary_identifier(self):
        """`live bench.binary_identifier`"""
        self.fail("not yet proven: both arms carry an identifier read back from the running binary")

    @support.owed('live clangd.measure: the clangd flags are measured against a real checkout')
    def test_clangd_measure(self):
        """`live clangd.measure`"""
        self.fail("not yet proven: the clangd flags are measured against a real checkout")

    @support.owed('live pi.session: pi runs on a macOS guest and a build box and its TUI in a container')
    def test_pi_session(self):
        """`live pi.session`"""
        self.fail("not yet proven: pi runs on a macOS guest and a build box and its TUI in a container")

    @support.owed('live run.inspector_remote: Web Inspector reaches a headless workspace over an ssh forward')
    def test_run_inspector_remote(self):
        """`live run.inspector_remote`"""
        self.fail("not yet proven: Web Inspector reaches a headless workspace over an ssh forward")

    @support.owed('live build.sysroot: wk build --sysroot cross-builds for an Ubuntu rpi3 and runs on the board')
    def test_build_sysroot(self):
        """`live build.sysroot`"""
        self.fail("not yet proven: wk build --sysroot cross-builds for an Ubuntu rpi3 and runs on the board")

    @support.owed("live setup.vm_gitconfig: ./setup converges the podman machine's git identity")
    def test_setup_vm_gitconfig(self):
        """`live setup.vm_gitconfig`"""
        self.fail("not yet proven: ./setup converges the podman machine's git identity")

    @support.owed('live sync.fresh_workspace_wired: a fresh container workspace reports itself wired right')
    def test_sync_fresh_workspace_wired(self):
        """`live sync.fresh_workspace_wired`"""
        self.fail("not yet proven: a fresh container workspace reports itself wired right")

    @support.owed('live remote.probe_macos_bsd: the macOS and BSD remote probe branches hold against a live remote')
    def test_remote_probe_macos_bsd(self):
        """`live remote.probe_macos_bsd`"""
        self.fail("not yet proven: the macOS and BSD remote probe branches hold against a live remote")


class TestOwedUnitRows(unittest.TestCase):

    @support.owed("unit bench.report_and_cost: a report labels an instrumented leg's time and leaves no stray settle directory")
    def test_bench_report_and_cost(self):
        """`unit bench.report_and_cost`"""
        self.fail("not yet written: a report labels an instrumented leg's time and leaves no stray settle directory")

    @support.owed('unit mya.setcap_after_build: wk build sets cap_checkpoint_restore on bin/mya and bin/testLibJSCTools after an ENABLE_MYA build')
    def test_mya_setcap_after_build(self):
        """`unit mya.setcap_after_build`"""
        self.fail("not yet written: wk build sets cap_checkpoint_restore on bin/mya and bin/testLibJSCTools after an ENABLE_MYA build")

    @support.owed("unit doctor.sip_rows: wk doctor mbp reads SIP on both installs and wk boot mbp refuses to arm while the bench volume's is on")
    def test_doctor_sip_rows(self):
        """`unit doctor.sip_rows`"""
        self.fail("not yet written: wk doctor mbp reads SIP on both installs and wk boot mbp refuses to arm while the bench volume's is on")

    @support.owed('unit bench.variance_pairing: the report pairs rounds across layout, env size, path length and shared cache, and names a side whose spread grows')
    def test_bench_variance_pairing(self):
        """`unit bench.variance_pairing`"""
        self.fail("not yet written: the report pairs rounds across layout, env size, path length and shared cache, and names a side whose spread grows")

    @support.owed('unit agents.models_per_machine: one ~/.pi/agent/models.json per machine replaces the one wk new writes per workspace')
    def test_agents_models_per_machine(self):
        """`unit agents.models_per_machine`"""
        self.fail("not yet written: one ~/.pi/agent/models.json per machine replaces the one wk new writes per workspace")

    @support.owed('unit helix.macos_languages: a macOS guest gets its own helix languages.toml')
    def test_helix_macos_languages(self):
        """`unit helix.macos_languages`"""
        self.fail("not yet written: a macOS guest gets its own helix languages.toml")

    @support.owed('unit pr.open_twice: wk pr open on a branch pushed before opens the PR and leaves the workspace wired right')
    def test_pr_open_twice(self):
        """`unit pr.open_twice`"""
        self.fail("not yet written: wk pr open on a branch pushed before opens the PR and leaves the workspace wired right")

    @support.owed('unit presets.asan_build_dir: an asan and a non-asan CMake preset build into separate directories')
    def test_presets_asan_build_dir(self):
        """`unit presets.asan_build_dir`"""
        self.fail("not yet written: an asan and a non-asan CMake preset build into separate directories")
