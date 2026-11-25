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

    def test_secrets_and_control_files_blocked(self):
        for name in (
            "secrets.json",
            ".env",
            "calendar.json",
            "vapid.json",
            "remotes.json",
            "push.json",
        ):
            self.assertFalse(is_path_safe(name), name)
            self.assertFalse(is_path_safe(f"memory/{name}"), name)
        self.assertFalse(is_path_safe("secrets/token.txt"))
        self.assertFalse(is_path_safe("memory/secrets/a.md"))

    def test_sqlite_sidecars_blocked(self):
        for path in (
            "store.db-wal",
            "store.db-shm",
            "store.sqlite-wal",
            "store.sqlite-shm",
            "memory/store.db-wal",
            "memory/store.db-shm",
            "memory/store.sqlite-wal",
            "memory/store.sqlite-shm",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_new_domains_registered(self):
        for name in ("personas", "schedules", "config"):
            self.assertIn(name, DOMAINS)
        root = Path("/tmp/agents")
        self.assertEqual(get_domain_root(root, "personas"), root / "personas")
        self.assertEqual(get_domain_root(root, "schedules"), root / "schedules")
        self.assertEqual(get_domain_root(root, "config"), root / "config.json")
        self.assertTrue(DOMAINS["config"].is_file)

    def test_run_dir_blocked(self):
        self.assertFalse(is_path_safe("run/delegates/dlg_1/meta.json"))
        self.assertFalse(is_path_safe("run/procs/proc_1/out.log"))
        self.assertFalse(is_path_safe("run/delegate.depth"))
        self.assertFalse(is_path_safe("memory/run/ephemeral.txt"))

    def test_absolute_and_traversal_blocked(self):
        for path in (
            "/etc/passwd",
            "/etc/cron.d/x",
            "C:/Windows/win.ini",
            "C:\\foo\\bar",
            "C:rel.txt",
            "\\\\server\\share\\x.md",
            "\\tmp\\evil.md",
            "../../known-dids.jsonl",
            "..\\..\\secrets.json",
            "memory/../../escape.md",
            "../memory/notes.md",
            "memory/sub/../../../x.md",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_traversal_checked_before_known_dids_exemption(self):
        self.assertFalse(is_path_safe("/etc/known-dids.jsonl"))
        self.assertFalse(is_path_safe("../../known-dids.jsonl"))
        self.assertFalse(is_path_safe("..\\known-dids.jsonl"))
        self.assertTrue(is_path_safe("known-dids.jsonl"))
        self.assertTrue(is_path_safe("keys/known-dids.jsonl"))

    def test_case_variants_blocked(self):
        for path in (
            "Secrets.json",
            "memory/SECRETS.JSON",
            "KEYS/agent.ED25519",
            "Keys/Agent.Ed25519",
            "memory/Keys/k.pem",
            "SYNC_PEERS.json",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_workspace_internal_files_blocked(self):
        for path in (
            "sync_peers.json",
            "workspace.json",
            "hubs.json",
            "devices.json",
            "users.json",
            "agents.json",
            "memory/sync_peers.json",
            "memory/workspace.json",
            "traces/hubs.json",
            ".tombstones.json",
            ".sync-baseline.json",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_db_journal_and_key_material_blocked(self):
        for path in (
            "store.db-journal",
            "memory/store.db-journal",
            "memory/store.sqlite-journal",
            "memory/store.db-wal",
            "memory/node.key",
            "memory/node.pem",
            "skills/agent.ed25519",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_unknown_roots_blocked(self):
        for path in (
            "random/evil.md",
            "etc/passwd",
            "unknown/nested/deep.md",
            "notadomain.json",
            "",
            "   ",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_allowed_domains_safe(self):
        self.assertTrue(is_path_safe("memory/notes/idea.md"))
        self.assertTrue(is_path_safe("skills/caveman/SKILL.md"))
        self.assertTrue(is_path_safe("known-dids.jsonl"))
        self.assertTrue(is_path_safe("traces/node1/2026-10-11.jsonl"))

    def test_dead_allowlist_entries_removed(self):
        """docs/ and identity.json are not registered Plane 1 domains;
        keeping them allowlisted would silently widen the wire surface."""
        self.assertFalse(is_path_safe("docs/stacks/python.md"))
        self.assertFalse(is_path_safe("docs"))
        self.assertFalse(is_path_safe("identity.json"))

    def test_bare_dirs_and_windows_edge_spellings_blocked(self):
        """Wire keys name files, never bare directories; trailing dots/
        spaces, NUL and ':' must not bypass name or extension checks."""
        for path in (
            "memory",
            "memory/",
            "memory/nested/",
            "memory/secrets.json.",
            "memory/store.db-wal.",
            "memory/notes.md ",
            "memory/dir./notes.md",
            "memory/dir /notes.md",
            "memory/nul\x00name.md",
            "memory/alternate:stream.md",
            "memory:stream.md",
        ):
            self.assertFalse(is_path_safe(path), path)

    def test_domain_roots(self):
        root = Path("/tmp/agents")
        self.assertEqual(get_domain_root(root, "memory"), root / "memory")
        self.assertEqual(get_domain_root(root, "skills"), root / "skills")


if __name__ == "__main__":
    unittest.main()
