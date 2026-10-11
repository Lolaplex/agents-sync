import unittest
import json

from agents_sync.merge import merge_markdown, merge_json_identity, merge_file_content


class TestMerge(unittest.TestCase):
    def test_merge_markdown_bullets(self):
        doc1 = "# Ideas\n- Idea 1\n- Shared idea\n"
        doc2 = "# Ideas\n- Idea 2\n- Shared idea\n"
        merged = merge_markdown(doc1, doc2)
        self.assertIn("- Idea 1", merged)
        self.assertIn("- Idea 2", merged)
        self.assertIn("- Shared idea", merged)
        self.assertEqual(merged.count("- Shared idea"), 1)

    def test_merge_markdown_with_tombstone(self):
        doc1 = "# Tasks\n- Task to delete\n- Keep task\n"
        doc2 = "# Tasks\n- Keep task\n"
        merged = merge_markdown(doc1, doc2, tombstoned_bullets={"Task to delete"})
        self.assertNotIn("Task to delete", merged)
        self.assertIn("Keep task", merged)

    def test_merge_identity_json(self):
        id1 = json.dumps({
            "users": {
                "u_1": {"id": "u_1", "aliases": ["alias_1"], "active_session": "ses_1"}
            },
            "sessions": {
                "ses_1": {"id": "ses_1", "user": "u_1"}
            }
        })
        id2 = json.dumps({
            "users": {
                "u_1": {"id": "u_1", "aliases": ["alias_2"], "active_session": "ses_1"},
                "u_2": {"id": "u_2", "aliases": ["alias_3"], "active_session": "ses_2"}
            },
            "sessions": {
                "ses_2": {"id": "ses_2", "user": "u_2"}
            }
        })
        merged_str = merge_json_identity(id1, id2)
        merged = json.loads(merged_str)
        self.assertIn("u_1", merged["users"])
        self.assertIn("u_2", merged["users"])
        self.assertIn("alias_1", merged["users"]["u_1"]["aliases"])
        self.assertIn("alias_2", merged["users"]["u_1"]["aliases"])
        self.assertIn("ses_1", merged["sessions"])
        self.assertIn("ses_2", merged["sessions"])

    def test_merge_jsonl_events(self):
        from agents_sync.merge import merge_jsonl

        l1 = '{"ts": "2026-10-11T12:00:00Z", "session": "ses_1", "tool": "read_file"}\n'
        l2 = '{"ts": "2026-10-11T12:05:00Z", "session": "ses_2", "tool": "write_file"}\n'
        merged = merge_jsonl(l1, l2)
        lines = [json.loads(line) for line in merged.strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["session"], "ses_1")
        self.assertEqual(lines[1]["session"], "ses_2")


if __name__ == "__main__":
    unittest.main()
