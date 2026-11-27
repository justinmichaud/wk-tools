import os
import tempfile
import threading
import unittest

from wk.core import conf
from wk.core.errors import Refused
from wk.core.fakes import Fake
from wk.core.fleet import UNKNOWN, Fleet


class TestConf(unittest.TestCase):
    def test_a_conf_is_its_kind_its_role_and_its_other_literals(self):
        c = conf.parse("srv", "# a server\n\nkind=linux\nuser=me\njobs_cap=8\nfork=git@github.com:me/WebKit.git\n")
        self.assertEqual((c.name, c.kind, c.get("user"), c.get("jobs_cap"), c.get("fork"), c.get("absent", "d")),
                         ("srv", "linux", "me", "8", "git@github.com:me/WebKit.git", "d"))

    def test_the_kind_decides_the_driver_and_who_holds_workspaces(self):
        self.assertEqual([(k, conf.parse("m", "kind=" + k).driver) for k in ("linux", "mac")],
                         [("linux", "container"), ("mac", "plain")])
        self.assertEqual([conf.parse("m", text).holds_workspaces for text in ("kind=mac", "kind=client", "kind=credential")],
                         [True, False, False])

    def refused(self, text, why):
        with self.assertRaises(conf.ConfError) as caught:
            conf.parse("m", text, "machines/m.conf")
        self.assertEqual(str(caught.exception), why)

    def test_an_uppercase_key_is_refused(self):
        self.refused("KIND=linux", "machines/m.conf:1: not a key=value line with a lowercase key: KIND=linux")

    def test_a_line_that_is_not_an_assignment_is_refused(self):
        self.refused("kind=linux\nexport x", "machines/m.conf:2: not a key=value line with a lowercase key: export x")

    def test_a_value_that_is_not_one_literal_word_is_refused(self):
        for value in ("a b", "$HOME/x", "`hostname`", '"quoted"', "", "x # comment"):
            self.refused("kind=linux\nkey=%s" % value,
                         "machines/m.conf:2: key is not one literal word: no spaces, quotes, $ or backticks (a default belongs in code)")

    def test_a_key_set_twice_is_refused(self):
        self.refused("kind=linux\nkind=mac", "machines/m.conf:2: kind is set twice: delete one line")

    def test_a_missing_or_unknown_kind_is_refused_naming_the_kinds(self):
        self.refused("user=me", "machines/m.conf: kind=None is not a kind: set kind= to one of client, linux, mac, credential")
        self.refused("kind=peer", "machines/m.conf: kind=peer is not a kind: set kind= to one of client, linux, mac, credential")


class TestLoad(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def put(self, name, text):
        with open(os.path.join(self.dir, name), "w") as f:
            f.write(text)

    def test_every_conf_file_is_a_machine_and_other_files_are_not(self):
        self.put("b.conf", "kind=mac")
        self.put("a-1.conf", "kind=client")
        self.put("notes.txt", "not a conf")
        self.assertEqual({n: c.kind for n, c in conf.load(self.dir).items()}, {"a-1": "client", "b": "mac"})

    def test_a_missing_directory_is_refused_naming_the_remedy(self):
        with self.assertRaisesRegex(conf.ConfError, "no machines directory at .*/absent: create it"):
            conf.load(os.path.join(self.dir, "absent"))

    def test_a_file_not_named_like_a_tailnet_name_is_refused(self):
        self.put("My_Mac.conf", "kind=mac")
        with self.assertRaisesRegex(conf.ConfError, "My_Mac.conf: a machine is named for its tailnet name"):
            conf.load(self.dir)

    def test_two_clients_or_two_credential_servers_are_refused_naming_both(self):
        self.put("a.conf", "kind=client")
        self.put("b.conf", "kind=client")
        with self.assertRaisesRegex(conf.ConfError, "a and b say kind=client and only one machine may"):
            conf.load(self.dir)
        self.put("b.conf", "kind=credential")
        self.put("c.conf", "kind=credential")
        with self.assertRaisesRegex(conf.ConfError, "b and c say kind=credential and only one machine may"):
            conf.load(self.dir)

    def test_the_confs_in_the_tree_load(self):
        from tests import ROOT
        self.assertEqual({c.kind for c in conf.load(os.path.join(ROOT, "machines")).values()}, {"client", "linux", "credential"})


class TestFleet(unittest.TestCase):
    def setUp(self):
        self.confs = {n: conf.parse(n, t) for n, t in (("desk", "kind=client"), ("lin", "kind=linux"), ("mac", "kind=mac"),
                                                       ("vault", "kind=credential"))}
        self.fakes = {n: Fake(n) for n in self.confs}
        self.fleet = Fleet(lambda: self.confs, "lin", self.fakes.get, self.fakes["lin"], workers=4)

    def test_it_lists_machines_by_name_and_servers_are_those_that_hold_workspaces(self):
        self.assertEqual(([c.name for c in self.fleet.machines()], [c.name for c in self.fleet.servers()]),
                         (["desk", "lin", "mac", "vault"], ["lin", "mac"]))

    def test_here_is_the_conf_named_for_this_hostname(self):
        self.assertEqual((self.fleet.here.name, self.fleet.here.kind), ("lin", "linux"))

    def test_the_machine_named_for_this_host_is_the_local_one_and_another_is_connected_to(self):
        local = object()
        fleet = Fleet(lambda: self.confs, "lin", self.fakes.get, local)
        self.assertEqual((fleet.machine("lin"), fleet.machine("mac")), (local, self.fakes["mac"]))

    def test_the_confs_are_loaded_once_and_only_when_asked_for(self):
        loads = []
        fleet = Fleet(lambda: loads.append(1) or self.confs, "lin", self.fakes.get, None)
        self.assertEqual(loads, [])
        fleet.machines(), fleet.here
        self.assertEqual(loads, [1])

    def test_the_credential_server_is_the_one_with_the_role(self):
        self.assertEqual(self.fleet.credential.name, "vault")
        del self.confs["vault"]
        with self.assertRaisesRegex(Refused, "no credential server: set kind=credential"):
            self.fleet.credential

    def test_asking_reaches_every_server_and_an_unreachable_one_is_unknown_not_empty(self):
        self.fakes["lin"].files["/f"] = ""
        self.fakes["mac"].down = True
        answers = self.fleet.ask(lambda m: m.listdir("/"))
        self.assertEqual(answers, {"lin": ["f"], "mac": UNKNOWN})
        self.assertEqual(repr(UNKNOWN), "UNKNOWN")

    def test_asking_takes_the_names_it_is_given(self):
        self.assertEqual(self.fleet.ask(lambda m: m.name, ["vault", "desk"]), {"vault": "vault", "desk": "desk"})

    def test_the_servers_are_asked_at_the_same_time(self):
        together = threading.Barrier(2, timeout=10)
        self.assertEqual(sorted(self.fleet.ask(lambda m: together.wait()).values()), [0, 1])

    def test_an_error_that_is_not_unreachability_is_not_swallowed(self):
        with self.assertRaises(ZeroDivisionError):
            self.fleet.ask(lambda m: 1 / 0)
