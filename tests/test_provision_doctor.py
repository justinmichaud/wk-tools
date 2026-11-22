import json
import unittest

from tests import ROOT
from tests.provision import DF, HOME, SHA, TOOLS, checkout, client, lanes, server
from tests.world import World
from wk.core.machine import Result

SMALL = {"desk": "kind=client", "lin": "kind=linux", "mac": "kind=mac"}
ENV = {"BAO_ADDR": "https://vault:8200", "BAO_TOKEN": "t"}
CLIENT = ("desk\n  ok       ssh            OpenSSH_9.6\n  ok       git            git version 2.43.0\n"
          "  ok       tailscale      100.64.0.1 desk\n  ok       BAO_ADDR       https://vault:8200\n  ok       store token    BAO_TOKEN\n")


class Toy(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.toy")
        self.w = World(confs=SMALL)
        client(self.w.machines["desk"])
        for name in ("lin", "mac"):
            self.w.machines[name].on(("true",), "")
        self.lin = self.w.machines["lin"]

    def doctor(self, *words, **how):
        return self.w.wk("doctor", *words, env=ENV, **how)


class TestDoctor(Toy):
    def test_on_the_client_it_reports_the_client_then_each_server_as_a_group(self):
        self.lin.files["/rows/mirror"] = "ok|fetched today|wk sync"
        r = self.doctor()
        self.assertEqual((r.code, r.out), (0, CLIENT + "lin\n  ok       reachable\n  ok       mirror         fetched today\nmac\n  ok       reachable\n"))

    def test_it_changes_nothing(self):
        self.assertEqual(self.doctor().effects, [])

    def test_a_missing_row_is_shouted_with_its_remedy_and_the_exit_code_is_1(self):
        self.lin.files.update({"/rows/pueue": "missing|no pueue|wk machine setup lin --stage pueue", "/rows/sdk": "unknown|podman died|"})
        r = self.doctor()
        self.assertEqual(r.code, 1)
        self.assertIn("lin\n  ok       reachable\n  MISSING  pueue          no pueue\n           fix: wk machine setup lin --stage pueue\n"
                      "  unknown  sdk            podman died\nmac\n", r.out)

    def test_an_unknown_row_with_nothing_missing_needs_a_person(self):
        self.lin.files["/rows/sdk"] = "unknown|podman died|wk machine setup lin"
        r = self.doctor()
        self.assertEqual(r.code, 4)
        self.assertIn("  unknown  sdk            podman died\n           fix: wk machine setup lin\n", r.out)

    def test_an_unreachable_machine_is_one_unknown_row_and_its_other_rows_are_not_invented(self):
        self.lin.files["/rows/mirror"] = "ok||"
        self.lin.down = True
        r = self.doctor()
        self.assertEqual(r.code, 4)
        self.assertIn("lin\n  unknown  reachable      cannot reach lin: the fake is down\n           fix: tailscale status\nmac\n  ok       reachable\n", r.out)

    def test_json_prints_the_groups_and_keeps_the_exit_code(self):
        self.lin.files["/rows/pueue"] = "missing|no pueue|wk machine setup lin"
        r = self.doctor("--json")
        self.assertEqual((r.code, json.loads(r.out)[1]),
                         (1, {"group": "lin", "rows": [{"name": "reachable", "status": "ok", "detail": "", "remedy": ""},
                                                       {"name": "pueue", "status": "missing", "detail": "no pueue", "remedy": "wk machine setup lin"}]}))

    def test_on_a_server_it_reports_that_server_alone(self):
        self.lin.files["/rows/mirror"] = "ok|fetched today|"
        r = self.doctor(on="lin")
        self.assertEqual((r.code, r.out, r.calls), (0, "lin\n  ok       reachable\n  ok       mirror         fetched today\n", ["[lin] true"]))


class TestDoctorWorkspace(Toy):
    def test_a_workspace_is_reported_by_its_server_whose_hooks_get_the_workspace(self):
        self.w.workspace("x", on="mac")
        r = self.doctor("x")
        self.assertEqual((r.code, r.out), (0, "x (workspace on mac)\n  ok       checkout       /home/u/wk/ws/x\n"))
        self.assertEqual(r.calls[-2:], ["[mac] wk doctor x --json", "[mac] wk ls --json"])

    def test_a_workspace_no_server_holds_is_refused(self):
        self.assertEqual(self.doctor("x").err, "error: no workspace named x: `wk ls` lists them, `wk new x` makes it\n")

    def test_on_a_server_a_workspace_it_does_not_hold_is_refused(self):
        self.w.workspace("x", on="mac")
        r = self.doctor("x", on="lin")
        self.assertEqual((r.code, r.err), (1, "error: no workspace named x on lin: `wk ls` lists them\n"))

    def test_a_server_whose_doctor_does_not_answer_in_json_is_an_unknown_row(self):
        self.w.workspace("x", on="mac")
        self.w.machines["mac"].on(("wk", "doctor"), Result(127, "", "sh: wk: command not found\n"))
        r = self.doctor("x")
        self.assertEqual((r.code, r.out), (4, "x\n  unknown  doctor         `wk doctor x --json` on mac answered sh: wk: command not found\n"
                                              "           fix: wk machine setup mac\n"))

    def test_all_adds_every_workspace_after_the_machines(self):
        self.w.workspace("x", on="mac")
        self.w.workspace("y", on="lin")
        r = self.doctor("--all")
        self.assertEqual((r.code, r.out), (0, CLIENT + "lin\n  ok       reachable\nmac\n  ok       reachable\n"
                                              "y (workspace on lin)\n  ok       checkout       /home/u/wk/ws/y\n"
                                              "x (workspace on mac)\n  ok       checkout       /home/u/wk/ws/x\n"))

    def test_all_names_a_server_whose_workspaces_could_not_be_listed(self):
        self.w.machines["mac"].on(("wk", "ls", "--json"), Result(127, "", "sh: wk: command not found\n"))
        r = self.doctor("--all")
        self.assertEqual(r.code, 4)
        self.assertTrue(r.out.endswith("mac\n  unknown  workspaces     `wk ls --json` did not answer\n           fix: wk machine setup mac\n"))

    def test_on_a_server_all_adds_its_own_workspaces(self):
        self.w.workspace("y", on="lin")
        self.assertEqual(self.doctor("--all", on="lin").out, "lin\n  ok       reachable\ny (workspace on lin)\n  ok       checkout       /home/u/wk/ws/y\n")


class Own(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.own")
        self.w = World(confs=SMALL)
        checkout(self.w.machines["desk"])
        server(self.w.machines["lin"])
        self.lin = self.w.machines["lin"]

    def rows(self, **how):
        r = self.w.wk("doctor", "--json", on="lin", **how)
        return r.code, {row["name"]: (row["status"], row["detail"], row["remedy"]) for row in json.loads(r.out)[0]["rows"]}


class TestServerRows(Own):
    def setUp(self):
        super().setUp()
        checkout(self.lin)

    def test_a_server_that_was_set_up_is_all_ok(self):
        self.w.wk("machine", "setup", "lin")
        r = self.w.wk("doctor", on="lin")
        self.assertEqual((r.code, r.out), (0, "lin\n  ok       reachable\n  ok       python3        3.11\n  ok       git            git version 2.43.0\n"
                                              "  ok       wk commit      %s\n  ok       wk on PATH     %s/bin/wk\n  ok       layout\n"
                                              "  ok       disk           100 GB free in %s, and the floor is 20\n"
                                              "  ok       clock          0 s from this machine's, and the bound is 30\n" % (SHA, HOME, HOME)))

    def test_a_bare_server_lacks_the_commit_the_path_and_the_layout_and_setup_is_the_remedy(self):
        code, rows = self.rows()
        remedy = "wk machine setup lin"
        self.assertEqual(code, 1)
        self.assertEqual((rows["wk commit"], rows["wk on PATH"]), (("missing", "lin has no checkout, and this checkout is at %s" % SHA, remedy),
                                                                  ("missing", "the login shell finds no wk", remedy)))
        self.assertEqual(rows["layout"], ("missing", "absent or wrongly moded: %s" % ", ".join(
            "%s/%s (%s)" % (HOME, name, mode) for name, mode in (("bin", "755"), ("cache", "755"), ("git", "755"), ("ws", "755"), ("secrets", "700"))), remedy))

    def test_another_commit_or_an_edited_tree_is_missing(self):
        self.w.wk("machine", "setup", "lin")
        self.lin.files[TOOLS + "/edited"] = "x"
        self.assertEqual(self.rows()[1]["wk commit"][1], "lin has %s with changes, and this checkout is at %s" % (SHA, SHA))

    def test_a_checkout_with_no_commit_to_compare_with_is_unknown(self):
        self.w.wk("machine", "setup", "lin")
        checkout(self.lin, sha="")
        code, rows = self.rows()
        self.assertEqual((code, rows["wk commit"][:2]), (4, ("unknown", "%s has no commit to compare with" % ROOT)))

    def test_from_the_client_the_commit_is_compared_with_the_clients(self):
        self.w.wk("machine", "setup", "lin")
        client(self.w.machines["desk"])
        server(self.w.machines["mac"])
        checkout(self.w.machines["desk"], sha="ab" * 20)
        r = self.w.wk("doctor", env=ENV)
        self.assertEqual(r.code, 1)
        self.assertIn("  MISSING  wk commit      lin has %s, and this checkout is at %s\n           fix: wk machine setup lin\n" % (SHA, "ab" * 20), r.out)

    def test_a_python_that_is_absent_is_missing_and_one_that_does_not_answer_is_unknown(self):
        self.lin.on(("python3",), Result(127, "", "sh: python3: command not found\n"))
        self.assertEqual(self.rows()[1]["python3"], ("missing", "sh: python3: command not found", "install it on lin with the system's package manager"))
        self.lin.on(("python3",), Result(124, "", "timed out after 30s"))
        self.assertEqual(self.rows()[1]["python3"][:2], ("unknown", "`python3 -c 'import sys; print('\"'\"'%d.%d'\"'\"' % sys.version_info[:2])'` did not answer in 30s"))

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
        self.assertEqual(json.loads(self.w.wk("doctor", "x", "--json", on="lin").out), [{"group": "x (workspace on lin)", "rows": []}])


class TestClientRows(Own):
    def setUp(self):
        super().setUp()
        self.desk = self.w.machines["desk"]
        client(self.desk)
        for name in ("lin", "mac"):
            self.w.machines[name].down = True

    def rows(self, env):
        out = self.w.wk("doctor", "--json", env=env).out
        return {row["name"]: (row["status"], row["detail"], row["remedy"]) for row in json.loads(out)[0]["rows"]}

    def test_the_store_address_and_a_token_are_checked_for_presence_only(self):
        rows = self.rows({})
        self.assertEqual(rows["BAO_ADDR"], ("missing", "not set", "export BAO_ADDR=<the credential server's OpenBao address> in your shell's profile"))
        self.assertEqual(rows["store token"], ("missing", "no BAO_TOKEN and no ~/.vault-token", "bao login"))

    def test_a_token_file_is_a_token(self):
        self.desk.files["/home/u/.vault-token"] = "t"
        self.assertEqual(self.rows({})["store token"][:2], ("ok", "~/.vault-token"))

    def test_a_tailscale_that_is_stopped_or_absent_is_missing_and_one_that_hangs_is_unknown(self):
        self.desk.on(("tailscale", "status"), Result(1, "", "Tailscale is stopped.\n"))
        self.assertEqual(self.rows(ENV)["tailscale"], ("missing", "Tailscale is stopped.", "install Tailscale, then `tailscale up`"))
        self.desk.on(("tailscale", "status"), Result(124, "", "timed out after 4s"))
        self.assertEqual(self.rows(ENV)["tailscale"][:2], ("unknown", "`tailscale status --peers=false` did not answer in 4s"))

    def test_ssh_and_git_are_checked(self):
        self.desk.on(("ssh", "-V"), Result(127, "", "sh: ssh: command not found\n"))
        rows = self.rows(ENV)
        self.assertEqual((rows["ssh"], rows["git"][:2]), (("missing", "sh: ssh: command not found", "install OpenSSH"), ("ok", "git version 2.43.0")))
