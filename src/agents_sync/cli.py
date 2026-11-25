"""Command-line interface for agents-sync."""
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import List, Optional

from .bundle import apply_bundle
from .client import check_health, fetch_snapshot, push_only, sync_all
from .config import (
    DEFAULT_DOMAINS,
    SyncConfig,
    _current_workspace,
    clear_config,
    get_agents_dir,
    load_config,
    save_config,
)
from .opdist import PEERS_FILE, PeerBook
from .tombstones import load_tombstones, merge_tombstones, save_tombstones

_PLANE1_DEPRECATION = (
    "agents-sync Plane 1 (file-bundle sync) is deprecated; migrate to "
    "hub-channel op-sync (klanker serve wssync / agents_sync.opdist)."
)

_PLANE1_COMMANDS = ("sync", "push", "pull", "serve")

# Ownership-field validation mirrors agents-workspace's strict read contract.
# The check is repeated here because Plane 1 must stay fail-closed even when
# installed alongside an older agents-workspace release whose tolerant reader
# silently falls back to a sync-enabled default on corrupt workspace.json.
_PEER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_EXEC_KEYS = frozenset({"owner", "failover", "lease_sec"})
_SYNC_POLICIES = ("local", "synced", "shared")


def _malformed_sync_hosts(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping) \
            or not isinstance(value, (list, tuple)):
        return True
    return any(
        not isinstance(item, str) or not _PEER_ID_RE.match(item.strip())
        for item in value
    )


def _malformed_default_host(value) -> bool:
    if value is None or value == "":
        return False
    return not isinstance(value, str) or not _HOST_ID_RE.match(value.strip())


def _malformed_repos(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)) or isinstance(value, Mapping) \
            or not isinstance(value, (list, tuple)):
        return True
    seen = set()
    for repo in value:
        if not isinstance(repo, Mapping) or "path" in repo:
            return True
        slug = repo.get("slug")
        clean_slug = slug.strip() if isinstance(slug, str) else ""
        if not clean_slug or ".." in clean_slug \
                or not _REPO_SLUG_RE.match(clean_slug) or clean_slug in seen:
            return True
        seen.add(clean_slug)
        mode = repo.get("checkout_mode") or "shared"
        if not isinstance(mode, str) or mode not in ("shared", "worktree"):
            return True
    return False


def _malformed_execution(value) -> bool:
    if value is None:
        return False
    if not isinstance(value, Mapping):
        return True
    if any(str(key) not in _EXEC_KEYS for key in value):
        return True
    if "owner" in value:
        owner = value["owner"]
        if owner is not None and not (
            isinstance(owner, str)
            and (not owner.strip() or _PEER_ID_RE.match(owner.strip()))
        ):
            return True
    if "failover" in value and value["failover"] not in ("standby", "none"):
        return True
    if "lease_sec" in value:
        lease = value["lease_sec"]
        if isinstance(lease, bool) or not isinstance(lease, int) \
                or not 15 <= lease <= 3600:
            return True
    return False


def _workspace_policy_state(ws) -> tuple[str, bool]:
    """Read and validate the authoritative policy fields from workspace.json.

    The object model alone is not a safe boundary across suite versions: older
    agents-workspace releases default a corrupt/missing policy document to
    ``synced``. Returning ``(state, malformed)`` keeps every ambiguous shape
    visibly fail-closed in the CLI refusal message.
    """
    try:
        ws_dir = Path(getattr(ws, "dir", None))
        raw = (ws_dir / "workspace.json").read_text(encoding="utf-8")
        meta = json.loads(raw)
    except (OSError, TypeError, ValueError):
        return "unreadable", True

    if not isinstance(meta, dict):
        return "unreadable", True

    malformed = False
    policy_value = meta.get("sync_policy")
    if policy_value is None:
        policy = "missing"
        malformed = True
    elif not isinstance(policy_value, str) \
            or policy_value not in _SYNC_POLICIES:
        policy = str(policy_value) if isinstance(policy_value, str) else "non-string"
        malformed = True
    else:
        policy = policy_value

    if _malformed_sync_hosts(meta.get("sync_hosts")) \
            or _malformed_default_host(meta.get("default_host")) \
            or _malformed_repos(meta.get("repos")) \
            or _malformed_execution(meta.get("execution")):
        malformed = True

    hosts = meta.get("sync_hosts")
    if policy == "local" and isinstance(hosts, list) and hosts:
        malformed = True

    return policy or "empty", malformed or bool(getattr(ws, "policy_error", False))


def _warn_plane1_deprecated() -> None:
    warnings.warn(_PLANE1_DEPRECATION, DeprecationWarning, stacklevel=2)
    print(f"Deprecated: {_PLANE1_DEPRECATION}", file=sys.stderr)


def _policy_refusal() -> Optional[tuple[str, str]]:
    """Return (workspace id, policy state) when Plane 1 sync is forbidden.

    Any resolution/read error is itself a refusal: workspace policy is a
    security boundary, so an unreadable boundary never defaults to allow.
    """
    try:
        ws = _current_workspace()
        if ws is None:
            return ("?", "missing")
        policy, malformed = _workspace_policy_state(ws)
        if policy == "synced" and not malformed:
            return None
        ident = str(getattr(ws, "id", "") or "?")
        if malformed:
            policy = f"{policy}+policy-error"
        return (ident, policy or "empty")
    except Exception:
        return ("?", "unreadable")


