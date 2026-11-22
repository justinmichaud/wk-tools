import os
import shutil
import tempfile
import unittest

from tests import ROOT
from wk.core.machine import Local

SHIM = os.path.join(ROOT, "ansible", "roles", "agents", "files", "shim.sh")
STUB = '#!/bin/sh\necho "real ninja in %s: $# args, last: ${2-}"\nexit 7\n'


class TestShim(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.shims, self.second, self.real = self.tool("shims"), self.tool("second"), self.tool("real", STUB % "real")

    def tool(self, directory, text=None):
        path = os.path.join(self.tmp, directory)
        os.mkdir(path)
        if text:
            with open(os.path.join(path, "ninja"), "w") as f:
                f.write(text)
        else:
            shutil.copy(SHIM, os.path.join(path, "ninja"))
        os.chmod(os.path.join(path, "ninja"), 0o755)
        return path

    def ninja(self, path, cwd=None, **env):
        return Local().run(["/usr/bin/env", "-i", "PATH=" + path] + ["%s=%s" % kv for kv in env.items()] + ["ninja", "-C", "out dir"], cwd=cwd)

    def path(self, *dirs):
        return ":".join(dirs + ("/usr/bin", "/bin"))

    def test_an_agent_is_refused_and_told_the_wk_commands(self):
        r = self.ninja(self.path(self.shims, self.real), WK_AGENT="1")
        self.assertEqual((r.rc, r.out), (1, ""))
        self.assertEqual(r.err, "ninja: refused: an agent builds and tests through wk, which sizes the job count to this machine.\n"
                                "    use: wk build <ws> <preset>  |  wk test <ws> -- <args>  |  wk run <ws> -- <args>\n")

    def test_wks_own_build_passes_to_the_real_tool_with_its_arguments_and_status(self):
        r = self.ninja(self.path(self.shims, self.real), WK_AGENT="1", WK_BUILD="1")
        self.assertEqual((r.rc, r.out, r.err), (7, "real ninja in real: 2 args, last: out dir\n", ""))

    def test_a_person_passes(self):
        self.assertEqual(self.ninja(self.path(self.shims, self.real)).rc, 7)

    def test_a_second_shim_directory_is_recognised_and_skipped(self):
        r = self.ninja(self.path(self.shims, self.second, self.real))
        self.assertEqual((r.rc, r.out), (7, "real ninja in real: 2 args, last: out dir\n"))

    def test_with_only_shims_on_path_the_tool_is_not_found(self):
        r = self.ninja(self.path(self.shims, self.second))
        self.assertEqual((r.rc, r.err), (127, "ninja: not found: PATH holds no ninja but wk's shim\n"))

    def test_an_empty_path_entry_is_not_the_current_directory(self):
        here = self.tool("here", STUB % "the current directory")
        r = self.ninja(self.shims + "::/usr/bin:/bin:", cwd=here)
        self.assertEqual((r.rc, r.out), (127, ""))

    def test_a_directory_or_a_file_that_does_not_execute_is_passed_over(self):
        os.mkdir(os.path.join(self.tool("plain", "not a program"), "sub"))
        os.chmod(os.path.join(self.tmp, "plain", "ninja"), 0o644)
        os.makedirs(os.path.join(self.tmp, "dir", "ninja"))
        self.assertEqual(self.ninja(self.path(self.shims, os.path.join(self.tmp, "dir"), os.path.join(self.tmp, "plain"), self.real)).rc, 7)

    def test_it_parses_as_posix_sh_and_as_bash(self):
        for shell in ("sh", "bash"):
            r = Local().run([shell, "-n", SHIM])
            self.assertEqual((r.rc, r.err), (0, ""), shell)
