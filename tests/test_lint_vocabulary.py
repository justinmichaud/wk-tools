"""lint.vocabulary: one spelling per concept, in no file outside docs/, CLAUDE.md"""
TIER = "lint"
import re
import subprocess
import unittest

from tests.support import REPO


class Unless:
    """`bad`, once every `ok` span is blanked: a retired word inside a phrase that is another program's stays legal."""

    def __init__(self, bad, ok):
        self.bad, self.ok, self.pattern = re.compile(bad), re.compile(ok), bad

    def search(self, line):
        return self.bad.search(self.ok.sub(lambda m: " " * len(m.group(0)), line))


TARGET_OK = (r"(?i)\b(?:build|cmake|make|ninja|cross(?:-build)?|systemd|precision)[- ]targets?\b"
             r"|\btargets? (?:triple|tuple|device)\b|\btargets\.conf\b|\bcross-target\b|\bport-target\b|\btarget-finalize\b"
             r"|\b(?:multi-user|default|local-fs|network(?:-online|-pre)?|sysinit|basic|timers|swap|sleep|suspend|hibernate|hybrid-sleep)\.target\b"
             r"|WantedBy=\S*|settings set target\.\S*|\btarget\.process\b|invalid target|\b(?:assign|n)\.targets\b"
             r"|\bhread\(target=|(?:^|[(,])\s*target=\w+(?:\.\w+)+|\b(?:pip install|rustc|cargo|findmnt|yocto_ws\.py)\b.*--target\b")
# Where "target" is another domain's own word for something that is neither a place nor a driver.
TARGET_DOMAINS = {
    "a symlink's target": ("wk", "bin/wk", "lib/wk/machine.py", "lib/wk/lock.py", "tests/test_wk_machine.py", "tests/test_wk_lock.py"),
    "an HTTP request-target": ("container/proxy/", "tests/test_egress.py"),
    "lldb's target": ("container/lldb/",),
    "a mount point or disk": ("lib/wk/mac.py", "lib/wk/sysimage/macvolume.py", "host/macos/machine.sh", "host/macos/vmtools.sh",
                              "tests/test_machine_mounts.py", "admin/wk-card-priv"),
    "the precision an A/B is asked to resolve": ("lib/wkdata.py", "lib/wk/bench/report.py", "lib/wk/bench/autorun.py",
                                                 "lib/wk/bench/board_ab.py", "tests/test_ab_precision.py",
                                                 "tests/test_bench_report.py", "tests/test_mac_autorun.py"),
    "the cross target an image is built for": ("lib/wk/sysimage/yocto_ws.py", "lib/wk/sysimage/buildroot_ws.py", "image/",
                                               "container/yocto/", "container/buildroot/", "tests/test_yocto_ws.py",
                                               "tests/test_yocto_stage.py", "tests/test_board_local_conf.py",
                                               "tests/test_buildroot_ws.py"),
    "a systemd target": ("boot/firstboot/", "host/units/", "host/linux/rpi5/"),
    "where a copy or a credential lands": ("tests/test_backup.py", "tests/test_boot_priv.py", "lib/wk/key/creds.py"),
}
RETIRED = {
    "say 'image workspace' (image_ws), or the precise word for the meaning": re.compile(r"(?i)\blanes?\b"),
    "say 'bench task'": re.compile(r"(?i)\bbenchmark tasks?\b|\bbench jobs?\b"),
    "say 'snapshot'": re.compile(r"(?i)\bbase[ -]snapshots?\b"),
    "say 'keyring'": re.compile(r"(?i)\bsecrets?[ _]dir(?:ector(?:y|ies))?\b|\bcredential stores?\b"),
    "say the part's name from README's Overrides (lib/wk/store.py)": re.compile(
        r"\b(?:record_dir|artifact_dir|base_dir|base_path|base_sha_file|ws_base_id|"
        r"secrets_view_dir|agent_rw_dir|push_held_dir|held_dir|broker_socket|provisioned_root|machine_store|"
        r"named_root|vm_root|container_mirror)\b"),
    "say 'place' (where a workspace lives, `--on`, WK_PLACE) or 'driver' (what makes one, WK_DRIVER)":
        Unless(r"(?i)\btargets?\b|\bWK_TARGET\w*", TARGET_OK),
    "say 'build preset' (`--preset`, WK_PRESET, `preset=`)":
        Unless(r"(?i)\bbuild[ -]configs?\b|--config\b|\bWK_CONFIG\b|\bconfig=(?:--config|arg)\b", r"rpi-eeprom-config\W.*--config\b"),
    "say 'bench machine'; `bench-device` is only the role value machines/*.conf declares":
        re.compile(r"(?i)\bbench devices?\b|(?<![\w\"'=-])bench-devices?(?![\w\"'-])"),
    "say 'A/B task'": Unless(r"(?i)\bexperiments?\b", r"Apple's experiment|\bexperiments(?==|\s+com\.apple)"),
}
EXEMPT = ("docs/", "claude/skills/")
README_MARKER = "*** Claude edit below here ***"


