import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents_sync.cli import main, build_parser
from agents_sync.config import load_config, save_config, SyncConfig


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_file = Path(self.tmp.name) / "sync_config.json"

    def tearDown(self):
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
                cfg = load_config()
                self.assertEqual(cfg.url, "https://vps.example.com:8765")
                self.assertEqual(cfg.token, "secret123")

                # Disconnect
                ret_disc = main(["disconnect"])
                self.assertEqual(ret_disc, 0)
                self.assertFalse(self.cfg_file.exists())


if __name__ == "__main__":
    unittest.main()
