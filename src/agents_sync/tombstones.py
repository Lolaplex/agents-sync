"""Deletion tombstones for multi-device sync across ~/.agents."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

TOMBSTONE_VERSION = 1
EXPIRY_DAYS = 90
TOMBSTONE_FILE = ".tombstones.json"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def mtime_iso(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return now_iso()
    return datetime.fromtimestamp(stamp, timezone.utc).strftime(_TS_FMT)


def norm_rel(rel: str) -> str:
    return str(rel).replace("\\", "/").strip().lstrip("/")


def empty_tombstones() -> dict[str, Any]:
    return {"version": TOMBSTONE_VERSION, "files": {}, "prefixes": {}, "bullets": {}}


def newer(write_iso: Optional[str], tomb_iso: Optional[str]) -> bool:
    """True when write_iso is strictly later UTC stamp than tombstone."""
    if not write_iso or not tomb_iso:
        return False
    return str(write_iso) > str(tomb_iso)


def load_tombstones(vault_root: Path) -> dict[str, Any]:
    path = vault_root / TOMBSTONE_FILE
    if not path.is_file():
        return empty_tombstones()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty_tombstones()
        files = data.get("files", {}) if isinstance(data.get("files"), dict) else {}
        prefixes = data.get("prefixes", {}) if isinstance(data.get("prefixes"), dict) else {}
        bullets = data.get("bullets", {}) if isinstance(data.get("bullets"), dict) else {}
        return {
            "version": TOMBSTONE_VERSION,
            "files": {norm_rel(k): str(v) for k, v in files.items()},
            "prefixes": {norm_rel(k): str(v) for k, v in prefixes.items()},
            "bullets": bullets,
        }
    except Exception:
        return empty_tombstones()


def save_tombstones(vault_root: Path, tombstones: dict[str, Any]) -> None:
    path = vault_root / TOMBSTONE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tombstones, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge_tombstones(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Union tombstones, keeping the newer deletion timestamp for any conflict."""
    out = empty_tombstones()
    for cat in ("files", "prefixes"):
        merged = dict(a.get(cat, {}))
        for k, v in b.get(cat, {}).items():
            if k not in merged or str(v) > str(merged[k]):
                merged[k] = str(v)
        out[cat] = merged

    # Merge bullets
    bullets_a = a.get("bullets", {})
    bullets_b = b.get("bullets", {})
    merged_bullets = {k: dict(v) for k, v in bullets_a.items()}
    for file_key, b_map in bullets_b.items():
        if file_key not in merged_bullets:
            merged_bullets[file_key] = dict(b_map)
        else:
            for b_text, b_stamp in b_map.items():
                if b_text not in merged_bullets[file_key] or str(b_stamp) > str(merged_bullets[file_key][b_text]):
                    merged_bullets[file_key][b_text] = str(b_stamp)
    out["bullets"] = merged_bullets
    return out


def record_file_deletion(vault_root: Path, rel_path: str, iso_ts: Optional[str] = None) -> None:
    t = load_tombstones(vault_root)
    t["files"][norm_rel(rel_path)] = iso_ts or now_iso()
    save_tombstones(vault_root, t)


def record_bullet_deletion(vault_root: Path, file_rel: str, bullet_text: str, iso_ts: Optional[str] = None) -> None:
    t = load_tombstones(vault_root)
    fkey = norm_rel(file_rel)
    if fkey not in t["bullets"]:
        t["bullets"][fkey] = {}
    t["bullets"][fkey][bullet_text.strip()] = iso_ts or now_iso()
    save_tombstones(vault_root, t)


def file_is_tombstoned(tombstones: dict[str, Any], rel_path: str, write_stamp: Optional[str] = None) -> bool:
    """Return True if path is deleted and not resurrected by a newer write stamp."""
    key = norm_rel(rel_path)
    tomb_stamp = tombstones.get("files", {}).get(key)
    if tomb_stamp:
        if write_stamp and newer(write_stamp, tomb_stamp):
            return False
        return True

    for prefix, p_stamp in tombstones.get("prefixes", {}).items():
        if key.startswith(prefix):
            if write_stamp and newer(write_stamp, p_stamp):
                return False
            return True
    return False
