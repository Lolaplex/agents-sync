"""Wire protocol and payload formats for agents-sync."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "1.0"

# Endpoints
EP_HEALTH = "/health"
EP_STATUS = "/api/v1/status"
EP_SNAPSHOT = "/api/v1/snapshot"
EP_MERGE = "/api/v1/merge"


def make_merge_payload(
    node_id: str,
    bundle: Dict[str, str],
    stamps: Dict[str, str],
    tombstones: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "node_id": node_id,
        "bundle": bundle,
        "stamps": stamps,
        "tombstones": tombstones,
    }
