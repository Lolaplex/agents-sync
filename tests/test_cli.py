import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_workspace import Host, doctor, resolve_secret

from agents_sync.cli import main, build_parser
from agents_sync.config import load_config, save_config, SyncConfig


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_file = Path(self.tmp.name) / "sync_config.json"
        self._env = patch.dict(
            os.environ,
            {
                "AGENTS_HOST_DIR": self.tmp.name,
                "AGENTS_SYNC_TOKEN": "",
                "AGENTS_SECRETS_ORDER": "",
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop("AGENTS_WORKSPACE", None)
        os.environ.pop("AGENTS_SYNC_CONFIG_FILE", None)

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_parser_options(self):
        parser = build_parser()
        args = parser.parse_args(["status"])
        self.assertEqual(args.command, "status")

    def test_connect_and_disconnect(self):
        with patch("agents_sync.config.get_config_path", return_value=self.cfg_file):
            with patch("agents_sync.cli.check_health", return_value=True):
                ret = main(["connect", "https://vps.example.com:8765", "--token", "secret123"])
                self.assertEqual(ret, 0)
                raw = self.cfg_file.read_text(encoding="utf-8")
                self.assertNotIn("secret123", raw)
                self.assertNotIn("token", json.loads(raw))
                cfg = load_config()
                self.assertEqual(cfg.url, "https://vps.example.com:8765")
                self.assertEqual(cfg.token, "secret123")
                self.assertEqual(resolve_secret("AGENTS_SYNC_TOKEN"), "secret123")

                # Disconnect
                ret_disc = main(["disconnect"])
                self.assertEqual(ret_disc, 0)
                self.assertFalse(self.cfg_file.exists())
                self.assertEqual(load_config().token, "")

    def test_multi_workspace_tokens_isolated(self):
        host = Host.open(self.tmp.name)
        ws_a = host.create("alpha")
        ws_b = host.create("beta")

        with patch("agents_sync.cli.check_health", return_value=True):
            with patch.dict(os.environ, {"AGENTS_WORKSPACE": "alpha"}):
                self.assertEqual(
                    main(["connect", "https://a.example", "--token", "tok-alpha"]), 0
                )
            with patch.dict(os.environ, {"AGENTS_WORKSPACE": "beta"}):
                self.assertEqual(
                    main(["connect", "https://b.example", "--token", "tok-beta"]), 0
                )

        self.assertNotIn("token", json.loads(ws_a.sync_config.read_text(encoding="utf-8")))
        self.assertNotIn("token", json.loads(ws_b.sync_config.read_text(encoding="utf-8")))

        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "alpha"}):
            cfg_a = load_config()
            self.assertEqual(cfg_a.url, "https://a.example")
            self.assertEqual(cfg_a.token, "tok-alpha")

        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "beta"}):
            cfg_b = load_config()
            self.assertEqual(cfg_b.url, "https://b.example")
            self.assertEqual(cfg_b.token, "tok-beta")

        sync_issues = [
            i for i in doctor(host)["issues"]
            if "sync_config" in i["path"] or "token" in i["message"].lower()
        ]
        self.assertEqual(sync_issues, [])

        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "alpha"}):
            self.assertEqual(main(["disconnect"]), 0)
            self.assertEqual(load_config().token, "")

        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "beta"}):
            self.assertEqual(load_config().token, "tok-beta")

    def test_legacy_sync_config_token_fallback_and_strip_on_save(self):
        host = Host.open(self.tmp.name)
        ws = host.create("default")
        ws.sync_config.write_text(
            json.dumps({"url": "https://legacy.example", "token": "legacy-tok"}),
            encoding="utf-8",
        )
        loaded = load_config()
        self.assertEqual(loaded.token, "legacy-tok")

        with patch.dict(
            os.environ,
            {"AGENTS_SYNC_TOKEN": "env-tok", "AGENTS_SECRETS_ORDER": "env-first"},
        ):
            self.assertEqual(load_config().token, "env-tok")

        save_config(loaded)
        disk = json.loads(ws.sync_config.read_text(encoding="utf-8"))
        self.assertNotIn("token", disk)
        self.assertEqual(load_config().token, "legacy-tok")
        sync_issues = [
            i for i in doctor(host)["issues"]
            if "sync_config" in i["path"] or "token" in i["message"].lower()
        ]
        self.assertEqual(sync_issues, [])

    def test_serve_rejects_token_on_argv(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["serve", "8765", "--token", "secret123"])

    def test_serve_starts_without_token_flag(self):
        with patch("agents_sync.server.run_server") as run:
            ret = main(["serve", "9", "--host", "127.0.0.1"])
        self.assertEqual(ret, 0)
        run.assert_called_once_with(port=9, host="127.0.0.1")

    def test_server_token_comes_from_env(self):
        from agents_sync.server import resolve_server_token

        with patch.dict(os.environ, {"AGENTS_SYNC_TOKEN": "env-token"}, clear=False):
            self.assertEqual(resolve_server_token(None), "env-token")
            self.assertEqual(resolve_server_token(""), "env-token")
        with patch.dict(os.environ, {"AGENTS_SYNC_TOKEN": ""}, clear=False):
            self.assertEqual(resolve_server_token("kept"), "kept")