def _require_syncable() -> Optional[int]:
    """Enforce the workspace sync_policy before any Plane 1 transfer."""
    refusal = _policy_refusal()
    if refusal is None:
        return None
    refused, policy = refusal
    print(
        f"Refusing sync: workspace '{refused}' has sync_policy='{policy}' "
        "(exactly 'synced' required; fail-closed). Set sync_policy='synced' "
        "in workspace.json to opt in, or use hub-channel op-sync.",
        file=sys.stderr,
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents-sync",
        description="Unified multi-device synchronizer for workspace domains (memory, skills, rules, traces).",
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

    # peers (Plane 2 op-sync cursor inspection)
    p_peers = sub.add_parser("peers", help="Inspect or reset op-sync peer cursors (sync_peers.json)")
    p_peers.add_argument("--reset", metavar="PEER", help="Reset one peer's cursors to 0")
    p_peers.add_argument("--reset-all", action="store_true", help="Reset every peer's cursors to 0")

    # serve
    p_srv = sub.add_parser("serve", help="Run cloud server on VPS")
    p_srv.add_argument("port", nargs="?", type=int, default=8765, help="Port (default: 8765)")
    p_srv.add_argument("--host", default="0.0.0.0", help="Host address (default: 0.0.0.0)")
    p_srv.add_argument(
        "--allow-insecure-dev",
        action="store_true",
        help="Start without AGENTS_SYNC_TOKEN (requests still 401; dev only)",
    )

    return parser


def _peers_path() -> Path:
    return get_agents_dir() / PEERS_FILE


def _read_peers_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _cmd_peers(args) -> int:
    try:
        path = _peers_path()
    except Exception as e:
        print(f"peers: cannot resolve workspace: {e}", file=sys.stderr)
        return 1

    if args.reset or args.reset_all:
        book = PeerBook(path)
        if args.reset_all:
            targets = sorted(_read_peers_file(path).keys())
            if not targets:
                print(f"No peers recorded in {path}.")
                return 0
        else:
            targets = [args.reset]
        for peer in targets:
            book.resync(peer)
            print(f"Reset peer '{peer}' cursors to 0.")
        return 0

    peers = _read_peers_file(path) if path.is_file() else {}
    if not peers:
        print(f"No peers recorded (expected at {path}).")
        return 0
    print(f"Peer cursors ({path}):")
    for peer, rec in sorted(peers.items()):
        rec = rec if isinstance(rec, dict) else {}
        print(f"  {peer}  cursor={rec.get('cursor', 0)}  acked={rec.get('acked', 0)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.command:
        parser.print_help()
        return 0

    if args.command in _PLANE1_COMMANDS:
        _warn_plane1_deprecated()

    # Policy is a security boundary and must be evaluated before config
    # loading touches the workspace resolver; a missing/corrupt workspace
    # refuses here instead of crashing or defaulting to allow.
    if args.command in ("sync", "push", "pull"):
        refused = _require_syncable()
        if refused is not None:
            return refused

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
            node_id=cfg.node_id,
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
        print(f"Workspace:        {get_agents_dir()}")
        print(f"Connected:        {'Yes' if cfg.is_connected() else 'No'}")
        if cfg.is_connected():
            print(f"Remote URL:       {cfg.url}")
            print(f"Enabled Domains:  {', '.join(cfg.enabled_domains)}")
            healthy = check_health(cfg.url, cfg.token)
            print(f"Remote Health:    {'Healthy' if healthy else 'Unreachable / Unauthorized'}")
        return 0

    if args.command in ("sync", "push", "pull"):
        if not cfg.is_connected():
            print("Error: Not connected. Run: agents-sync connect <url> --token <token>")
            return 1

    if args.command == "sync":
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

    if args.command == "push":
        try:
            print(f"Pushing node [{cfg.node_id}] to {cfg.url}...")
            res = push_only(cfg)
            print(f"Pushed {res['pushed_files']} files to remote (push-only).")
            return 0
        except Exception as e:
            print(f"Push error: {e}")
            return 1

    if args.command == "pull":
        try:
            print(f"Pulling from {cfg.url}...")
            root = get_agents_dir()
            bundle, stamps, tombs = fetch_snapshot(cfg.url, cfg.token, cfg.enabled_domains)
            written, deleted = apply_bundle(
                root, bundle, tombs, stamps, enabled_domains=cfg.enabled_domains
            )
            # Persist merged tombstones or this node forgets remote deletions.
            merged = merge_tombstones(load_tombstones(root), tombs)
            save_tombstones(root, merged)
            print(f"Applied snapshot: {len(written)} updated, {len(deleted)} deleted.")
            return 0
        except Exception as e:
            print(f"Pull error: {e}")
            return 1

    if args.command == "peers":
        return _cmd_peers(args)

    if args.command == "serve":
        from .server import run_server

        print(f"Starting agents-sync server on {args.host}:{args.port}...")
        run_server(port=args.port, host=args.host, allow_insecure_dev=args.allow_insecure_dev)
        return 0

    return 0
