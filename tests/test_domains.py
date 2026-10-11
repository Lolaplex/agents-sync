import unittest
from pathlib import Path

from agents_sync.domains import is_path_safe, DOMAINS, get_domain_root


class TestDomains(unittest.TestCase):
    def test_forbidden_keys_blocked(self):
        self.assertFalse(is_path_safe("keys/agent.ed25519"))
        self.assertFalse(is_path_safe("keys/secret.key"))
        self.assertFalse(is_path_safe("memory/keys/test.ed25519"))
        self.assertFalse(is_path_safe("any/path/id.ed25519"))

    def test_database_and_locks_blocked(self):
        self.assertFalse(is_path_safe("memory/.index/cache.sqlite"))
        self.assertFalse(is_path_safe("memory/store.db"))
        self.assertFalse(is_path_safe("memory/.sync.lock"))
        self.assertFalse(is_path_safe("memory/temp.tmp"))
        self.assertFalse(is_path_safe("sync_config.json"))
        self.assertFalse(is_path_safe("remote_config.json"))

    def test_allowed_domains_safe(self):
        self.assertTrue(is_path_safe("memory/notes/idea.md"))
        self.assertTrue(is_path_safe("skills/caveman/SKILL.md"))
        self.assertTrue(is_path_safe("docs/stacks/python.md"))
        self.assertTrue(is_path_safe("identity.json"))
        self.assertTrue(is_path_safe("known-dids.jsonl"))
        self.assertTrue(is_path_safe("traces/node1/2026-10-11.jsonl"))

    def test_domain_roots(self):
        root = Path("/tmp/agents")
        self.assertEqual(get_domain_root(root, "memory"), root / "memory")
        self.assertEqual(get_domain_root(root, "skills"), root / "skills")
        self.assertEqual(get_domain_root(root, "identity"), root / "identity.json")


if __name__ == "__main__":
    unittest.main()
