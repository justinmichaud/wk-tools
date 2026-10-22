"""lint.budgets: Python non-blank lines per package against docs/PLAN.md's budget table.

Each row is owed until the cut lands; `python3 -m tests.test_lint_budgets` prints today against budget.
The `fixtures` line (tests/*.py that are not test_*) is this module's addition, budgeted at 800."""
TIER = "lint"
import fnmatch
import functools
import subprocess
import unittest

from tests.support import REPO, owed
from tests.test_comment_density import python_counts

SUPPORT = ("tests/support.py", "tests/fakes.py", "tests/killpoints.py", "tests/fake_boot.py", "tests/bashlift.py", "tests/run.py")
# name: (core prefixes or paths, test_*.py globs, core budget, tests budget); the tests globs assign each module to its row.
ROWS = {
    "dispatcher": (("lib/wk/__init__.py", "lib/wk/dispatch.py", "lib/wk/decl.py", "lib/wk/act.py", "lib/wk/completion.py", "wk"),
                   ("dispatch*", "options", "wk_decl", "wk_act", "completion", "ls_halves", "owed_cli_exit_code", "owed_dispatch", "registry_free", "setup_converges", "dispatcher"), 800, 900),
    "machine": (("lib/wk/machine.py",), ("wk_machine", "linux_machine_dry_run", "macos_machine_*", "machines_dir", "machine_wins", "machine_mounts", "machine_prepare", "remote_driver", "remote", "remote_deps", "wk_sshalias"), 400, 500),
    "places": (("lib/wk/places.py",), ("wk_places", "place_os", "lifecycle", "home_mounts", "container_workspace", "clobbering", "tree_identity", "tart_locator", "scp", "zed*", "enter", "mirror_path", "new_fetch", "owed_new", "egress"), 950, 1000),
    "record": (("lib/wk/record.py", "lib/wk/job.py", "lib/wk/lock.py", "lib/wk/resources.py", "lib/wk/clock.py", "lib/wk/kv.py"),
               ("wk_record", "wk_job", "wk_lock", "wk_resources", "lock_resources", "resources", "record_home", "task_record", "kv", "interrupt", "stall_report", "stop_tasks", "pattern_matching", "build_liveness", "build_kill", "crash_only", "state", "atexit_owner", "selftest_guard", "device_claim", "ensure_dir"), 550, 700),
    "workspace": (("lib/wk/workspace.py", "lib/wk/build.py", "lib/wk/sync.py", "lib/wk/pr.py", "lib/wk/git.py", "lib/wk/presets.py", "lib/wk/store.py", "lib/wk/tools.py", "lib/wk/agents.py", "lib/wk/repos.py", "lib/wk/project.py", "lib/treehash.py"),
                  ("wk_workspace", "wk_build", "wk_run", "wk_test", "wk_store", "wk_gui", "workspace_cleanup", "box_workspace", "repo_workspace", "build", "build_perf", "presets", "sync", "pr_*", "peer", "agents", "tools_sync", "cli_refspecs", "rm_*", "run_until_crash", "rr", "wk_profile", "git_gate", "hook_levels", "ai_inside"), 1150, 1800),
    "secrets": (("lib/wk/wall.py", "lib/wk/pushgate.py", "lib/wk/secrets.py", "lib/wk/key/", "lib/credcheck.py", "lib/secretfile.py", "lib/wk/publish.py", "lib/wk/backup.py", "lib/wk/claudelogin.py", "lib/wk/sudo.py", "lib/wk/priv.py"),
                ("commit_wall", "push_keys", "push_remote", "push_service", "key*", "wk_key", "wk_secrets", "wk_sudo", "credcheck", "backup", "agent_token", "claude_login", "ai", "store_secrets", "no_publish", "dev_integration", "doctor_wall", "pi_agent"), 1150, 2000),
    "status": (("lib/wk/status.py", "lib/wk/statusview.py", "lib/wk/doctor.py", "lib/wk/gc.py", "lib/wk/rubble.py", "lib/wk/fleet.py", "lib/wk/reach.py", "lib/wk/setupstate.py", "lib/wkdata.py"),
               ("status*", "wk_overrides_cmd1", "doctor", "owed_gc", "owed_status_log", "owed_cli_color", "gc_tart_cache", "fleet_walk", "reach", "confs", "host_only", "host_tools", "newcomer_path", "ceilings", "layers", "quiesce", "build_wall"), 700, 900),
    "bench": (("lib/wk/bench/", "lib/wk/pgo.py", "lib/wk/sched.py", "lib/wk/profile.py", "lib/wk/samply.py", "lib/wk/ldpath.py"),
              ("bench_*", "ab_*", "mac_ab_*", "mac_pgo", "mac_autorun", "mac_gates", "board_pgo", "pgo_harness", "sched", "pi_ab_systems", "owed_bench", "preflight_unknown", "profile_debug", "slots"), 1300, 2200),
    "boot": (("lib/wk/boot/",), ("boot_*", "pi", "pi_tryboot", "rpi5_*", "second_system", "quiet_siblings", "kernel_pin"), 650, 1100),
    "sysimage": (("lib/wk/sysimage/", "lib/wk/images.py", "lib/wk/slot.py", "lib/wk/disk.py"),
                 ("sysimage_*", "images", "image_*", "disk*", "buildroot*", "yocto_*", "build_subject", "board_config_append", "board_local_conf", "card_edits", "write_*", "wifi_seed", "multilib_image", "sdk_refresh", "tailnet_name", "vm_base", "mac_tailnet", "mac_volume"), 950, 1400),
    "guest": (("lib/wk/guest.py", "lib/wk/daemons.py", "lib/wk/mac.py", "lib/wk/quiet.py", "lib/wk/session.py", "lib/wk/screen.py", "lib/wk/notify.py", "lib/wk/tailnet.py", "lib/wk/machine_cmd/", "lib/wk/sshalias.py"),
              ("guest", "daemons", "mac_guest", "mac_quiet*", "mac_display", "session", "notify", "tailnet_retire", "machine_cmd", "provision_stale", "vm_clock", "vm_desktop", "vmtools_mounts", "push_vm", "editors", "shell_*"), 900, 1000),
    "cmd": (("cmd/",), (), 500, 0),
    "lint rules": ((), ("lint_*", "comment_density", "runner", "support", "cli_shape", "static_rules", "entry_points", "help_*", "prompts", "reporting_words", "report_readonly", "rescue_layer", "owed_static_audits", "battery", "wk_overrides_lib"), 0, 700),
    "live bodies": ((), ("owed_live", "*_live"), 0, 800),
}
FIXTURES_BUDGET = 800


