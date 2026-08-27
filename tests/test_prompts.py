"""Audit: "Prompts guard destructive actions only" (CLAUDE.md). Every
interactive prompt in the tree goes through the one yes/no helper
(lib/common.sh's confirm()), that helper defaults to No and declines
without a terminal, and every site that calls it is destructive -- or is
named here as owed work if it is not.

This is a point-in-time audit, not a general-purpose scanner: the expected
site lists below are (file, exact prompt text) snapshots of cmd/*, lib/*.sh,
boot/*.sh, bench/*.sh, image/*.sh and admin/* as of this writing -- keyed on
the prompt text rather than a line number because several of these files
were observed changing line count *during the writing of this test* (an
unrelated concurrent edit elsewhere in the tree); a line-number key would
have failed on every unrelated line added above it, which is not what this
audit is for. A new prompt, a reworded one, or a deleted one still fails
the relevant test -- on purpose, so it is looked at and the list updated
deliberately rather than drifting unnoticed. Failure messages report the
line grep finds *right now*, for whoever is looking.

Beyond the (file, text) classification, TestConfirmSitesArePrecededByDestructiveWording
re-derives "destructive" a second, independent way: it greps the ~10 lines
leading into each confirm() call for an actual destructive verb/word
(erase, delete, remove, overwrite, destroy, ...) or an already-present
`warn` saying as much, rather than trusting DESTRUCTIVE_SITES' own
say-so -- so a site added to that set without any such wording nearby is
still caught.

Run: python3 -m unittest tests.test_prompts -v
"""

import os
import pty
import re
import subprocess
import unittest

from tests.support import REPO, bash


# --- what gets audited --------------------------------------------------------
# cmd/* and admin/* (every file directly under each, which mostly lack an
# extension -- admin/wk-card-priv, admin/wk-quiesce-priv) plus lib/, boot/,
# bench/, image/'s own *.sh files -- not their subdirectories (image/yocto/...
# is recipe metadata, not a wk command).
def _target_files():
    files = [p for p in sorted((REPO / "cmd").iterdir()) if p.is_file()]
    files += [p for p in sorted((REPO / "admin").iterdir()) if p.is_file()]
    for d in ("lib", "boot", "bench", "image"):
        files += sorted((REPO / d).glob("*.sh"))
    return [p.relative_to(REPO) for p in files]


