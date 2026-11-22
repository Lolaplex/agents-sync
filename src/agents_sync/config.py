"""Configuration management for agents-sync."""
from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from agents_workspace import (
    ENV_SECRETS_ORDER,
    Host,
    Workspace,
    delete_secret,
    resolve_secret,
    secrets_ws_file,
    write_secret,
)

DEFAULT_DOMAINS = [
    "memory",
    "rules",
    "skills",
    "traces",
    "personas",
    "schedules",
    "config",
    "members",
]


def _current_workspace() -> Optional[Workspace]:
    try:
        return Workspace.from_env()
    except ValueError:
        return None


def _read_ws_vault_token(ws: Workspace) -> str:
    path = secrets_ws_file(ws)
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    val = data.get("AGENTS_SYNC_TOKEN")
    return val.strip() if isinstance(val, str) and val.strip() else ""


def _resolve_sync_token(legacy_token: str = "") -> str:
    ws = _current_workspace()
    clean_legacy = (legacy_token or "").strip()
    order = os.environ.get(ENV_SECRETS_ORDER, "").strip().lower()
    if order == "env-first":
        return resolve_secret("AGENTS_SYNC_TOKEN", ws=ws).strip() or clean_legacy
    if clean_legacy:
        if ws is not None:
            ws_tok = _read_ws_vault_token(ws)
            if ws_tok:
                return ws_tok
        return clean_legacy
    return resolve_secret("AGENTS_SYNC_TOKEN", ws=ws).strip()


def get_agents_dir() -> Path:
    """Return the active workspace root via the resolver."""
    return Workspace.from_env().dir


def get_config_path() -> Path:
    """Return path to sync_config.json (workspace level)."""
    env_cfg = os.environ.get("AGENTS_SYNC_CONFIG_FILE")
    if env_cfg:
        return Path(env_cfg).expanduser().resolve()
    legacy = os.environ.get("AGENTS_SYNC_CONFIG")
    if legacy:
        import warnings
        warnings.warn(
            "AGENTS_SYNC_CONFIG is deprecated; use AGENTS_SYNC_CONFIG_FILE",
            DeprecationWarning, stacklevel=2,
        )
        return Path(legacy).expanduser().resolve()
    return get_agents_dir() / "sync_config.json"


def get_host_config() -> dict[str, Any]:
    """Host-level config from <host>/agents.json."""
    path = Host.from_env().agents_json
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_host_config(data: dict[str, Any]) -> Path:
    path = Host.from_env().agents_json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass
class SyncConfig:
    url: str = ""
    token: str = ""
    node_id: str = field(default_factory=lambda: f"{platform.node().lower()}-{uuid.uuid4().hex[:6]}")
    enabled_domains: List[str] = field(default_factory=lambda: list(DEFAULT_DOMAINS))
    host_base_root: str = ""

    def is_connected(self) -> bool:
        return bool(self.url.strip() and self.token.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "enabled_domains": self.enabled_domains,
        }

    def require_host_base_root(self) -> str:
        if not self.host_base_root:
            raise RuntimeError(
                "host_base_root missing: set it in <host>/agents.json"
            )
        return self.host_base_root

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncConfig":
        host = get_host_config()
        node_id = str(host.get("node_id") or "") or f"{platform.node().lower()}-{uuid.uuid4().hex[:6]}"
        legacy_token = str(data.get("token") or "").strip()
        return cls(
            url=data.get("url", ""),
            token=_resolve_sync_token(legacy_token),
            node_id=node_id,
            enabled_domains=list(data.get("enabled_domains") or DEFAULT_DOMAINS),
            host_base_root=str(host.get("host_base_root") or ""),
        )


def load_config() -> SyncConfig:
    """Load config from disk or return default unconnected config."""
    path = get_config_path()
    if not path.is_file():
        # Fallback check: check if legacy remote_config.json exists in memory
        mem_cfg = get_agents_dir() / "memory" / "remote_config.json"
        if mem_cfg.is_file():
            try:
                data = json.loads(mem_cfg.read_text(encoding="utf-8"))
                return SyncConfig.from_dict(
                    {
                        "url": data.get("remote_url", ""),
                        "token": data.get("token", ""),
                    }
                )
            except Exception:
                pass
        return SyncConfig.from_dict({})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SyncConfig.from_dict(data)
    except Exception:
        return SyncConfig.from_dict({})


def save_config(config: SyncConfig) -> Path:
    """Save non-secret config to sync_config.json and token to Host Secret Vault."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    host = get_host_config()
    if not host.get("node_id") and config.node_id:
        host["node_id"] = config.node_id
        save_host_config(host)
    clean_token = (config.token or "").strip()
    if clean_token:
        ws = _current_workspace()
        if ws is not None:
            write_secret("AGENTS_SYNC_TOKEN", clean_token, ws=ws, scope="workspace")
        else:
            write_secret("AGENTS_SYNC_TOKEN", clean_token, scope="host")
    return path


def clear_config() -> bool:
    """Remove sync_config.json and delete the workspace sync token from the vault."""
    removed = False
    path = get_config_path()
    if path.is_file():
        path.unlink()
        removed = True
    ws = _current_workspace()
    if ws is not None:
        removed = delete_secret("AGENTS_SYNC_TOKEN", ws=ws, scope="workspace") or removed
    else:
        removed = delete_secret("AGENTS_SYNC_TOKEN", scope="host") or removed
    return removed