def tracked():
    return subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                          cwd=REPO, capture_output=True, text=True, check=True).stdout.split()


def lines(rel):
    return python_counts((REPO / rel).read_text())[0]


def core_files():
    return [f for f in tracked() if f == "wk" or (f.startswith(("lib/", "cmd/")) and (f.endswith(".py") or f.startswith("cmd/")))]


def test_files():
    return [f for f in tracked() if f.startswith("tests/test_") and f.endswith(".py")]


def owners(rel, column):
    if column == 0:
        return [r for r, row in ROWS.items() if any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in row[0])]
    name = rel[len("tests/test_"):-3]
    return [r for r, row in ROWS.items() if any(fnmatch.fnmatchcase(name, g) for g in row[1])]


@functools.cache
def table():
    core = {r: 0 for r in ROWS}
    tests = {r: 0 for r in ROWS}
    for f in core_files():
        for r in owners(f, 0)[:1]:
            core[r] += lines(f)
    for f in test_files():
        for r in owners(f, 1)[:1]:
            tests[r] += lines(f)
    out = [(r + " core", core[r], row[2]) for r, row in ROWS.items() if row[2]]
    out += [(r + " tests", tests[r], row[3]) for r, row in ROWS.items() if row[3]]
    out.append(("fixtures tests", sum(lines(f) for f in SUPPORT), FIXTURES_BUDGET))
    return out


def report():
    print(f"{'row':<22}{'today':>8}{'budget':>8}{'over-by':>9}")
    for name, today, budget in table():
        print(f"{name:<22}{today:>8}{budget:>8}{max(0, today - budget):>9}")


def under(name):
    today, budget = next((t, b) for n, t, b in table() if n == name)
    assert today <= budget, f"{name}: {today} non-blank lines over the budget {budget}; remove this test's @owed mark"


class Budgets(unittest.TestCase):
    def test_every_file_has_one_row(self):
        bad = [f for f in core_files() if len(owners(f, 0)) != 1] + [f for f in test_files() if len(owners(f, 1)) != 1]
        self.assertEqual(bad, [], "each file belongs to exactly one row of ROWS")

    def test_sums(self):
        t = table()
        core = sum(today for n, today, _ in t if n.endswith(" core"))
        tests = sum(today for n, today, _ in t if n.endswith(" tests"))
        self.assertEqual((core <= 10000, tests <= 15000), (True, True), f"core {core}, tests {tests}")

    test_sums = owed("core 10,000 and tests 15,000 total; the sum row of PLAN.md's budget table")(test_sums)


def _row(name, today, budget):
    def test(self):
        under(name)
    return owed(f"{name}: {today} today, budget {budget} (PLAN.md budgets per package)")(test) if today > budget else test


for _name, _today, _budget in table():
    setattr(Budgets, "test_budget_" + _name.replace(" ", "_"), _row(_name, _today, _budget))

if __name__ == "__main__":
    report()
