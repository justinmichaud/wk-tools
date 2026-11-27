import unittest

from tests.provision import DF, HOME, SHA, TOOLS, controller, lanes, provisioned, server
from tests.world import World
from wk.core.machine import Result
from wk.provision import ansible

SMALL = {"desk": "kind=workstation", "lin": "kind=linux", "mac": "kind=mac", "vault": "kind=controller"}
CONTROLLER = ("vault\n  ok       ssh            OpenSSH_9.6\n  ok       git            git version 2.43.0\n"
              "  ok       tailscale      100.64.0.1 vault\n  ok       ansible        ansible-playbook [core 2.19.3]\n"
              "  ok       xorriso        xorriso 1.5.6\n  ok       qemu-nbd       qemu-nbd 8.2.2\n"
              "  ok       websocat       %s\n" % ansible.websocat(HOME))
SETUP = "wk machine setup lin, typed on vault"


def rows(out):
    """{name: (status, detail, remedy)} of `wk doctor`'s text: one line per row, a `fix:` line under one that is not ok."""
    found, last = {}, None
    for line in out.splitlines():
        if line[11:].startswith("fix: "):
            found[last] = found[last][:2] + (line[16:],)
        elif line.startswith("  "):
            last = line[11:25].strip()
            found[last] = (line[2:10].strip().lower(), line[26:], "")
    return found


