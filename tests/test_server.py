import os
import re
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch


def _test_client(app):
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")
    return TestClient(app)


class TestServerAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _app(self, **kw):
        from agents_sync.server import create_app

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return create_app(vault_dir=self.root, **kw)

    def test_empty_token_refuses_startup(self):
        with patch.dict(os.environ, {"AGENTS_SYNC_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                self._app()

    def test_empty_token_dev_mode_returns_401(self):
        with patch.dict(os.environ, {"AGENTS_SYNC_TOKEN": ""}):
            app = self._app(allow_insecure_dev=True)
        client = _test_client(app)
        self.assertEqual(client.get("/api/v1/status").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/v1/status", headers={"Authorization": "Bearer x"}
            ).status_code,
            401,
        )
        self.assertEqual(
            client.post("/api/v1/merge", json={}).status_code, 401
        )

    def test_missing_and_wrong_bearer_rejected(self):
        app = self._app(secret_token="right-token")
        client = _test_client(app)
        self.assertEqual(client.get("/api/v1/status").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/v1/status", headers={"Authorization": "Bearer wrong"}
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                "/api/v1/status",
                headers={"Authorization": "Bearer right-token"},
            ).status_code,
            200,
        )

    def test_create_app_emits_deprecation_warning(self):
        from agents_sync.server import create_app

        with self.assertWarns(DeprecationWarning):
            create_app(vault_dir=self.root, secret_token="tok")

    def test_merge_partitions_per_workspace(self):
        app = self._app(secret_token="tok")
        client = _test_client(app)
        auth = {"Authorization": "Bearer tok"}
        payload = {
            "bundle": {"memory/alpha.md": "alpha"},
            "stamps": {"memory/alpha.md": "2026-01-01T00:00:00Z"},
            "tombstones": {"files": {}, "prefixes": {}, "bullets": {}},
        }
        r = client.post(
            "/api/v1/merge?workspace=alpha", json=payload, headers=auth
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            (self.root / "workspaces" / "alpha" / "memory" / "alpha.md").is_file()
        )
        self.assertFalse(
            (self.root / "workspaces" / "beta" / "memory" / "alpha.md").exists()
        )

        # Header-based workspace selection.
        r2 = client.post(
            "/api/v1/merge",
            json=payload,
            headers={**auth, "X-Agents-Workspace": "beta"},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(
            (self.root / "workspaces" / "beta" / "memory" / "alpha.md").is_file()
        )

    def test_workspace_slug_sanitized(self):
        app = self._app(secret_token="tok")
        client = _test_client(app)
        auth = {"Authorization": "Bearer tok"}
        for bad in ("../x", "a/b", "a\\b", ".hidden", "a" * 100, "workspace name"):
            r = client.get(
                "/api/v1/snapshot",
                params={"workspace": bad},
                headers=auth,
            )
            self.assertEqual(r.status_code, 400, bad)
        # Empty/absent workspace falls back to the "default" partition.
        r = client.get("/api/v1/snapshot", params={"workspace": ""}, headers=auth)
        self.assertEqual(r.status_code, 200)

    def test_merge_filters_response_domains(self):
        app = self._app(secret_token="tok")
        client = _test_client(app)
        auth = {"Authorization": "Bearer tok"}
        ws_root = self.root / "workspaces" / "default"
        skill_victim = ws_root / "skills" / "s" / "gone.md"
        memory_victim = ws_root / "memory" / "gone.md"
        skill_victim.parent.mkdir(parents=True, exist_ok=True)
        memory_victim.parent.mkdir(parents=True, exist_ok=True)
        skill_victim.write_text("skill", encoding="utf-8")
        memory_victim.write_text("memory", encoding="utf-8")
        payload = {
            "bundle": {"memory/a.md": "a", "skills/s/SKILL.md": "s"},
            "stamps": {},
            "tombstones": {
                "files": {
                    "skills/s/gone.md": "2999-01-01T00:00:00Z",
                    "memory/gone.md": "2999-01-01T00:00:00Z",
                },
                "prefixes": {},
                "bullets": {},
            },
            "domains": ["memory"],
        }
        r = client.post("/api/v1/merge", json=payload, headers=auth)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("memory/a.md", body["bundle"])
        self.assertNotIn("skills/s/SKILL.md", body["bundle"])
        # The requested domain scopes writes too: skills/ was not stored.
        self.assertFalse(
            (self.root / "workspaces" / "default" / "skills" / "s" / "SKILL.md").exists()
        )
        # Tombstone enforcement is scoped the same way: memory is deleted,
        # skills/ survives because it was outside the requested domains.
        self.assertFalse(memory_victim.exists())
        self.assertTrue(skill_victim.is_file())

    def test_merge_rejects_malformed_json_shapes(self):
        """Malformed bundle/stamps/tombstones shapes are HTTP 400 input
        refusals, never unhandled 500s inside merge/apply."""
        app = self._app(secret_token="tok")
        client = _test_client(app)
        auth = {"Authorization": "Bearer tok"}
        valid = {
            "bundle": {},
            "stamps": {},
            "tombstones": {"files": {}, "prefixes": {}, "bullets": {}},
        }
        bad_payloads = []
        for field in ("bundle", "stamps", "tombstones"):
            payload = dict(valid)
            payload[field] = []
            bad_payloads.append(payload)
        nested = dict(valid)
        nested["tombstones"] = {"files": [], "prefixes": {}, "bullets": {}}
        bad_payloads.append(nested)
        bad_stamp = dict(valid)
        bad_stamp["tombstones"] = {
            "files": {"memory/x.md": 3}, "prefixes": {}, "bullets": {}
        }
        bad_payloads.append(bad_stamp)
        bad_bullets = dict(valid)
        bad_bullets["tombstones"] = {
            "files": {}, "prefixes": {}, "bullets": {"memory/x.md": []}
        }
        bad_payloads.append(bad_bullets)
        bad_domains = dict(valid)
        bad_domains["domains"] = "memory"
        bad_payloads.append(bad_domains)
        bad_domain_item = dict(valid)
        bad_domain_item["domains"] = ["memory", 3]
        bad_payloads.append(bad_domain_item)

        for payload in bad_payloads:
            r = client.post("/api/v1/merge", json=payload, headers=auth)
            self.assertEqual(r.status_code, 400, payload)
        for payload in ([], "not-an-object", 3):
            r = client.post("/api/v1/merge", json=payload, headers=auth)
            self.assertEqual(r.status_code, 400, payload)

    def test_testclient_dependency_declared_and_ci_installs_test_extra(self):
        """Starlette 1.x TestClient imports httpx2. Packaging and CI must
        install it explicitly so auth tests execute instead of erroring."""
        repo = Path(__file__).resolve().parents[1]
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        server_match = re.search(r"(?ms)^server\s*=\s*\[(.*?)^\]", pyproject)
        test_match = re.search(r"(?ms)^test\s*=\s*\[(.*?)^\]", pyproject)
        self.assertIsNotNone(server_match)
        self.assertIsNotNone(test_match)
        self.assertIn("httpx2", server_match.group(1))
        self.assertIn("httpx2", test_match.group(1))
        ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn(".[server,test]", ci)

    def test_merge_rejects_traversal_wire_keys(self):
        app = self._app(secret_token="tok")
        client = _test_client(app)
        auth = {"Authorization": "Bearer tok"}
        payload = {
            "bundle": {"../pwned.md": "x", "memory/ok.md": "ok"},
            "stamps": {},
            "tombstones": {"files": {}, "prefixes": {}, "bullets": {}},
        }
        r = client.post("/api/v1/merge", json=payload, headers=auth)
        self.assertEqual(r.status_code, 200)
        ws_root = self.root / "workspaces" / "default"
        self.assertTrue((ws_root / "memory" / "ok.md").is_file())
        self.assertFalse((self.root / "workspaces" / "pwned.md").exists())
        self.assertFalse((self.root / "pwned.md").exists())


if __name__ == "__main__":
    unittest.main()
