"""Configuration management for agents-sync."""
from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

DEFAULT_DOMAINS = ["memory", "skills", "docs", "identity", "traces", "known_dids"]


def get_agents_dir() -> Path:
    """Return the root ~/.agents directory (or AGENTS_DIR env override)."""
    env_dir = os.environ.get("AGENTS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / ".agents").resolve()


def get_config_path() -> Path:
    """Return path to sync_config.json."""
    env_cfg = os.environ.get("AGENTS_SYNC_CONFIG")
    if env_cfg:
        return Path(env_cfg).expanduser().resolve()
    return get_agents_dir() / "sync_config.json"


@dataclass
class SyncConfig:
    url: str = ""
    token: str = ""
    node_id: str = field(default_factory=lambda: f"{platform.node().lower()}-{uuid.uuid4().hex[:6]}")
    enabled_domains: List[str] = field(default_factory=lambda: list(DEFAULT_DOMAINS))
    host_base_root: str = "~/Coding"

    def is_connected(self) -> bool:
        return bool(self.url.strip() and self.token.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "token": self.token,
            "node_id": self.node_id,
            "enabled_domains": self.enabled_domains,
            "host_base_root": self.host_base_root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SyncConfig":
        return cls(
            url=data.get("url", ""),
            token=data.get("token", ""),
            node_id=data.get("node_id") or f"{platform.node().lower()}-{uuid.uuid4().hex[:6]}",
            enabled_domains=list(data.get("enabled_domains") or DEFAULT_DOMAINS),
            host_base_root=data.get("host_base_root", "~/Coding"),
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
