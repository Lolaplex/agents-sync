import contextlib
import io
import json
import os
import tempfile
import unittest
import warnings
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
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                ret = main(["serve", "9", "--host", "127.0.0.1"])
        self.assertEqual(ret, 0)
        run.assert_called_once_with(
            port=9, host="127.0.0.1", allow_insecure_dev=False
        )

    def test_serve_allow_insecure_dev_flag(self):
        with patch("agents_sync.server.run_server") as run:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                ret = main(["serve", "9", "--allow-insecure-dev"])
        self.assertEqual(ret, 0)
        run.assert_called_once_with(
            port=9, host="0.0.0.0", allow_insecure_dev=True
        )

    def test_server_token_comes_from_env(self):
        from agents_sync.server import resolve_server_token

        with patch.dict(os.environ, {"AGENTS_SYNC_TOKEN": "env-token"}, clear=False):
            self.assertEqual(resolve_server_token(None), "env-token")
            self.assertEqual(resolve_server_token(""), "env-token")
        with patch.dict(os.environ, {"AGENTS_SYNC_TOKEN": ""}, clear=False):
            self.assertEqual(resolve_server_token("kept"), "kept")

    def _set_sync_policy(self, ws, policy: str) -> None:
        """Write sync_policy into workspace.json — version-agnostic across
        agents-workspace releases (older versions have no create() kwarg)."""
        meta_file = ws.dir / "workspace.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["sync_policy"] = policy
        meta_file.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    def test_sync_refused_on_local_sync_policy(self):
        host = Host.open(self.tmp.name)
        ws = host.create("localws")
        self._set_sync_policy(ws, "local")
        for command in ("sync", "push", "pull"):
            with patch.dict(os.environ, {"AGENTS_WORKSPACE": "localws"}):
                err = io.StringIO()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    with contextlib.redirect_stderr(err):
                        ret = main([command])
                self.assertEqual(ret, 2, command)
                self.assertIn("sync_policy", err.getvalue())
                self.assertIn("local", err.getvalue())

    def test_sync_refused_on_missing_shared_corrupt_or_ambiguous_policy(self):
        """The policy gate is fail-closed: missing workspaces, reserved
        'shared', corrupt metadata, and malformed ownership fields all
        refuse with exit 2 rather than defaulting to allow or crashing."""
        host = Host.open(self.tmp.name)

        def run_refusal(ws_id):
            with patch.dict(os.environ, {"AGENTS_WORKSPACE": ws_id}):
                err = io.StringIO()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    with contextlib.redirect_stderr(err):
                        return main(["sync"]), err.getvalue()

        ret, err = run_refusal("missing-ws")
        self.assertEqual(ret, 2)
        self.assertIn("missing", err)

        no_policy = host.create("nopolicyws")
        meta_file = no_policy.dir / "workspace.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta.pop("sync_policy", None)
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
        ret, err = run_refusal("nopolicyws")
        self.assertEqual(ret, 2)
        self.assertIn("missing", err)

        bad_ownership = host.create("badownershipws")
        meta_file = bad_ownership.dir / "workspace.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["default_host"] = 42
        meta["sync_policy"] = "synced"
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
        ret, err = run_refusal("badownershipws")
        self.assertEqual(ret, 2)
        self.assertIn("policy-error", err)

        shared = host.create("sharedws")
        self._set_sync_policy(shared, "shared")
        ret, err = run_refusal("sharedws")
        self.assertEqual(ret, 2)
        self.assertIn("shared", err)

        corrupt = host.create("corruptws")
        (corrupt.dir / "workspace.json").write_text("{not json", encoding="utf-8")
        ret, err = run_refusal("corruptws")
        self.assertEqual(ret, 2)
        self.assertIn("sync_policy", err)

        ambiguous = host.create("ambiguousws")
        meta_file = ambiguous.dir / "workspace.json"
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["sync_policy"] = "synced"
        meta["sync_hosts"] = "not-a-list"
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
        ret, err = run_refusal("ambiguousws")
        self.assertEqual(ret, 2)
        self.assertIn("policy-error", err)

    def test_sync_warns_deprecated_and_runs_on_synced_workspace(self):
        host = Host.open(self.tmp.name)
        ws = host.create("syncws")
        self._set_sync_policy(ws, "synced")
        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "syncws"}):
            with patch("agents_sync.cli.sync_all", return_value={
                "pushed_files": 0,
                "received_files": 0,
                "locally_written": [],
                "locally_deleted": [],
            }) as sync_mock:
                # Not connected -> must still emit DeprecationWarning first.
                with self.assertWarns(DeprecationWarning):
                    ret = main(["sync"])
                self.assertEqual(ret, 1)
                sync_mock.assert_not_called()

    def test_push_uses_push_only_client(self):
        host = Host.open(self.tmp.name)
        ws = host.create("syncws")
        self._set_sync_policy(ws, "synced")
        cfg = SyncConfig(url="https://x.example", token="t")
        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "syncws"}):
            with patch("agents_sync.cli.load_config", return_value=cfg):
                with patch(
                    "agents_sync.cli.push_only",
                    return_value={"pushed_files": 3},
                ) as push_mock, patch("agents_sync.cli.sync_all") as sync_mock:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        ret = main(["push"])
        self.assertEqual(ret, 0)
        push_mock.assert_called_once_with(cfg)
        sync_mock.assert_not_called()

    def test_pull_saves_merged_tombstones(self):
        host = Host.open(self.tmp.name)
        ws = host.create("syncws")
        self._set_sync_policy(ws, "synced")
        cfg = SyncConfig(url="https://x.example", token="t")
        remote_tombs = {
            "version": 1,
            "files": {"memory/dead.md": "2999-01-01T00:00:00Z"},
            "prefixes": {},
            "bullets": {},
        }
        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "syncws"}):
            with patch("agents_sync.cli.load_config", return_value=cfg):
                with patch(
                    "agents_sync.cli.fetch_snapshot",
                    return_value=({}, {}, remote_tombs),
                ):
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        ret = main(["pull"])
        self.assertEqual(ret, 0)
        from agents_sync.tombstones import load_tombstones

        saved = load_tombstones(ws.dir)
        self.assertIn("memory/dead.md", saved["files"])

    def test_peers_lists_and_resets_sync_peers(self):
        host = Host.open(self.tmp.name)
        ws = host.create("syncws")
        (ws.dir / "sync_peers.json").write_text(
            json.dumps({"hub-1": {"cursor": 5, "acked": 4}}),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "syncws"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ret = main(["peers"])
            self.assertEqual(ret, 0)
            self.assertIn("hub-1", out.getvalue())
            self.assertIn("5", out.getvalue())

            ret = main(["peers", "--reset", "hub-1"])
            self.assertEqual(ret, 0)
            data = json.loads((ws.dir / "sync_peers.json").read_text())
            self.assertEqual(data["hub-1"]["acked"], 0)
            self.assertEqual(data["hub-1"]["cursor"], 0)

    def test_peers_empty_when_no_file(self):
        host = Host.open(self.tmp.name)
        host.create("syncws")
        with patch.dict(os.environ, {"AGENTS_WORKSPACE": "syncws"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ret = main(["peers"])
            self.assertEqual(ret, 0)
            self.assertIn("sync_peers.json", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class TestHostConfig(unittest.TestCase):
    def test_domains_are_workspace_only(self):
        from agents_sync.domains import DOMAINS
        from agents_sync.config import DEFAULT_DOMAINS
        self.assertEqual(
            set(DOMAINS),
            {
                "memory",
                "rules",
                "skills",
                "traces",
                "personas",
                "schedules",
                "config",
                "members",
            },
        )
        self.assertTrue(DOMAINS["members"].is_file)
        self.assertEqual(DOMAINS["members"].relative_path, "members.json")
        self.assertTrue(DOMAINS["config"].is_file)
        self.assertEqual(DOMAINS["config"].relative_path, "config.json")
        self.assertEqual(
            DEFAULT_DOMAINS,
            [
                "memory",
                "rules",
                "skills",
                "traces",
                "personas",
                "schedules",
                "config",
                "members",
            ],
        )

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
                self.assertEqual(
                    loaded.enabled_domains,
                    [
                        "memory",
                        "rules",
                        "skills",
                        "traces",
                        "personas",
                        "schedules",
                        "config",
                        "members",
                    ],
                )

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
