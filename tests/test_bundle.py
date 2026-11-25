import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents_sync.bundle import collect_bundle, apply_bundle
from agents_sync.tombstones import load_tombstones, mtime_iso, record_file_deletion


class TestBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_bundle_across_domains(self):
        # Create domain dirs
        (self.root / "memory" / "notes").mkdir(parents=True)
        (self.root / "memory" / "notes" / "todo.md").write_text("# Todo\n- buy milk\n", encoding="utf-8")

        (self.root / "skills" / "caveman").mkdir(parents=True)
        (self.root / "skills" / "caveman" / "SKILL.md").write_text("name: caveman", encoding="utf-8")
        (self.root / "members.json").write_text('{"members": [{"id": "u_1", "role": "owner"}]}\n', encoding="utf-8")


        # Create forbidden key (must be skipped)
        (self.root / "keys").mkdir(parents=True)
        (self.root / "keys" / "secret.ed25519").write_text("SECRET_BYTES", encoding="utf-8")

        stamps = {}
        bundle = collect_bundle(self.root, stamps=stamps)

        self.assertIn("memory/notes/todo.md", bundle)
        self.assertIn("skills/caveman/SKILL.md", bundle)
        self.assertIn("members.json", bundle)
        self.assertNotIn("keys/secret.ed25519", bundle)

    def test_apply_bundle_and_tombstones(self):
        remote_bundle = {
            "memory/notes/remote.md": "# Remote\nContent\n",
            "skills/ponytail/SKILL.md": "name: ponytail",
        }
        written, deleted = apply_bundle(self.root, remote_bundle)
        self.assertIn("memory/notes/remote.md", written)
        self.assertIn("skills/ponytail/SKILL.md", written)
        self.assertTrue((self.root / "memory" / "notes" / "remote.md").is_file())

        # Now tombstone a file and apply
        record_file_deletion(self.root, "memory/notes/remote.md")
        tombs = load_tombstones(self.root)

        written2, deleted2 = apply_bundle(self.root, remote_bundle, remote_tombstones=tombs)
        self.assertIn("memory/notes/remote.md", deleted2)
        self.assertFalse((self.root / "memory" / "notes" / "remote.md").is_file())

    def test_apply_bundle_rejects_traversal_keys(self):
        remote_bundle = {
            "../escape.md": "escaped",
            "memory/../../escape2.md": "escaped",
            "/etc/abs.md": "absolute",
            "C:/evil/win.md": "drive",
            "..\\up.md": "backslash",
        }
        written, deleted = apply_bundle(self.root, remote_bundle)
        self.assertEqual(written, [])
        self.assertEqual(deleted, [])
        self.assertFalse((self.root.parent / "escape.md").exists())
        self.assertFalse((self.root.parent / "escape2.md").exists())
        self.assertFalse((self.root / "up.md").exists())

    def test_apply_bundle_rejects_symlink_escape(self):
        outside_dir = Path(self.tmp.name).parent / f"{Path(self.tmp.name).name}-outside"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(outside_dir, ignore_errors=True))
        outside = outside_dir / "outside.md"
        outside.write_text("original", encoding="utf-8")
        link_dir = self.root / "memory"
        link_dir.mkdir(parents=True)
        link = link_dir / "link.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        written, _ = apply_bundle(self.root, {"memory/link.md": "pwned"})
        self.assertNotIn("memory/link.md", written)
        self.assertEqual(outside.read_text(encoding="utf-8"), "original")

    def test_apply_bundle_rejects_symlinked_domain_dir(self):
        outside_dir = Path(self.tmp.name).parent / f"{Path(self.tmp.name).name}-outside2"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(outside_dir, ignore_errors=True))
        try:
            os.symlink(outside_dir, self.root / "memory", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        written, _ = apply_bundle(self.root, {"memory/evil.md": "pwned"})
        self.assertNotIn("memory/evil.md", written)
        self.assertFalse((outside_dir / "evil.md").exists())

    def test_apply_bundle_deletes_tombstoned_key_absent_from_remote_bundle(self):
        victim = self.root / "memory" / "gone.md"
        victim.parent.mkdir(parents=True)
        victim.write_text("bye", encoding="utf-8")
        remote_tombs = {
            "version": 1,
            "files": {"memory/gone.md": "2999-01-01T00:00:00Z"},
            "prefixes": {},
            "bullets": {},
        }
        # Deleted key deliberately NOT present in remote_bundle.
        written, deleted = apply_bundle(self.root, {}, remote_tombstones=remote_tombs)
        self.assertIn("memory/gone.md", deleted)
        self.assertFalse(victim.exists())

    def test_apply_bundle_expired_tombstone_does_not_delete(self):
        victim = self.root / "memory" / "ancient.md"
        victim.parent.mkdir(parents=True)
        victim.write_text("still here", encoding="utf-8")
        remote_tombs = {
            "version": 1,
            "files": {"memory/ancient.md": "2000-01-01T00:00:00Z"},
            "prefixes": {},
            "bullets": {},
        }
        written, deleted = apply_bundle(self.root, {}, remote_tombstones=remote_tombs)
        self.assertNotIn("memory/ancient.md", deleted)
        self.assertTrue(victim.exists())

    def test_apply_bundle_rejects_intra_workspace_symlink_escape(self):
        """memory/evil.md -> ../secrets.json stays inside agents_dir after
        resolution, so containment alone passes; the resolved relative path
        must still fail is_path_safe before any write."""
        secret = self.root / "secrets.json"
        secret.write_text('{"token":"original"}', encoding="utf-8")
        mem = self.root / "memory"
        mem.mkdir(parents=True)
        try:
            os.symlink(secret, mem / "evil.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        written, _ = apply_bundle(self.root, {"memory/evil.md": "pwned"})
        self.assertNotIn("memory/evil.md", written)
        self.assertEqual(secret.read_text(encoding="utf-8"), '{"token":"original"}')

    def test_enabled_domains_apply_to_post_resolution_symlink_target(self):
        """A memory/ wire key must not become a config.json write merely
        because a local symlink resolves there; domain scoping applies to
        the resolved target as well as the wire spelling."""
        config = self.root / "config.json"
        config.write_text('{"original":true}', encoding="utf-8")
        mem = self.root / "memory"
        mem.mkdir(parents=True)
        try:
            os.symlink(config, mem / "evil.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        written, _ = apply_bundle(
            self.root,
            {"memory/evil.md": '{"pwned":true}'},
            enabled_domains=["memory"],
        )
        self.assertNotIn("memory/evil.md", written)
        self.assertEqual(config.read_text(encoding="utf-8"), '{"original":true}')

    def test_enabled_domains_filter_writes_and_tombstone_deletes(self):
        """domains=['memory'] must scope both remote writes and pass-2
        deletion enforcement; skills/ content is neither written nor deleted."""
        (self.root / "memory").mkdir(parents=True)
        (self.root / "skills" / "caveman").mkdir(parents=True)
        (self.root / "memory" / "gone.md").write_text("memory", encoding="utf-8")
        (self.root / "skills" / "caveman" / "gone.md").write_text("skill", encoding="utf-8")
        remote_bundle = {
            "memory/new.md": "memory new",
            "skills/caveman/new.md": "skill new",
        }
        remote_tombs = {
            "version": 1,
            "files": {
                "memory/gone.md": "2999-01-01T00:00:00Z",
                "skills/caveman/gone.md": "2999-01-01T00:00:00Z",
            },
            "prefixes": {},
            "bullets": {},
        }
        written, deleted = apply_bundle(
            self.root,
            remote_bundle,
            remote_tombstones=remote_tombs,
            enabled_domains=["memory"],
        )
        self.assertEqual(written, ["memory/new.md"])
        self.assertEqual(deleted, ["memory/gone.md"])
        self.assertTrue((self.root / "memory" / "new.md").is_file())
        self.assertFalse((self.root / "memory" / "gone.md").exists())
        self.assertFalse((self.root / "skills" / "caveman" / "new.md").exists())
        self.assertTrue((self.root / "skills" / "caveman" / "gone.md").is_file())

    def test_applied_file_preserves_remote_mtime_for_tombstone_ordering(self):
        """The local sync wall clock must not become the write stamp. A file
        written before a later tombstone stays deleted instead of being
        resurrected merely because sync applied it today."""
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        remote_stamp = (now - timedelta(days=2)).strftime(fmt)
        tomb_stamp = (now - timedelta(days=1)).strftime(fmt)
        written, _ = apply_bundle(
            self.root,
            {"memory/ordered.md": "remote"},
            remote_stamps={"memory/ordered.md": remote_stamp},
        )
        self.assertEqual(written, ["memory/ordered.md"])
        target = self.root / "memory" / "ordered.md"
        self.assertEqual(mtime_iso(target), remote_stamp)

        tombs = {
            "version": 1,
            "files": {"memory/ordered.md": tomb_stamp},
            "prefixes": {},
            "bullets": {},
        }
        _, deleted = apply_bundle(self.root, {}, remote_tombstones=tombs)
        self.assertIn("memory/ordered.md", deleted)
        self.assertFalse(target.exists())

    def test_collect_bundle_skips_escaping_symlinked_file(self):
        outside_dir = Path(self.tmp.name).parent / f"{Path(self.tmp.name).name}-outside3"
        outside_dir.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(outside_dir, ignore_errors=True))
        outside = outside_dir / "secret.md"
        outside.write_text("host secret", encoding="utf-8")
        mem = self.root / "memory"
        mem.mkdir(parents=True)
        try:
            os.symlink(outside, mem / "leak.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this host")
        bundle = collect_bundle(self.root)
        self.assertNotIn("memory/leak.md", bundle)
        self.assertNotIn("host secret", bundle.values())


if __name__ == "__main__":
    unittest.main()
