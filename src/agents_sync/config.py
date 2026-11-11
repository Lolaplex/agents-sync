"""Configuration management for agents-sync."""
from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from agents_workspace import Workspace

DEFAULT_DOMAINS = ["memory", "skills", "traces"]


def get_agents_dir() -> Path:
    """Return the active workspace root via the resolver."""
    return Workspace.from_env().root


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
    path = Workspace.from_env().host.agents_json
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_host_config(data: dict[str, Any]) -> Path:
    path = Workspace.from_env().host.agents_json
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
            "token": self.token,
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
        node_id = host.get("node_id")
        if not node_id:
            node_id = f"{platform.node().lower()}-{uuid.uuid4().hex[:6]}"
            host["node_id"] = node_id
            try:
                save_host_config(host)
            except OSError:
                pass
        return cls(
            url=data.get("url", ""),
            token=data.get("token", ""),
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
                return SyncConfig(
                    url=data.get("remote_url", ""),
                    token=data.get("token", ""),
                )
            except Exception:
                pass
        return SyncConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SyncConfig.from_dict(data)
    except Exception:
        return SyncConfig()


def save_config(config: SyncConfig) -> Path:
    """Save config to sync_config.json."""
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def clear_config() -> bool:
    """Remove sync_config.json."""
    path = get_config_path()
    if path.is_file():
        path.unlink()
        return True
    return False
