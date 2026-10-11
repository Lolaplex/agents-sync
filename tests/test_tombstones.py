import tempfile
import unittest
from pathlib import Path

from agents_sync.tombstones import (
    load_tombstones,
    save_tombstones,
    merge_tombstones,
    record_file_deletion,
    record_bullet_deletion,
    file_is_tombstoned,
)


class TestTombstones(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_check_deletion(self):
        record_file_deletion(self.root, "memory/old.md")
        t = load_tombstones(self.root)
        self.assertIn("memory/old.md", t["files"])
        self.assertTrue(file_is_tombstoned(t, "memory/old.md"))

    def test_newer_write_resurrects(self):
        record_file_deletion(self.root, "memory/resurrect.md", iso_ts="2026-10-01T10:00:00Z")
        t = load_tombstones(self.root)
        # Write happened AFTER tombstone
        self.assertFalse(file_is_tombstoned(t, "memory/resurrect.md", write_stamp="2026-10-05T12:00:00Z"))
        # Write happened BEFORE tombstone
        self.assertTrue(file_is_tombstoned(t, "memory/resurrect.md", write_stamp="2026-09-20T12:00:00Z"))

    def test_merge_tombstones(self):
        t1 = {"files": {"a.md": "2026-10-01T00:00:00Z"}, "prefixes": {}, "bullets": {}}
        t2 = {"files": {"a.md": "2026-10-02T00:00:00Z", "b.md": "2026-10-01T00:00:00Z"}, "prefixes": {}, "bullets": {}}
        merged = merge_tombstones(t1, t2)
        self.assertEqual(merged["files"]["a.md"], "2026-10-02T00:00:00Z")
        self.assertEqual(merged["files"]["b.md"], "2026-10-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
