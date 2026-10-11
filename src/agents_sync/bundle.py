"""Packaging and applying sync bundles across ~/.agents."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .domains import DOMAINS, get_domain_root, is_path_safe
from .merge import merge_file_content
from .tombstones import (
    file_is_tombstoned,
    load_tombstones,
    mtime_iso,
    norm_rel,
    record_file_deletion,
    save_tombstones,
)


def collect_bundle(
    agents_dir: Path,
    enabled_domains: Optional[List[str]] = None,
    node_id: str = "node",
    stamps: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Gather all allowed files across enabled domains under ~/.agents."""
    bundle: Dict[str, str] = {}
    stamps_map = stamps if stamps is not None else {}
    target_domains = enabled_domains or list(DOMAINS.keys())

    for domain_name in target_domains:
        domain_root = get_domain_root(agents_dir, domain_name)
        if not domain_root or not domain_root.exists():
            continue

        spec = DOMAINS[domain_name]

        if spec.is_file:
            if not is_path_safe(spec.relative_path):
                continue
            try:
                content = domain_root.read_text(encoding="utf-8", errors="replace")
                key = spec.relative_path
                bundle[key] = content
                stamps_map[key] = mtime_iso(domain_root)
            except OSError:
                continue
            continue

        # Domain is a directory
        for p in domain_root.rglob("*"):
            if not p.is_file():
                continue
            rel_to_agents = p.relative_to(agents_dir).as_posix()
            if not is_path_safe(rel_to_agents):
                continue

            wire_key = rel_to_agents

            try:
                bundle[wire_key] = p.read_text(encoding="utf-8", errors="replace")
                stamps_map[wire_key] = mtime_iso(p)
            except OSError:
                continue

    return bundle


def apply_bundle(
    agents_dir: Path,
    remote_bundle: Dict[str, str],
    remote_tombstones: Optional[Dict[str, Any]] = None,
    remote_stamps: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], List[str]]:
    """Merge and write remote bundle files to local ~/.agents."""
    written: List[str] = []
    deleted: List[str] = []
    tombs = remote_tombstones or {}
    r_stamps = remote_stamps or {}

    local_tombstones = load_tombstones(agents_dir)

    for wire_key, remote_content in remote_bundle.items():
        if not is_path_safe(wire_key):
            continue

        r_stamp = r_stamps.get(wire_key)
        if file_is_tombstoned(tombs, wire_key, write_stamp=r_stamp) or file_is_tombstoned(
            local_tombstones, wire_key, write_stamp=r_stamp
        ):
            target_file = agents_dir / wire_key
            if target_file.is_file():
                try:
                    target_file.unlink()
                    deleted.append(wire_key)
                except OSError:
                    pass
            continue

        target_file = agents_dir / wire_key
        local_content: Optional[str] = None
        local_stamp: Optional[str] = None
        if target_file.is_file():
            try:
                local_content = target_file.read_text(encoding="utf-8", errors="replace")
                local_stamp = mtime_iso(target_file)
            except OSError:
                pass

        file_tombs_bullets = set(tombs.get("bullets", {}).get(wire_key, {}).keys())

        merged = merge_file_content(
            wire_key,
            local_content=local_content,
            remote_content=remote_content,
            local_stamp=local_stamp,
            remote_stamp=r_stamp,
            tombstoned_bullets=file_tombs_bullets,
        )

        if local_content != merged:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(merged, encoding="utf-8")
            written.append(wire_key)

    return written, deleted
