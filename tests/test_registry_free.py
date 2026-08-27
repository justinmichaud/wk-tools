"""Registry-free target resolution (lib/target.sh): `ws_target` derives which
target a workspace lives on from evidence -- a stat per configured target
against its own store, or, before that store exists, the status file `wk
new` writes as its first act, trusted only while the process writing it is
alive -- never from a workspace->target registry, because there is no
longer one to consult. `wk completion --list-workspaces` derives the same
way, walking the stores this machine can see. Each docstring is the phrase
of the behaviour it checks.

There is no hit/miss pair to test any more: a registry is a cache that can
answer right or wrong about a fact recomputed elsewhere, so testing it means
testing both cases. A derivation has exactly one answer, so there is nothing
to compare it against -- only whether that one answer is right.

Run: python3 -m unittest tests.test_registry_free -v
The podman-gated assertion (no registry file left behind by a real `wk new`)
lives in tests.test_lifecycle's existing container-lifecycle test, not here.
"""
import os
import tempfile
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run

_SOURCES = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
'''


class TestWsTargetDerivesFromTheStore(WkTest):
    def test_ws_target_resolves_a_workspace_in_a_fake_local_store(self):
        """`ws_target` resolves a workspace that exists only in a fake local store"""
        with tempfile.TemporaryDirectory(prefix="wk-registry-free-") as tmp:
            name = f"demo-{rand_suffix()}"
            os.makedirs(os.path.join(tmp, "ws", name))
            cp = bash(f'{_SOURCES}\nws_target {name}', env={"WK_STORE": tmp})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(
                cp.stdout.strip(), "container",
                "a workspace directory under $WK_STORE/ws is a container workspace: "
                "container is the one built-in kind whose store is plain $WK_STORE",
            )

    def test_ws_target_resolves_an_unknown_name_to_container(self):
        """a name in no store resolves to `container`"""
        name = f"nowhere-{rand_suffix()}"
        cp = bash(f'{_SOURCES}\nws_target {name}')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "container")

    def test_ws_target_resolves_a_live_creating_record_to_its_target(self):
        """a `creating` record with `target=` resolves to that target before any store dir exists"""
        with tempfile.TemporaryDirectory(prefix="wk-registry-free-") as tmp:
            name = f"creating-{rand_suffix()}"
            # No ws/<name> anywhere -- only the status file `wk new` writes as
            # its first act, naming the target it was asked for. `pid=$$` is
            # this very bash process's own pid, alive for exactly as long as
            # the ws_target call below that has to trust it.
            cp = bash(f'''
{_SOURCES}
mkdir -p "{tmp}/create"
cat > "{tmp}/create/{name}.status" <<EOF
state=creating
pid=$$
stage=create
target=vm
EOF
WK_VM_STORE="{tmp}" ws_target {name}
''')
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(
                cp.stdout.strip(), "vm",
                "a live creation record naming target=vm, with no store directory "
                "yet, should resolve to vm",
            )

    def test_ws_target_ignores_a_dead_creating_record(self):
        """a dead process's creation record is not trusted"""
        with tempfile.TemporaryDirectory(prefix="wk-registry-free-") as tmp:
            name = f"dead-{rand_suffix()}"
            cp = bash(f'''
{_SOURCES}
mkdir -p "{tmp}/create"
cat > "{tmp}/create/{name}.status" <<EOF
state=creating
pid=4194304
stage=create
target=vm
EOF
WK_VM_STORE="{tmp}" ws_target {name}
''')
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(
                cp.stdout.strip(), "container",
                "a status file left by a process that is no longer running is an "
                "unverifiable claim, not evidence -- it must not be believed",
            )


class TestCompletionListsTheStore(WkTest):
    def test_completion_list_workspaces_lists_fake_store_workspace(self):
        """`wk completion --list-workspaces` lists the fake store's workspace"""
        with tempfile.TemporaryDirectory(prefix="wk-registry-free-") as tmp:
            name = f"demo-{rand_suffix()}"
            os.makedirs(os.path.join(tmp, "ws", name))
            cp = run("completion", "--list-workspaces", env={"WK_STORE": tmp})
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(name, cp.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
