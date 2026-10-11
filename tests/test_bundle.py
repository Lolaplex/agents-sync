import tempfile
import unittest
from pathlib import Path

from agents_sync.bundle import collect_bundle, apply_bundle
from agents_sync.tombstones import load_tombstones, record_file_deletion


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

        (self.root / "identity.json").write_text('{"users": {}}', encoding="utf-8")

        # Create forbidden key (must be skipped)
        (self.root / "keys").mkdir(parents=True)
        (self.root / "keys" / "secret.ed25519").write_text("SECRET_BYTES", encoding="utf-8")

        stamps = {}
        bundle = collect_bundle(self.root, stamps=stamps)

        self.assertIn("memory/notes/todo.md", bundle)
        self.assertIn("skills/caveman/SKILL.md", bundle)
        self.assertIn("identity.json", bundle)
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


if __name__ == "__main__":
    unittest.main()
