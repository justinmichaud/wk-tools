"""tests/support.py's own environment scrub: every test that shells out
through run()/bash() gets a WK_TARGET_REGISTRY and a WK_HOST_SECRETS that
point at scratch directories, never at the real fleet registry or the real
~/.config/wk/secrets -- a test that forgot to pass its own would otherwise
read or write machine state silently.

Run: python3 -m unittest tests.test_support -v
"""
import unittest

from tests.support import NO_REGISTRY, NO_SECRETS, _clean_env


class TestCleanEnvScrubsMachineState(unittest.TestCase):
    def test_wk_target_registry_defaults_to_the_empty_fake_registry(self):
        self.assertEqual(_clean_env()["WK_TARGET_REGISTRY"], NO_REGISTRY)

    def test_wk_host_secrets_defaults_to_a_scratch_dir(self):
        env = _clean_env()
        self.assertEqual(env["WK_HOST_SECRETS"], NO_SECRETS)
        self.assertNotIn(".config/wk", env["WK_HOST_SECRETS"])

    def test_a_callers_own_value_still_wins(self):
        env = _clean_env({"WK_HOST_SECRETS": "/explicit/scratch/secrets"})
        self.assertEqual(env["WK_HOST_SECRETS"], "/explicit/scratch/secrets")


if __name__ == "__main__":
    unittest.main()