def exempt(rel, why):
    return why.startswith("say 'place'") and any(rel == f or (f.endswith("/") and rel.startswith(f))
                                                   for files in TARGET_DOMAINS.values() for f in files)


def hits():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    for rel in sorted(set(out)):
        path = REPO / rel
        if rel in ("CLAUDE.md", "tests/test_lint_vocabulary.py") or rel.startswith(EXEMPT) or path.is_symlink() or not path.is_file():
            continue
        text, skipped = path.read_text(errors="replace"), 0
        if rel == "README.md":  # above the marker is the user's spec
            head, _, text = text.partition(README_MARKER)
            skipped = head.count("\n")
        for n, line in enumerate(text.splitlines(), skipped + 1):
            for why, pattern in RETIRED.items():
                if pattern.search(line) and not exempt(rel, why):
                    yield "%s:%d: %s (%s)" % (rel, n, line.strip(), why)


class TestVocabulary(unittest.TestCase):
    def test_the_patterns_catch_the_retired_words(self):
        word, long, tgt, cfg = "la" + "ne", "bench" + "mark task", "tar" + "get", "con" + "fig"
        cases = [  # (the rule, retired spellings, spellings that stay)
            ("image workspace", ["each %s builds" % word, "two %ss" % word.upper(), "<%s>" % word], ["a plane, %sway" % word]),
            ("bench task", ["a " + long, ("a " + long + "s").title(), "the bench" + " job hands back"],
             ["a bench task, a benchmark", "a job is planted"]),
            ("'snapshot'", ["the base" + " snapshot", "Base" + "-snapshots"], ["a snapshot", "the base image"]),
            ("'keyring'", ["the secrets" + " directory", "secrets" + "_dir", "a credential" + " store"],
             ["the keyring", "a credential stored here", "WK_HOST_SECRETS"]),
            ("part's name", ["store." + "record_dir()", "s." + "agent_rw_dir()", "Store." + "broker_socket"],
             ["store.records_dir()", "store.keyring_agent_rw_dir()", "runtime_socket", "GUEST_BROKER_SOCKET"]),
            ("'place'", ["wk new foo --%s vm" % tgt, "the workspace's %s" % tgt, "WK_%s=vm wk ls" % tgt.upper(),
                         "WK_%s_KIND" % tgt.upper(), "unknown %s 'x'" % tgt, "every %s here" % tgt, "%ss: container, vm" % tgt.title()],
             ["wk new foo --on vm", "the place's driver", "a CMake %s" % tgt, "the build %s tuple" % tgt, "--cross-%s=rpi4" % tgt,
              "rustc --%s=aarch64-unknown-linux-gnu" % tgt, "WantedBy=multi-user.%s" % tgt, "Tools/yocto/%ss.conf" % tgt,
              "settings set %s.process.follow-fork-mode child" % tgt, "threading.Thread(%s=self.run)" % tgt, "retargeted",
              "YOC_PORT_%s_FROM" % tgt.upper(), "the precision %s was met" % tgt]),
            ("'build preset'", ["wk run ws --%s gtk-release" % cfg, "a build " + cfg, "Build %ss" % cfg, "WK_%s=jsc-release" % cfg.upper(),
                                "%s=--%s" % (cfg, cfg), "%s=arg" % cfg],
             ["wk run ws --preset gtk-release", "rpi-eeprom-%s --%s boot.conf" % (cfg, cfg), "git %s --get user.name" % cfg,
              "WK_%sURATION_BUILD_DIR" % cfg.upper(), "an image %suration" % cfg]),
            ("'bench machine'", ["a bench" + " device", "on a bench" + "-device it is", "Bench" + " devices here"],
             ["a bench machine", "role=bench" + "-device", 'v["role"] == "bench' + '-device"']),
            ("'A/B task'", ["an older " + "experiment's rounds", "the whole " + "experiment"],
             ["Apple's " + "experiment configurations", "experiment" + "s=off", "experimental branches"]),
        ]
        for key, bad, good in cases:
            pattern = next(p for why, p in RETIRED.items() if key in why)
            for text in bad:
                self.assertRegex(text, pattern)
            for text in good:
                self.assertNotRegex(text, pattern)

    def test_no_file_uses_a_retired_word(self):
        self.assertEqual([], list(hits()))


if __name__ == "__main__":
    unittest.main()