if __name__ == "__main__":
    unittest.main()


class TestHostConfig(unittest.TestCase):
    def test_domains_are_workspace_only(self):
        from agents_sync.domains import DOMAINS
        from agents_sync.config import DEFAULT_DOMAINS
        self.assertEqual(set(DOMAINS), {"memory", "rules", "skills", "traces", "members"})
        self.assertTrue(DOMAINS["members"].is_file)
        self.assertEqual(DOMAINS["members"].relative_path, "members.json")
        self.assertEqual(DEFAULT_DOMAINS, ["memory", "rules", "skills", "traces", "members"])

    def test_from_dict_is_pure_and_save_config_writes_node_id(self):
        from agents_workspace import Host
        from agents_sync.config import get_host_config, save_host_config

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_HOST_DIR": tmp}, clear=False):
                os.environ.pop("AGENTS_WORKSPACE", None)
                host = Host.from_env()
                host.create("default")
                self.assertFalse(host.agents_json.exists())

                cfg = SyncConfig.from_dict({"url": "https://sync.example"})
                self.assertFalse(host.agents_json.exists())
                loaded = load_config()
                self.assertFalse(host.agents_json.exists())

                save_config(cfg)
                self.assertTrue(host.agents_json.is_file())
                self.assertEqual(get_host_config()["node_id"], cfg.node_id)

                save_host_config({"node_id": "pinned-node"})
                cfg2 = SyncConfig.from_dict({})
                self.assertEqual(cfg2.node_id, "pinned-node")
                save_config(SyncConfig(url="https://sync.example", node_id="other"))
                self.assertEqual(get_host_config()["node_id"], "pinned-node")
                self.assertEqual(loaded.enabled_domains, ["memory", "rules", "skills", "traces", "members"])

    def test_agents_dir_env_ignored(self):
        import os, tempfile
        from unittest.mock import patch
        from agents_sync.config import get_agents_dir
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENTS_DIR": tmp, "AGENTS_HOST_DIR": tempfile.mkdtemp()}):
                self.assertNotEqual(str(get_agents_dir()), tmp)

    def test_missing_host_base_root_errors(self):
        from agents_sync.config import SyncConfig
        with self.assertRaises(RuntimeError) as ctx:
            SyncConfig().require_host_base_root()
        self.assertIn("agents.json", str(ctx.exception))

    def test_host_config_uses_host_not_workspace(self):
        from agents_workspace import Host
        from agents_sync.config import get_host_config, save_host_config

        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENTS_HOST_DIR": tmp}
            with patch.dict(os.environ, env, clear=False):
                os.environ.pop("AGENTS_WORKSPACE", None)
                Host.from_env().create("alpha")
                Host.from_env().create("beta")
                save_host_config({"node_id": "n-host"})
                self.assertEqual(get_host_config()["node_id"], "n-host")