def _grep(pattern, files):
    """(relpath, lineno, line-content) for an extended-regex pattern across
    files, via `grep -n -E` -- the same tool this audit was built with.
    Paths are passed and matched relative to REPO, so the results line up
    with the file:line pairs recorded below rather than this machine's
    absolute checkout path."""
    if not files:
        return []
    cp = subprocess.run(
        ["grep", "-n", "-E", pattern, *[str(p) for p in files]],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    out = []
    for line in cp.stdout.splitlines():
        path, lineno, content = line.split(":", 2)
        out.append((path, int(lineno), content))
    return out


def _confirm_sites():
    """{(file, exact confirm-call text): current line number}."""
    return {
        (path, content.strip()): lineno
        for path, lineno, content in _grep(r'confirm "', _target_files())
    }


def _raw_read_sites():
    """{(file, exact read text): current line number} for every
    `read -r`/`read -p` outside lib/common.sh (the one file allowed to
    implement a prompt), minus the `while ... read` data-processing loops
    that are not asking a person anything."""
    files = [f for f in _target_files() if f.name != "common.sh"]
    out = {}
    for path, lineno, content in _grep(r"read -r|read -p", files):
        if "while" in content:
            continue
        out[(path, content.strip())] = lineno
    return out


# --- the audit's findings, as of this writing ---------------------------------
# Every confirm() call site in the tree (24), classified by (file, exact
# call text) -- see the module docstring for why not by line number. A site
# landing in neither set below is new and unclassified -- the test that
# compares this union against a fresh grep is what catches that.
DESTRUCTIVE_SITES = {
    ("cmd/bridge", 'confirm "remove the bridge role from $BR_SSH?" || die "aborted -- nothing was changed"'),
    ("cmd/gc", 'confirm "erase the mirror and every base snapshot? \'wk sync\' refetches them (a full clone)" \\'),
    ("cmd/remote", 'confirm "remove the local conf $conf anyway?" || die "aborted -- nothing was changed"'),
    ("cmd/remote", 'confirm "deprovision $WK_REMOTE_HOST and remove target \'$TARGET\'?" \\'),
    ("cmd/remote", 'if confirm "remove the remote root $root ($sz) from $WK_REMOTE_HOST?"; then'),
    ("cmd/remote", 'if confirm "remove $d from $r_host?"; then'),
    ("cmd/remote", 'if confirm "remove the mirror at $M?"; then'),
    ("cmd/pi", 'confirm "write this configuration to $HOST\'s EEPROM?" || die "not written"'),
    ("cmd/rm", 'confirm "destroy workspace(s)${_list} and all their changes?" || die "aborted"'),
    ("cmd/rm", 'confirm "destroy workspace \'$NAME\' and all its changes?" || die "aborted"'),
    ("cmd/skills", 'confirm "overwrite them with the shared copy?" || die "aborted; commit or stash first"'),
    ("cmd/skills", 'confirm "no git repo at $WK_ROOT -- pull cannot be undone; continue?" || die "aborted"'),
    ("cmd/skills", 'confirm "overwrite the shared skills with the repo copy?" || die "aborted"'),
    ("cmd/sysimage", 'confirm "write $from onto $DISK_DEV attached to $DISK_MACHINE?" || die "not written"'),
    ("cmd/vm", 'confirm "delete and rebuild the golden base VM \'$WK_VM_BASE\'?" || die "aborted"'),
    ("cmd/vm", 'confirm "delete the golden base VM \'$WK_VM_BASE\' ($sz)? rebuilding it is hours" \\'),
    ("cmd/vm", 'if confirm "also drop the pulled image cache ($csz)? it is re-downloadable"; then'),
    ("cmd/vm", 'confirm "delete macOS VM \'$NAME\'?" || die "aborted"'),
    ("bench/mac-bench-volume.sh", 'confirm "start the install onto \'$VOLUME\' now?" || { log "nothing done"; return 0; }'),
    ("image/pmos.sh", 'confirm "erase it on $h? the next build there refetches (minutes, not hours)" \\'),
}

# Known offenders against "prompts guard destructive actions only": each
# asks consent for something that installs, reconfigures or merely warns --
# answering yes destroys nothing. None of these files are in the set this
# task may edit (only cmd/sync is), so they are reported, not fixed; each
# reason is the owed work docs/HANDOFF-test-runner.md now names.
NON_DESTRUCTIVE_OFFENDERS = {
}

# The raw (non-confirm()) `read -r`/`read -p` sites outside lib/common.sh, and
# why each is not a competing yes/no implementation.
EXPECTED_SAFE_RAW_READS = {
    ("cmd/bridge", 'read -r _reply || die "aborted -- nothing further was changed"'):
        "pause(): waits for Enter before a manual step with no alternative -- explicitly not confirm() (see the comment above it), since there is nothing to answer no to",
    ("cmd/remotes", '{ read -r extra_name; read -r extra_url; read -r ssh_config; } <<EOF'):
        "reads three lines from a heredoc, not a terminal",
    ("cmd/sync", 'read -r _pick </dev/tty || _pick=""'):
        "reads a menu number (which workspace, or --machine) -- a choice among several, not a yes/no destructive decision",
    ("admin/wk-card-priv", 'read -r type tran <<EOF'):
        "reads two fields from a heredoc, not a terminal",
    ("cmd/sysimage", 'read -r bytes sha < "$WRITE_META"'):
        "reads the stream meter's byte count and hash from a file, not a terminal",
}


class TestOnePromptHelper(unittest.TestCase):
    """(a): every yes/no prompt goes through lib/common.sh's confirm() --
    no raw `read` implements a second one anywhere in the audited tree."""

    def test_confirm_is_defined_exactly_once(self):
        sites = _grep(r"^confirm\(\)", _target_files())
        self.assertEqual(
            [(path, content) for path, _, content in sites],
            [("lib/common.sh", "confirm() {")],
            f"expected exactly one confirm() definition, found: {sites}",
        )

    def test_no_competing_raw_read_prompt(self):
        found = _raw_read_sites()
        expected = set(EXPECTED_SAFE_RAW_READS)
        unexpected = set(found) - expected
        self.assertEqual(
            unexpected, set(),
            "raw read(s) outside confirm() not accounted for -- audit each "
            f"(current line, if still there): "
            f"{[(f, c, found[(f, c)]) for f, c in unexpected]}",
        )
        missing = expected - set(found)
        self.assertEqual(
            missing, set(),
            f"expected safe read site(s) not found -- audit is stale: {sorted(missing)}",
        )


class TestConfirmSitesAreDestructive(unittest.TestCase):
    """(c): every confirm() call site guards a destructive action, or is
    named as owed work if it does not."""

    def test_every_confirm_site_is_classified(self):
        found = _confirm_sites()
        known = DESTRUCTIVE_SITES | set(NON_DESTRUCTIVE_OFFENDERS)
        unexpected = set(found) - known
        self.assertEqual(
            unexpected, set(),
            "new confirm() site(s), not yet classified destructive or not "
            f"(current line, if still there): "
            f"{[(f, c, found[(f, c)]) for f, c in unexpected]}",
        )
        missing = known - set(found)
        self.assertEqual(
            missing, set(),
            f"expected confirm() site(s) not found -- audit is stale: {sorted(missing)}",
        )

    def test_known_offenders_still_prompt_and_are_still_not_destructive(self):
        # xfail-style: each of these still calls confirm() today, still
        # guards a non-destructive action, and is still owed a fix outside
        # the file set this task may touch. If one of these no longer
        # appears verbatim, the finding is stale -- fixed, reworded, or
        # removed -- and this fails so the list gets updated rather than
        # lying. Re-derived from a fresh grep each run, so it survives an
        # unrelated line shifting elsewhere in the file.
        found = _confirm_sites()
        for key, reason in NON_DESTRUCTIVE_OFFENDERS.items():
            path, text = key
            self.assertIn(
                key, found,
                f"{path}: {text!r} no longer found ({reason}) -- update NON_DESTRUCTIVE_OFFENDERS",
            )


# A destructive verb/word, or a warn()-shaped statement of one (ERASES,
# deletes, removes, overwrite -- CLAUDE.md's own examples, plus the other
# words the tree actually uses for the same thing: destroy, wipe,
# deprovision, rebuild, drop, replace, "cannot be undone").
DESTRUCTIVE_WORD_RE = re.compile(
    r"destroy|erase|delet|remov|overwrit|wipe|deprovision|rebuild|drop|"
    r"replac|cannot be undone",
    re.IGNORECASE,
)

# confirm() site(s) that guard something genuinely destructive but where
# DESTRUCTIVE_WORD_RE finds nothing in the ~10 lines leading into the
# prompt -- the hazard is stated further up the file, past this check's
# window. A name, not a fix: none of these files are in the set this task
# may edit.
DESTRUCTIVE_WORDING_TOO_FAR = {
    ("bench/mac-bench-volume.sh",
     'confirm "start the install onto \'$VOLUME\' now?" || { log "nothing done"; return 0; }'):
        "installing macOS overwrites the target volume; the hazard "
        "(\"NEXT IS THE PART THAT ... REBOOTS THIS MACHINE\") is stated "
        "~50 lines above the confirm, past the ~10-line window this check reads",
}


def _confirm_context(path, lineno, before=10):
    """The `before` lines leading into (and including) line `lineno` of
    `path` -- the window TestConfirmSitesArePrecededByDestructiveWording
    reads, matching the ~10 lines the task asked for."""
    text = (REPO / path).read_text(errors="replace").splitlines()
    start = max(0, lineno - before)
    return "\n".join(text[start:lineno])


class TestConfirmSitesArePrecededByDestructiveWording(unittest.TestCase):
    """Automated half of (c), independent of DESTRUCTIVE_SITES' own
    classification: every confirm() call site must have an actual
    destructive verb/word (or an already-printed warn() saying as much) in
    the ~10 lines leading into the prompt -- so a site added to
    DESTRUCTIVE_SITES without any such wording nearby is caught here
    rather than only trusted."""

    def test_every_confirm_site_has_destructive_wording_nearby(self):
        found = _confirm_sites()
        unworded = []
        for (path, text), lineno in found.items():
            if (path, text) in DESTRUCTIVE_WORDING_TOO_FAR:
                continue
            ctx = _confirm_context(path, lineno)
            if not DESTRUCTIVE_WORD_RE.search(ctx):
                unworded.append((path, lineno, text))
        self.assertEqual(
            unworded, [],
            "confirm() site(s) with no destructive verb/word in the ~10 "
            f"lines leading into the prompt: {unworded}",
        )

    def test_the_far_wording_exception_is_still_accurate(self):
        found = _confirm_sites()
        for key, reason in DESTRUCTIVE_WORDING_TOO_FAR.items():
            self.assertIn(
                key, found,
                f"{key}: no longer found ({reason}) -- update DESTRUCTIVE_WORDING_TOO_FAR",
            )
            path, _ = key
            lineno = found[key]
            ctx = _confirm_context(path, lineno)
            self.assertIsNone(
                DESTRUCTIVE_WORD_RE.search(ctx),
                f"{key}: now has destructive wording nearby -- drop it from "
                "DESTRUCTIVE_WORDING_TOO_FAR",
            )


class TestConfirmDefaultsToNoAndDeclinesWithoutATerminal(unittest.TestCase):
    """(b): confirm()'s own behaviour, driven directly -- no wk command, no
    workspace, no store."""

    SCRIPT = (
        ". lib/common.sh\n"
        "rc=0\n"
        "confirm 'do the thing?' || rc=$?\n"
        "echo RC=$rc\n"
    )

    def test_declines_with_stdin_not_a_tty(self):
        # bash() runs with stdin inherited from the test process; explicit
        # DEVNULL is what actually guarantees "not a terminal" regardless of
        # how the suite itself is invoked.
        cp = subprocess.run(
            ["bash", "-c", self.SCRIPT],
            cwd=str(REPO),
            env={**os.environ, "WK_ROOT": str(REPO)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertIn("RC=1", cp.stdout, cp.stderr)
        self.assertIn("declining", cp.stderr)
        self.assertIn("no terminal", cp.stderr)

    def test_declines_a_piped_yes_because_a_pipe_is_not_a_tty(self):
        # `echo n | ...` (and, tellingly, `echo y | ...`) both hit the same
        # [ ! -t 0 ] branch as the DEVNULL case above: a pipe is not a
        # terminal either, so the reply's content never even gets read --
        # there is no way to answer "yes" without a real tty or WK_YES.
        cp = subprocess.run(
            ["bash", "-c", self.SCRIPT],
            cwd=str(REPO),
            env={**os.environ, "WK_ROOT": str(REPO)},
            input="y\n",
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertIn("RC=1", cp.stdout, cp.stderr)
        self.assertIn("declining", cp.stderr)
        self.assertIn("no terminal", cp.stderr)

    def test_wk_yes_bypasses_even_with_no_terminal(self):
        cp = subprocess.run(
            ["bash", "-c", self.SCRIPT],
            cwd=str(REPO),
            env={**os.environ, "WK_ROOT": str(REPO), "WK_YES": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertIn("RC=0", cp.stdout, cp.stderr)

    def _confirm_over_a_real_tty(self, reply):
        # [ -t 0 ] needs an actual terminal device, not a pipe -- a pty is
        # the only way to reach the y/N read at all.
        master, slave = pty.openpty()
        try:
            env = {**os.environ, "WK_ROOT": str(REPO)}
            env.pop("WK_YES", None)
            proc = subprocess.Popen(
                ["bash", "-c", self.SCRIPT],
                cwd=str(REPO),
                env=env,
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(slave)
            slave = -1
            os.write(master, reply)
            out, err = proc.communicate(timeout=20)
            return out, err
        finally:
            if slave != -1:
                os.close(slave)
            os.close(master)

    def test_defaults_to_no_on_an_empty_reply(self):
        out, err = self._confirm_over_a_real_tty(b"\n")
        self.assertIn("RC=1", out, err)

    def test_declines_on_anything_but_y(self):
        out, err = self._confirm_over_a_real_tty(b"n\n")
        self.assertIn("RC=1", out, err)

    def test_accepts_a_y_reply(self):
        out, err = self._confirm_over_a_real_tty(b"y\n")
        self.assertIn("RC=0", out, err)

    def test_accepts_an_uppercase_y_reply(self):
        out, err = self._confirm_over_a_real_tty(b"Y\n")
        self.assertIn("RC=0", out, err)


if __name__ == "__main__":
    unittest.main()
