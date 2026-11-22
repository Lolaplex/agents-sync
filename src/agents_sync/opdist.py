"""Store op-log distribution: envelopes, peer cursors, dedupe, resume.

Ownership boundary: agents-sync delivers, agents-store decides. This
module is a pure-stdlib carrier — it serializes ops pulled from a
store's ops_since() tail into wire envelopes, tracks per-peer send
cursors, and hands received envelopes to a caller-supplied ingest
callback. No agents-store import is required at the wire layer; the
receiving side owns semantics (ord, dedupe by origin, view resolution).

Envelope shape (one store tx on the wire):

    {
      "v":      1,                 # envelope format version
      "actor":  "<node_id>",       # origin node of the tx
      "seq":    <int>,             # origin-local tx id (dedupe key)
      "hlc":    "<ord key>",       # global ordering key
      "op":     "assert",          # store tx op kind
      "note":   <str|null>,
      "datoms": [{"e","a","v","added"}, ...]
    }

Dedupe identity is (actor, seq): a tx re-broadcast through any number of
hubs keeps its true origin, so echo and re-delivery collapse.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

ENVELOPE_VERSION = 1
PEERS_FILE = "sync_peers.json"

IngestFn = Callable[[str, int, str, Iterable[Tuple[str, str, str, bool]]], Any]


def pack_ops(ops: List[Dict[str, Any]], node_id: str) -> List[Dict[str, Any]]:
    """Store ops_since rows -> wire envelopes.

    Locally-minted ops carry origin_actor=None; the sender stamps its own
    node_id so receivers dedupe on (sender, tx). Re-broadcast ops keep
    their true origin identity.
    """
    out: List[Dict[str, Any]] = []
    for op in ops:
        out.append({
            "v": ENVELOPE_VERSION,
            "actor": op.get("origin_actor") or node_id,
            "seq": int(op.get("origin_seq") or op["tx"]),
            "hlc": op.get("hlc") or "",
            "op": op.get("op") or "assert",
            "note": op.get("note"),
            "datoms": [dict(d) for d in op.get("datoms", [])],
        })
    return out


def dedupe_key(env: Dict[str, Any]) -> str:
    return f"{env['actor']}:{env['seq']}"


class PeerBook:
    """Per-peer send/receive cursors in a JSON sidecar file.

    cursor = last origin-seq offered to this peer (send side);
    acked  = last origin-seq the peer confirmed (receive side).
    Resume = "send ops after cursor"; resync = reset cursor to 0.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._peers: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._peers = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._peers = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._peers, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def cursor(self, peer: str) -> int:
        return int(self._peers.get(peer, {}).get("cursor", 0))

    def advance(self, peer: str, cursor: int) -> None:
        rec = self._peers.setdefault(peer, {"cursor": 0, "acked": 0})
        rec["cursor"] = max(int(rec["cursor"]), int(cursor))
        self._save()

    def ack(self, peer: str, seq: int) -> None:
        rec = self._peers.setdefault(peer, {"cursor": 0, "acked": 0})
        rec["acked"] = max(int(rec["acked"]), int(seq))
        self._save()

    def resync(self, peer: str) -> None:
        """Drop the peer cursor: next pull replays the full tail."""
        self._peers[peer] = {"cursor": 0, "acked": 0}
        self._save()


def deliver(
    ops_since: Callable[[int], List[Dict[str, Any]]],
    book: PeerBook,
    peer: str,
    node_id: str,
    send: Callable[[List[Dict[str, Any]]], None],
    *,
    limit: int = 1000,
) -> int:
    """Pull the local tail since the peer's cursor and send envelopes.

    ops_since(cursor) -> store tail rows; send(envelopes) -> transport
    (relay channel op frames, HTTP POST, whatever). Returns the cursor
    advanced to (the max tx sent, or the unchanged cursor).
    """
    cursor = book.cursor(peer)
    ops = ops_since(cursor)[:limit]
    if not ops:
        return cursor
    send(pack_ops(ops, node_id))
    new_cursor = max(o["tx"] for o in ops)
    book.advance(peer, new_cursor)
    return new_cursor


def receive(
    envelopes: List[Dict[str, Any]],
    ingest: IngestFn,
    seen: set[str] | None = None,
) -> Dict[str, int]:
    """Hand received envelopes to the store ingest callback.

    `seen` is an optional local dedupe set for callers that cannot rely
    on the store's origin index (e.g. dry-run validation); the store
    remains the authoritative deduper.
    """
    stats = {"applied": 0, "dupes": 0, "refused": 0, "envelopes": 0}
    for env in envelopes:
        key = dedupe_key(env)
        if seen is not None and key in seen:
            stats["dupes"] += len(env.get("datoms", []))
            continue
        stats["envelopes"] += 1
        datoms = [(d["e"], d["a"], d["v"], bool(d["added"])) for d in env.get("datoms", [])]
        res = ingest(env["actor"], int(env["seq"]), env.get("hlc", ""), datoms)
        applied = getattr(res, "applied", 0)
        dupes = getattr(res, "dupes", 0)
        refused = getattr(res, "refused", 0)
        stats["applied"] += applied
        stats["dupes"] += dupes
        stats["refused"] += refused
        if seen is not None:
            seen.add(key)
    return stats
