"""Crash safety and the dry run of a mutating command, enumerated.

    def make(): w = World(); w.workspace("x"); return w
    converges(self, make, "rm", "x", "--yes")              # killed after every effect and re-run: the same final state
    dry_run_is_the_plan(self, make, "rm", "x", "--yes")    # --dry-run lists exactly the wet run's effects, changes nothing"""

from wk.core.fakes import Killed


def converges(case, make, *words, limit=50):
    """Type `words` into make()'s world, killed after effect n for every n, then typed again: each must reach the
    state an uninterrupted run leaves. A kill that already left that state has converged and is not re-run."""
    whole = make()
    case.assertEqual(whole.wk(*words).code, 0, "the uninterrupted run fails")
    want = whole.state()
    for n in range(limit):
        world = make()
        world.journal.stop_after = n
        try:
            world.wk(*words)
        except Killed:
            pass
        else:
            return
        world.journal.stop_after = None
        if world.state() != want:
            world.wk(*words)
            case.assertEqual(world.state(), want, "killed after effect %d" % n)
    case.fail("the command makes more than %d effects" % limit)


def dry_run_is_the_plan(case, make, *words):
    wet = make().wk(*words)
    world = make()
    before = world.state()
    dry = world.wk("--dry-run", *words)
    case.assertTrue(wet.effects, "the wet run changes nothing")
    case.assertEqual(dry.plan, wet.effects)
    case.assertEqual((dry.effects, world.state()), ([], before))