class Toy(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.toy")
        self.w = World(confs=SMALL, here="lin")
        self.lin = self.w.machines["lin"]

    def doctor(self, *words, **how):
        return self.w.wk("doctor", *words, **how)


class TestDoctor(Toy):
    def test_on_a_server_it_reports_that_server_alone_and_asks_no_other(self):
        self.lin.files["/rows/mirror"] = "ok|fetched today|"
        r = self.doctor()
        self.assertEqual((r.code, r.out, [c for c in r.calls if not c.startswith("[lin]")]), (0, "lin\n  ok       mirror         fetched today\n", []))

    def test_it_changes_nothing(self):
        self.assertEqual(self.doctor().effects, [])

    def test_a_missing_row_is_shouted_with_its_remedy_and_the_exit_code_is_1(self):
        self.lin.files.update({"/rows/pueue": "missing|no pueue|wk machine setup lin --stage pueue", "/rows/sdk": "unknown|podman died|"})
        r = self.doctor()
        self.assertEqual((r.code, r.out), (1, "lin\n  MISSING  pueue          no pueue\n           fix: wk machine setup lin --stage pueue\n"
                                              "  unknown  sdk            podman died\n"))

    def test_an_unknown_row_with_nothing_missing_needs_a_person(self):
        self.lin.files["/rows/sdk"] = "unknown|podman died|wk machine setup lin"
        r = self.doctor()
        self.assertEqual(r.code, 4)
        self.assertIn("  unknown  sdk            podman died\n           fix: wk machine setup lin\n", r.out)

    def test_on_the_controller_it_reports_the_controller(self):
        self.w.machines["vault"].files["/rows/injector"] = "ok|login held|"
        self.assertEqual(self.doctor(on="vault").out, "vault\n  ok       injector       login held\n")

    def test_it_is_not_typed_on_a_workstation(self):
        self.assertIn("error: wk doctor runs on the controller or a Linux server or a Mac server (lin, mac, vault), and this is desk",
                      self.doctor(on="desk").err)


class TestDoctorWorkspace(Toy):
    def test_a_workspace_is_reported_by_the_server_that_holds_it_whose_hooks_get_the_workspace(self):
        self.w.workspace("x", on="lin")
        r = self.doctor("x")
        self.assertEqual((r.code, r.out), (0, "x (workspace on lin)\n  ok       checkout       /home/u/wk/ws/x\n"))

    def test_a_workspace_and_all_are_refused_on_the_controller(self):
        for words, what in ((("x",), "x"), (("--all",), "--all")):
            r = self.doctor(*words, on="vault")
            self.assertEqual((r.code, r.err), (1, "error: vault holds no workspace: `wk doctor %s` is typed on the server that holds it\n"
                                                  "usage: wk doctor [--all] [<ws>]\n" % what))

    def test_on_a_server_a_workspace_it_does_not_hold_is_refused(self):
        self.w.workspace("x", on="mac")
        r = self.doctor("x")
        self.assertEqual((r.code, r.err), (1, "error: no workspace named x on lin: `wk ls` lists them, `wk new x` makes it\n"))

    def test_on_a_server_all_adds_its_own_workspaces(self):
        self.w.workspace("y", on="lin")
        self.assertEqual(self.doctor("--all").out, "lin\ny (workspace on lin)\n  ok       checkout       /home/u/wk/ws/y\n")


class Own(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.own")
        self.w = World(confs=SMALL, here="lin")
        server(self.w.machines["lin"])
        self.lin = self.w.machines["lin"]

    def rows(self, **how):
        r = self.w.wk("doctor", **how)
        return r.code, rows(r.out)


class TestServerRows(Own):
    def test_a_server_that_was_set_up_is_all_ok(self):
        provisioned(self.lin)
        r = self.w.wk("doctor")
        self.assertEqual((r.code, r.out), (0, "lin\n  ok       python3        3.11\n  ok       git            git version 2.43.0\n"
                                              "  ok       wk commit      %s\n  ok       wk on PATH     %s/bin/wk\n  ok       layout\n"
                                              "  ok       disk           100 GB free in %s, and the floor is 20\n"
                                              "  ok       clock          0 s from this machine's, and the bound is 30\n" % (SHA, HOME, HOME)))

    def test_a_bare_server_lacks_the_checkout_the_path_and_the_layout_and_setup_on_the_controller_is_the_remedy(self):
        code, found = self.rows()
        self.assertEqual(code, 1)
        self.assertEqual((found["wk commit"], found["wk on PATH"]), (("missing", "%s has no checkout of wk" % HOME, SETUP),
                                                                    ("missing", "the login shell finds no wk", SETUP)))
        self.assertEqual(found["layout"], ("missing", "absent or wrongly moded: %s" % ", ".join(
            "%s/%s" % (HOME, name) for name in ("bin", "cache", "git", "ws", "secrets")), SETUP))

    def test_an_edited_tree_is_missing(self):
        provisioned(self.lin)
        self.lin.files[TOOLS + "/edited"] = "x"
        self.assertEqual(self.rows()[1]["wk commit"], ("missing", "%s with changes: it runs what the controller did not give it" % SHA, SETUP))

    def test_a_python_that_is_absent_is_missing_and_one_that_does_not_answer_is_unknown(self):
        self.lin.on(("python3",), Result(127, "", "sh: python3: command not found\n"))
        self.assertEqual(self.rows()[1]["python3"], ("missing", "sh: python3: command not found", "install it on lin with the system's package manager"))
        self.lin.on(("python3",), Result(124, "", "timed out after 30s"))
        self.assertEqual(self.rows()[1]["python3"][:2], ("unknown", "`python3 -c 'import sys; print('\"'\"'%d.%d'\"'\"' % sys.version_info[:2])'` did not answer in 30s"))

    def test_a_python_older_than_wk_runs_on_is_missing(self):
        self.lin.on(("python3",), "3.8\n")
        self.assertEqual(self.rows()[1]["python3"], ("missing", "3.8 is older than 3.9", "install it on lin with the system's package manager"))

    def test_free_disk_under_the_floor_is_missing_and_gc_is_the_remedy(self):
        self.lin.on(("df", "-Pk"), DF % (19 * 2 ** 20))
        self.assertEqual(self.rows()[1]["disk"], ("missing", "19 GB free in %s, and the floor is 20" % HOME, "wk gc"))

    def test_a_disk_that_cannot_be_read_is_unknown(self):
        self.lin.on(("df", "-Pk"), Result(1, "", "df: %s: No such file or directory\n" % HOME))
        self.assertEqual(self.rows()[1]["disk"][:2], ("unknown", "df %s: df: %s: No such file or directory" % (HOME, HOME)))

    def test_a_clock_beyond_the_bound_is_missing_either_way(self):
        for far, off in (("1031", 31), ("968", 31)):
            self.lin.on(("date", "+%s"), far + "\n")
            self.assertEqual(self.rows()[1]["clock"], ("missing", "%d s from this machine's, and the bound is 30" % off, "turn on time synchronisation on lin"))

    def test_a_clock_that_does_not_answer_a_number_is_unknown(self):
        self.lin.on(("date", "+%s"), Result(1))
        self.assertEqual(self.rows()[1]["clock"][:2], ("unknown", "`date +%s` on lin answered nothing"))

    def test_on_a_server_wk_home_is_the_environments(self):
        server(self.lin, home="/srv/wk")
        self.assertEqual(self.rows(env={"WK_HOME": "/srv/wk"})[1]["disk"][1], "100 GB free in /srv/wk, and the floor is 20")

    def test_a_workspace_has_no_row_of_this_lanes(self):
        self.w.workspace("x", on="lin")
        self.assertEqual(self.w.wk("doctor", "x").out, "x (workspace on lin)\n")


class TestControllerRows(Own):
    def setUp(self):
        super().setUp()
        self.vault = self.w.machines["vault"]
        controller(self.vault)

    def rows(self):
        return rows(self.w.wk("doctor", on="vault").out)

    def test_a_controller_with_its_tools_and_the_pinned_websocat_is_all_ok(self):
        r = self.w.wk("doctor", on="vault")
        self.assertEqual((r.code, r.out), (0, CONTROLLER))

    def test_ansible_is_asked_in_the_locale_its_playbooks_run_in(self):
        self.w.wk("doctor", on="vault")
        self.assertEqual([c.env for c in self.vault.calls if c.argv[0] == "ansible-playbook"], [ansible.LOCALE])

    def test_each_tool_that_is_absent_names_its_package(self):
        for tool, argv, package in (("ansible", ("ansible-playbook", "--version"), "ansible"), ("xorriso", ("xorriso", "-version"), "xorriso"),
                                    ("qemu-nbd", ("qemu-nbd", "--version"), "qemu-utils"), ("ssh", ("ssh", "-V"), "openssh-client"),
                                    ("git", ("git", "--version"), "git")):
            self.vault.on(argv, Result(127, "", "sh: %s: command not found\n" % argv[0]))
            self.assertEqual(self.rows()[tool], ("missing", "sh: %s: command not found" % argv[0], "sudo apt install " + package))

    def test_a_websocat_not_fetched_is_missing_and_the_controller_stage_fetches_it(self):
        del self.vault.files[ansible.websocat(HOME)]
        self.assertEqual(self.rows()["websocat"], ("missing", ansible.websocat(HOME), "wk machine setup vault --stage controller"))

    def test_a_tailscale_that_is_stopped_is_missing_and_one_that_hangs_is_unknown(self):
        self.vault.on(("tailscale", "status"), Result(1, "", "Tailscale is stopped.\n"))
        self.assertEqual(self.rows()["tailscale"], ("missing", "Tailscale is stopped.", "install Tailscale, then `tailscale up`"))
        self.vault.on(("tailscale", "status"), Result(124, "", "timed out after 4s"))
        self.assertEqual(self.rows()["tailscale"][:2], ("unknown", "`tailscale status --peers=false` did not answer in 4s"))
