"""Domain registries and path isolation rules."""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, List, Optional, Set

# Strictly forbidden file patterns that must NEVER be synced over the wire
FORBIDDEN_EXTENSIONS: Set[str] = {
    ".ed25519",  # Ed25519 private keys
    ".key",
    ".pem",
    ".sqlite",
    ".db",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".db-wal",
    ".db-shm",
    ".db-journal",
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
    "secrets.json",
    ".env",
    "calendar.json",
    "vapid.json",
    "remotes.json",
    "push.json",
    # Plane 2 / workspace internals must never travel the file-bundle wire
    "sync_peers.json",
    "workspace.json",
    "hubs.json",
    "devices.json",
    "users.json",
    "agents.json",
}

FORBIDDEN_PARTS: Set[str] = {
    ".index",
    ".git",
    "__pycache__",
    "keys",  # Whole ~/.agents/keys directory is blocked except known-dids.jsonl
    "secrets",
    "run",  # Ephemeral <ws>/run state (delegates, procs, locks) must never sync
}

# Exact workspace-relative file paths that may legally sync.
ALLOWED_FILES: Set[str] = {
    "config.json",
    "members.json",
    # Public-DID ledger only; never the private keys next to it.
    "known-dids.jsonl",
    "keys/known-dids.jsonl",
}

# Top-level domain directories that may legally sync.
ALLOWED_PREFIXES: Set[str] = {
    "memory",
    "rules",
    "skills",
    "traces",
    "personas",
    "schedules",
}


class DomainSpec:
    def __init__(self, name: str, relative_path: str, is_file: bool = False):
        self.name = name
        self.relative_path = relative_path
        self.is_file = is_file


# Registered syncable workspace domains
DOMAINS: Dict[str, DomainSpec] = {
    "memory": DomainSpec("memory", "memory", is_file=False),
    "rules": DomainSpec("rules", "rules", is_file=False),
    "skills": DomainSpec("skills", "skills", is_file=False),
    "traces": DomainSpec("traces", "traces", is_file=False),
    "personas": DomainSpec("personas", "personas", is_file=False),
    "schedules": DomainSpec("schedules", "schedules", is_file=False),
    "config": DomainSpec("config", "config.json", is_file=True),
    "members": DomainSpec("members", "members.json", is_file=True),
}


def is_path_safe(rel_path: str) -> bool:
    """Return False if path violates security, touches private keys, or is temporary.

    Fail-closed: a wire key is only safe when it names a file below a
    registered sync domain (or is an explicitly allowed root file) and
    survives the traversal/name/extension deny checks. All comparisons
    are case-insensitive because the local filesystem may be.
    """
    raw = str(rel_path)
    if not raw or not raw.strip():
        return False

    # NUL and ':' are never legal portable wire-key characters. ':' is a
    # Windows drive/ADS spellings vector even when PureWindowsPath does
    # not classify the whole key as a drive path.
    if "\x00" in raw or ":" in raw:
        return False

    # Reject traversal and absolute paths BEFORE any file exemption:
    # leading slash/backslash, Windows drive letters/UNC, ".." segments.
    # A trailing separator denotes a directory, never a syncable file.
    if raw.startswith(("/", "\\")) or raw.endswith(("/", "\\")):
        return False
    win = PureWindowsPath(raw)
    if win.drive or win.is_absolute():
        return False
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not path.parts or not path.name or ".." in path.parts:
        return False

    # Exact-match exemptions (public DIDs + root-level sync files).
    if normalized in ALLOWED_FILES:
        return True

    # Reject if any part is forbidden, hidden, or uses Windows' flaky
    # trailing-dot/space filename spellings (foo. / foo ). Those spellings
    # otherwise bypass suffix and exact-name checks while resolving to the
    # stripped filename on Windows.
    for part in path.parts:
        lowered = part.lower()
        if lowered in FORBIDDEN_PARTS or lowered.startswith("."):
            return False
        if part.endswith((".", " ")):
            return False

    if path.name.lower() in FORBIDDEN_NAMES:
        return False

    if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
        return False

    # Enforce the registered domain allowlist: the first path segment
    # must be a known sync domain directory and the key must name a file
    # below it. A bare domain directory is never a syncable file key.
    if path.parts[0].lower() not in ALLOWED_PREFIXES:
        return False
    if len(path.parts) < 2:
        return False

    return True


def get_domain_root(agents_dir: Path, domain_name: str) -> Optional[Path]:
    """Get the local filesystem path for a domain under ~/.agents."""
    spec = DOMAINS.get(domain_name)
    if not spec:
        return None
    return agents_dir / spec.relative_path
