"""Merging algorithms for markdown, json, and generic text."""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Set

_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def is_bullet(line: str) -> bool:
    return bool(_BULLET_RE.match(line))


def bullet_payload(line: str) -> str:
    m = _BULLET_RE.match(line)
    return m.group(1).strip() if m else line.strip()


def merge_markdown(
    local_text: str,
    remote_text: str,
    tombstoned_bullets: Optional[Set[str]] = None,
) -> str:
    """Intelligently merge two markdown documents preserving unique bullets and sections."""
    if local_text == remote_text:
        return local_text

    tombs = tombstoned_bullets or set()
    local_lines = local_text.splitlines()
    remote_lines = remote_text.splitlines()

    # Fast path: if one side is empty, return the other minus tombs
    if not local_text.strip():
        return "\n".join(l for l in remote_lines if not (is_bullet(l) and bullet_payload(l) in tombs))
    if not remote_text.strip():
        return "\n".join(l for l in local_lines if not (is_bullet(l) and bullet_payload(l) in tombs))

    out_lines: List[str] = []
    seen_bullets: Set[str] = set()

    # Add local lines first
    for line in local_lines:
        if is_bullet(line):
            b_text = bullet_payload(line)
            if b_text in tombs:
                continue
            seen_bullets.add(b_text)
        out_lines.append(line)

    # Union new bullets from remote under their respective sections
    for line in remote_lines:
        if is_bullet(line):
            b_text = bullet_payload(line)
            if b_text in tombs or b_text in seen_bullets:
                continue
            seen_bullets.add(b_text)
            out_lines.append(line)
        elif line not in out_lines and line.startswith("#"):
            # Header exists in remote but not local
            out_lines.append(line)

    return "\n".join(out_lines).strip() + "\n"


def merge_json_identity(local_text: str, remote_text: str) -> str:
    """Merge ~/.agents/identity.json preserving users and sessions."""
    try:
        local_data = json.loads(local_text) if local_text.strip() else {}
    except Exception:
        local_data = {}
    try:
        remote_data = json.loads(remote_text) if remote_text.strip() else {}
    except Exception:
        remote_data = {}

    merged: dict[str, Any] = {"users": {}, "sessions": {}}
    
    # Merge users
    local_users = local_data.get("users", {})
    remote_users = remote_data.get("users", {})
    for u_id in set(local_users.keys()) | set(remote_users.keys()):
        u_loc = local_users.get(u_id, {})
        u_rem = remote_users.get(u_id, {})
        merged_aliases = list(set(u_loc.get("aliases", [])) | set(u_rem.get("aliases", [])))
        # Pick the user definition with non-empty active_session or display
        picked = u_rem if u_rem.get("active_session") else u_loc
        u_out = dict(picked)
        u_out["aliases"] = sorted(merged_aliases)
        merged["users"][u_id] = u_out

    # Merge sessions
    local_sessions = local_data.get("sessions", {})
    remote_sessions = remote_data.get("sessions", {})
    for s_id in set(local_sessions.keys()) | set(remote_sessions.keys()):
        s_loc = local_sessions.get(s_id, {})
        s_rem = remote_sessions.get(s_id, {})
        merged["sessions"][s_id] = s_rem if s_rem else s_loc

    return json.dumps(merged, indent=2) + "\n"


def merge_file_content(
    rel_path: str,
    local_content: Optional[str],
    remote_content: Optional[str],
    local_stamp: Optional[str] = None,
    remote_stamp: Optional[str] = None,
    tombstoned_bullets: Optional[Set[str]] = None,
) -> str:
    """Dispatch merging based on file type."""
    if local_content is None and remote_content is not None:
        return remote_content
    if remote_content is None and local_content is not None:
        return local_content
    if local_content is None and remote_content is None:
        return ""

    loc = local_content or ""
    rem = remote_content or ""

    if loc == rem:
        return loc

    if rel_path.endswith("identity.json"):
        return merge_json_identity(loc, rem)

    if rel_path.endswith(".md") or rel_path.endswith(".mdc"):
        return merge_markdown(loc, rem, tombstoned_bullets=tombstoned_bullets)

    # Default LWW for code / configs
    if remote_stamp and local_stamp and remote_stamp > local_stamp:
        return rem
    return loc
