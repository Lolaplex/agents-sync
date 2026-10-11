"""Standard-library HTTP client for agents-sync."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .bundle import apply_bundle, collect_bundle
from .config import SyncConfig, get_agents_dir, load_config
from .protocol import EP_HEALTH, EP_MERGE, EP_SNAPSHOT, EP_STATUS, make_merge_payload
from .tombstones import load_tombstones, merge_tombstones, save_tombstones


class SyncClientError(Exception):
    """Raised when remote communication fails."""


def _make_request(
    url: str,
    endpoint: str,
    token: str,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    full_url = urllib.parse.urljoin(url.rstrip("/") + "/", endpoint.lstrip("/"))
    headers = {
        "User-Agent": "agents-sync/0.1.0",
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body: Optional[bytes] = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(full_url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            if not content.strip():
                return {}
            return json.loads(content)
    except urllib.error.HTTPError as e:
        error_msg = f"HTTP {e.code}: {e.reason}"
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            if "detail" in err_json:
                error_msg += f" - {err_json['detail']}"
        except Exception:
            pass
        raise SyncClientError(error_msg) from e
    except urllib.error.URLError as e:
        raise SyncClientError(f"Network error: {e.reason}") from e


def check_health(url: str, token: str) -> bool:
    try:
        res = _make_request(url, EP_HEALTH, token, timeout=10)
        return res.get("status") in ("ok", "healthy")
    except Exception:
        return False


def fetch_snapshot(
    url: str,
    token: str,
    domains: Optional[list[str]] = None,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, Any]]:
    """Download full cloud vault snapshot."""
    ep = EP_SNAPSHOT
    if domains:
        ep += f"?domains={','.join(domains)}"
    res = _make_request(url, ep, token)
    bundle = res.get("bundle", {})
    stamps = res.get("stamps", {})
    tombstones = res.get("tombstones", {})
    return bundle, stamps, tombstones


def push_merge(
    url: str,
    token: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Push local changes to remote vault and receive merged updates."""
    return _make_request(url, EP_MERGE, token, method="POST", data=payload)


def sync_all(
    config: Optional[SyncConfig] = None,
    agents_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Perform a full bi-directional sync (collect -> push/merge -> apply)."""
    cfg = config or load_config()
    if not cfg.is_connected():
        raise SyncClientError("Not connected to cloud vault. Run: agents-sync connect <url> --token <token>")

    root = agents_dir or get_agents_dir()
    local_stamps: Dict[str, str] = {}
    local_bundle = collect_bundle(root, cfg.enabled_domains, node_id=cfg.node_id, stamps=local_stamps)
    local_tombstones = load_tombstones(root)

    payload = make_merge_payload(
        node_id=cfg.node_id,
        bundle=local_bundle,
        stamps=local_stamps,
        tombstones=local_tombstones,
    )

    response = push_merge(cfg.url, cfg.token, payload)

    remote_bundle = response.get("bundle", {})
    remote_stamps = response.get("stamps", {})
    remote_tombs = response.get("tombstones", {})

    written, deleted = apply_bundle(
        root,
        remote_bundle,
        remote_tombstones=remote_tombs,
        remote_stamps=remote_stamps,
    )

    # Save merged tombstones locally
    merged_tombs = merge_tombstones(local_tombstones, remote_tombs)
    save_tombstones(root, merged_tombs)

    return {
        "pushed_files": len(local_bundle),
        "received_files": len(remote_bundle),
        "locally_written": written,
        "locally_deleted": deleted,
    }
