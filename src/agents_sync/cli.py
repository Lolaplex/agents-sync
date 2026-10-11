"""Command-line interface for agents-sync."""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .bundle import apply_bundle, collect_bundle
from .client import check_health, fetch_snapshot, sync_all
from .config import (
    DEFAULT_DOMAINS,
    SyncConfig,
    clear_config,
    get_agents_dir,
    load_config,
    save_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents-sync",
        description="Unified multi-device cloud synchronizer for ~/.agents (memory, skills, docs, traces, identity).",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    # connect
    p_conn = sub.add_parser("connect", help="Connect this node to a cloud vault")
    p_conn.add_argument("url", help="Remote vault URL (e.g. https://vps.lolax.dev:8765)")
    p_conn.add_argument("--token", required=True, help="Bearer authorization token")
    p_conn.add_argument(
        "--domains",
        help=f"Comma-separated list of domains to sync (default: {','.join(DEFAULT_DOMAINS)})",
    )

    # disconnect
    sub.add_parser("disconnect", help="Disconnect from cloud vault and remove credentials")

    # status
    sub.add_parser("status", help="Show current connection status and health")

    # sync
    sub.add_parser("sync", help="Run full bi-directional sync (push local + pull remote)")

    # push
    sub.add_parser("push", help="Push local state to remote vault")

    # pull
    sub.add_parser("pull", help="Pull remote vault and apply to local node")

    # serve
    p_srv = sub.add_parser("serve", help="Run cloud server on VPS")
    p_srv.add_argument("port", nargs="?", type=int, default=8765, help="Port (default: 8765)")
    p_srv.add_argument("--host", default="0.0.0.0", help="Host address (default: 0.0.0.0)")
    p_srv.add_argument("--token", help="Secret token required for clients")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.command:
        parser.print_help()
        return 0

    cfg = load_config()

    if args.command == "connect":
        domains = (
            [d.strip() for d in args.domains.split(",") if d.strip()]
            if args.domains
            else list(DEFAULT_DOMAINS)
        )
        new_cfg = SyncConfig(
            url=args.url.rstrip("/"),
            token=args.token,
            enabled_domains=domains,
        )
        print(f"Connecting to {new_cfg.url}...")
        if not check_health(new_cfg.url, new_cfg.token):
            print("Warning: Remote health check failed or returned unauthorized. Saving config anyway.")
        save_path = save_config(new_cfg)
        print(f"Saved connection config to {save_path}")
        return 0

    if args.command == "disconnect":
        if clear_config():
            print("Disconnected. Removed sync_config.json.")
        else:
            print("Already disconnected.")
        return 0

    if args.command == "status":
        print(f"Node ID:          {cfg.node_id}")
        print(f"Agents Home:      {get_agents_dir()}")
        print(f"Connected:        {'Yes' if cfg.is_connected() else 'No'}")
        if cfg.is_connected():
            print(f"Remote URL:       {cfg.url}")
            print(f"Enabled Domains:  {', '.join(cfg.enabled_domains)}")
            healthy = check_health(cfg.url, cfg.token)
            print(f"Remote Health:    {'Healthy' if healthy else 'Unreachable / Unauthorized'}")
        return 0

    if args.command in ("sync", "push"):
        if not cfg.is_connected():
            print("Error: Not connected. Run: agents-sync connect <url> --token <token>")
            return 1
        try:
            print(f"Syncing node [{cfg.node_id}] with {cfg.url}...")
            res = sync_all(cfg)
            print(f"Pushed {res['pushed_files']} files to remote.")
            print(f"Received {res['received_files']} files from remote.")
            if res["locally_written"]:
                print(f"Locally updated {len(res['locally_written'])} files:")
                for f in res["locally_written"][:10]:
                    print(f"  + {f}")
                if len(res["locally_written"]) > 10:
                    print(f"  ... and {len(res['locally_written']) - 10} more.")
            if res["locally_deleted"]:
                print(f"Locally deleted {len(res['locally_deleted'])} files (tombstoned).")
            print("Sync complete.")
            return 0
        except Exception as e:
            print(f"Sync error: {e}")
            return 1

    if args.command == "pull":
        if not cfg.is_connected():
            print("Error: Not connected. Run: agents-sync connect <url> --token <token>")
            return 1
        try:
            print(f"Pulling from {cfg.url}...")
            bundle, stamps, tombs = fetch_snapshot(cfg.url, cfg.token, cfg.enabled_domains)
            written, deleted = apply_bundle(get_agents_dir(), bundle, tombs, stamps)
            print(f"Applied snapshot: {len(written)} updated, {len(deleted)} deleted.")
            return 0
        except Exception as e:
            print(f"Pull error: {e}")
            return 1

    if args.command == "serve":
        from .server import run_server

        print(f"Starting agents-sync server on {args.host}:{args.port}...")
        run_server(port=args.port, host=args.host, token=args.token)
        return 0

    return 0
