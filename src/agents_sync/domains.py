"""Domain registries and path isolation rules."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Set

# Strictly forbidden file patterns that must NEVER be synced over the wire
FORBIDDEN_EXTENSIONS: Set[str] = {
    ".ed25519",  # Ed25519 private keys
    ".sqlite",
    ".db",
    ".lock",
    ".tmp",
    ".pyc",
}

FORBIDDEN_NAMES: Set[str] = {
    "sync_config.json",
    "remote_config.json",
    "board_attach.json",
    "host_paths.json",
    ".sync.lock",
}

FORBIDDEN_PARTS: Set[str] = {
    ".index",
    ".git",
    "__pycache__",
    "keys",  # Whole ~/.agents/keys directory is blocked except known-dids.jsonl
}


class DomainSpec:
    def __init__(self, name: str, relative_path: str, is_file: bool = False):
        self.name = name
        self.relative_path = relative_path
        self.is_file = is_file


# Registered syncable domains under ~/.agents
DOMAINS: Dict[str, DomainSpec] = {
    "memory": DomainSpec("memory", "memory", is_file=False),
    "rules": DomainSpec("rules", "rules", is_file=False),
    "skills": DomainSpec("skills", "skills", is_file=False),
    "docs": DomainSpec("docs", "docs", is_file=False),
    "identity": DomainSpec("identity", "identity.json", is_file=True),
    "traces": DomainSpec("traces", "traces", is_file=False),
    "known_dids": DomainSpec("known_dids", "known-dids.jsonl", is_file=True),
}


def is_path_safe(rel_path: str) -> bool:
    """Return False if path violates security, touches private keys, or is temporary."""
    path = Path(rel_path)
    
    # Special exemption: known-dids.jsonl in keys or root is safe (it only contains public DIDs)
    if path.name == "known-dids.jsonl":
        return True

    # Reject if any part is forbidden
    for part in path.parts:
        if part in FORBIDDEN_PARTS:
            return False
        if part.startswith(".") and part != ".tombstones.json" and part != ".sync-baseline.json":
            return False

    if path.name in FORBIDDEN_NAMES:
        return False

    if path.suffix in FORBIDDEN_EXTENSIONS:
        return False

    return True


def get_domain_root(agents_dir: Path, domain_name: str) -> Optional[Path]:
    """Get the local filesystem path for a domain under ~/.agents."""
    spec = DOMAINS.get(domain_name)
    if not spec:
        return None
    return agents_dir / spec.relative_path
