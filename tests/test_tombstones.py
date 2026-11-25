import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_sync.tombstones import (
    load_tombstones,
    save_tombstones,
    merge_tombstones,
    prune_tombstones,
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

    def test_prune_expired_tombstones(self):
        tombs = {
            "version": 1,
            "files": {
                "memory/old.md": "2000-01-01T00:00:00Z",
                "memory/fresh.md": "2999-01-01T00:00:00Z",
                "memory/malformed.md": "not-a-date",
            },
            "prefixes": {"memory/stale/": "2001-06-01T00:00:00Z"},
            "bullets": {
                "memory/notes.md": {
                    "old bullet": "2000-01-01T00:00:00Z",
                    "new bullet": "2999-01-01T00:00:00Z",
                }
            },
        }
        pruned = prune_tombstones(tombs)
        self.assertNotIn("memory/old.md", pruned["files"])
        self.assertIn("memory/fresh.md", pruned["files"])
        # Malformed stamps are kept (fail safe: never resurrect by accident).
        self.assertIn("memory/malformed.md", pruned["files"])
        self.assertNotIn("memory/stale/", pruned["prefixes"])
        bullets = pruned["bullets"].get("memory/notes.md", {})
        self.assertNotIn("old bullet", bullets)
        self.assertIn("new bullet", bullets)

    def test_save_prunes_before_atomic_replace(self):
        """Persistence itself owns pruning + atomic replace: callers cannot
        accidentally save expired entries or a half-written ledger."""
        tombs = {
            "version": 1,
            "files": {
                "memory/expired.md": "2000-01-01T00:00:00Z",
                "memory/fresh.md": "2999-01-01T00:00:00Z",
            },
            "prefixes": {},
            "bullets": {},
        }
        with patch("agents_sync.tombstones.os.replace", wraps=os.replace) as replace:
            save_tombstones(self.root, tombs)
        self.assertTrue(replace.called)
        saved = load_tombstones(self.root)
        self.assertNotIn("memory/expired.md", saved["files"])
        self.assertIn("memory/fresh.md", saved["files"])


if __name__ == "__main__":
    unittest.main()
