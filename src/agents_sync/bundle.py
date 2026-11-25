"""Packaging and applying sync bundles across ~/.agents."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from .domains import DOMAINS, get_domain_root, is_path_safe
from .merge import merge_file_content
from .tombstones import (
    file_is_tombstoned,
    load_tombstones,
    merge_tombstones,
    mtime_iso,
    norm_rel,
    parse_iso_timestamp,
    prune_tombstones,
    record_file_deletion,
    save_tombstones,
)


def _contained(
    agents_dir: Path,
    wire_key: str,
    enabled_domains: Optional[List[str]] = None,
) -> Optional[Path]:
    """Resolve wire_key under agents_dir; None when it escapes the root
    or when the RESOLVED in-root path is itself not sync-safe.

    Containment alone is not enough for intra-workspace symlinks:
    memory/evil.md -> ../secrets.json resolves inside agents_dir, so the
    post-resolution relative path must pass the same denylist as the wire
    key before any write/delete is allowed.
    """
    try:
        base = agents_dir.resolve()
        target = (agents_dir / wire_key).resolve()
        if not target.is_relative_to(base):
            return None
        rel_resolved = target.relative_to(base).as_posix()
        if not is_path_safe(rel_resolved) \
                or not _wire_key_enabled(rel_resolved, enabled_domains):
            return None
        return target
    except (OSError, RuntimeError, ValueError):
        return None


def _wire_key_enabled(
    wire_key: str,
    enabled_domains: Optional[List[str]],
) -> bool:
    """True when a wire key belongs to one of the enabled sync domains.

    None means the legacy full registered set. An empty or unknown domain
    list enables nothing (fail closed): it must never widen back to all
    domains by accident.
    """
    if enabled_domains is None:
        return True
    key = norm_rel(wire_key).lower()
    for domain_name in enabled_domains:
        if not isinstance(domain_name, str):
            continue
        spec = DOMAINS.get(domain_name.strip().lower())
        if spec is None:
            continue
        root = norm_rel(spec.relative_path).lower()
        if spec.is_file:
            if key == root:
                return True
        elif key.startswith(root + "/"):
            return True
    return False


def _iter_domain_files(
    agents_dir: Path,
    enabled_domains: Optional[List[str]] = None,
) -> Iterator[Tuple[str, Path]]:
    """Yield (wire_key, local_path) for syncable files under enabled domains."""
    base = agents_dir.resolve()
    target_domains = (
        list(DOMAINS.keys()) if enabled_domains is None else enabled_domains
    )
    for domain_name in target_domains:
        domain_root = get_domain_root(agents_dir, domain_name)
        if not domain_root or not domain_root.exists():
            continue
        spec = DOMAINS[domain_name]
        candidates = [domain_root] if spec.is_file else list(domain_root.rglob("*"))
        for p in candidates:
            if not p.is_file():
                continue
            try:
                resolved = p.resolve()
                if not resolved.is_relative_to(base):
                    continue  # symlink escape
                rel = resolved.relative_to(base).as_posix()
            except (OSError, ValueError):
                continue
            if not is_path_safe(rel):
                continue
            yield rel, resolved


def _atomic_write(path: Path, content: str) -> None:
    """Write content via temp file + os.replace (no half-written sync state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def collect_bundle(
    agents_dir: Path,
    enabled_domains: Optional[List[str]] = None,
    node_id: str = "node",
    stamps: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Gather all allowed files across enabled domains under ~/.agents."""
    bundle: Dict[str, str] = {}
    stamps_map = stamps if stamps is not None else {}

    for wire_key, path in _iter_domain_files(agents_dir, enabled_domains):
        try:
            bundle[wire_key] = path.read_text(encoding="utf-8", errors="replace")
            stamps_map[wire_key] = mtime_iso(path)
        except OSError:
            continue

    return bundle


def apply_bundle(
    agents_dir: Path,
    remote_bundle: Dict[str, str],
    remote_tombstones: Optional[Dict[str, Any]] = None,
    remote_stamps: Optional[Dict[str, str]] = None,
    enabled_domains: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """Merge and write remote bundle files to local ~/.agents.

    Every wire key must survive is_path_safe() AND resolve to a path
    strictly inside agents_dir — crafted relatives and symlink escapes
    are skipped, never written. Tombstones (local + remote, pruned past
    EXPIRY_DAYS) are enforced unconditionally: a deletion propagates
    even when the remote omitted the deleted key from its bundle.
    """
    written: List[str] = []
    deleted: List[str] = []
    tombs = dict(remote_tombstones or {})
    tombs["files"] = {
        norm_rel(k): str(v) for k, v in tombs.get("files", {}).items()
    }
    tombs["prefixes"] = {
        norm_rel(k): str(v) for k, v in tombs.get("prefixes", {}).items()
    }
    r_stamps = remote_stamps or {}

    local_tombstones = load_tombstones(agents_dir)
    all_tombs = prune_tombstones(merge_tombstones(local_tombstones, tombs))

    for wire_key, remote_content in remote_bundle.items():
        if not is_path_safe(wire_key) \
                or not _wire_key_enabled(wire_key, enabled_domains):
            continue
        target_file = _contained(agents_dir, wire_key, enabled_domains)
        if target_file is None or target_file.is_dir():
            continue

        r_stamp = r_stamps.get(wire_key)
        if file_is_tombstoned(all_tombs, wire_key, write_stamp=r_stamp):
            if target_file.is_file():
                try:
                    target_file.unlink()
                    deleted.append(wire_key)
                except OSError:
                    pass
            continue

        local_content: Optional[str] = None
        local_stamp: Optional[str] = None
        if target_file.is_file():
            try:
                local_content = target_file.read_text(encoding="utf-8", errors="replace")
                local_stamp = mtime_iso(target_file)
            except OSError:
                pass

        file_tombs_bullets = set(
            all_tombs.get("bullets", {}).get(norm_rel(wire_key), {}).keys()
        )

        merged = merge_file_content(
            wire_key,
            local_content=local_content,
            remote_content=remote_content,
            local_stamp=local_stamp,
            remote_stamp=r_stamp,
            tombstoned_bullets=file_tombs_bullets,
        )

        if local_content != merged:
            _atomic_write(target_file, merged)
            # Preserve the authoritative remote write time AFTER the
            # atomic replace. A wall-clock sync time would masquerade as a
            # newer write and resurrect content against later tombstones.
            stamp_epoch = parse_iso_timestamp(r_stamp)
            if stamp_epoch is not None:
                try:
                    os.utime(target_file, (stamp_epoch, stamp_epoch))
                except (OSError, ValueError, OverflowError):
                    pass
            written.append(wire_key)

    # Second pass: enforce deletions for keys the remote bundle does not
    # carry at all — tombstoned local files, plus tombstone entries
    # pointing at files still on disk. Local mtime acts as the write
    # stamp so a file re-created after the deletion stays resurrected.
    candidates: Set[str] = set(all_tombs.get("files", {}).keys())
    candidates.update(
        rel for rel, _ in _iter_domain_files(agents_dir, enabled_domains)
    )
    for rel_key in candidates:
        if rel_key in remote_bundle or not is_path_safe(rel_key) \
                or not _wire_key_enabled(rel_key, enabled_domains):
            continue
        target_file = _contained(agents_dir, rel_key, enabled_domains)
        if target_file is None or not target_file.is_file():
            continue
        if file_is_tombstoned(all_tombs, rel_key, write_stamp=mtime_iso(target_file)):
            try:
                target_file.unlink()
                deleted.append(rel_key)
            except OSError:
                pass

    return written, deleted
