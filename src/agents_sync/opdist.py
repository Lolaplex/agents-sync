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

import inspect
import json
import logging
import os
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Protocol, Tuple

log = logging.getLogger(__name__)

ENVELOPE_VERSION = 1
PEERS_FILE = "sync_peers.json"


class IngestFn(Protocol):
    """Store-side ingest callback: (actor, seq, hlc, datoms, *, op, note)
    -> result carrying .applied/.dupes/.refused datom counts."""

    def __call__(
        self,
        actor: str,
        seq: int,
        hlc: str,
        datoms: Iterable[Tuple[str, str, str, bool]],
        *,
        op: str = "assert",
        note: str | None = None,
    ) -> Any: ...


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
            "seq": (int(op["origin_seq"]) if op.get("origin_seq") is not None
                    else int(op["tx"])),
            "hlc": op.get("hlc") or "",
            "op": op.get("op") or "assert",
            "note": op.get("note"),
            "datoms": [dict(d) for d in op.get("datoms", [])],
        })
    return out


def dedupe_key(env: Dict[str, Any]) -> str:
    return f"{env['actor']}:{env['seq']}"


class BoundedSeen:
    """Fast-path dedupe ring: last N envelope keys in memory. The store's
    origin index remains the only durable truth — this is a pure
    short-circuit for hot redelivery loops."""

    def __init__(self, size: int = 4096):
        if size < 1:
            raise ValueError("BoundedSeen size must be >= 1")
        self._size = size
        self._keys: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def add(self, key: str) -> None:
        if key in self._keys:
            self._keys.move_to_end(key)
            return
        self._keys[key] = None
        while len(self._keys) > self._size:
            self._keys.popitem(last=False)


_BOOK_LOCKS: Dict[str, threading.RLock] = {}
_BOOK_LOCKS_GUARD = threading.Lock()


def _book_lock(path: Path) -> threading.RLock:
    """One lock per resolved peers-file path, shared by every PeerBook
    instance pointing at it (multiple hub workers can hold separate
    books on the same sync_peers.json)."""
    key = os.path.normcase(str(Path(path).resolve()))
    with _BOOK_LOCKS_GUARD:
        lock = _BOOK_LOCKS.get(key)
        if lock is None:
            lock = _BOOK_LOCKS[key] = threading.RLock()
        return lock


class PeerBook:
    """Per-peer send/receive cursors in a JSON sidecar file.

    acked = last origin-seq the peer confirmed — the single durable
    delivery cursor. cursor() reports the same position; resume =
    "send ops after acked"; resync = reset the peer to 0.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = _book_lock(self.path)
        self._peers: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _read(self) -> Dict[str, Dict[str, Any]]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _load(self) -> None:
        with self._lock:
            self._peers = self._read()

    def _save(self, reset: Iterable[str] = ()) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reset = frozenset(reset)
        # Reload-merge under the per-path lock BEFORE the replace: peers
        # written by sibling instances merge monotonically (max per
        # field) so one worker never clobbers another's cursors. Peers
        # in `reset` take the in-memory record verbatim (resync intent).
        with self._lock:
            disk = self._read()
            merged: Dict[str, Dict[str, Any]] = {}
            for peer in set(disk) | set(self._peers):
                if peer in reset:
                    merged[peer] = dict(self._peers[peer])
                    continue
                d = disk.get(peer) or {}
                m = self._peers.get(peer) or {}
                merged[peer] = {
                    "cursor": max(int(d.get("cursor", 0)),
                                  int(m.get("cursor", 0))),
                    "acked": max(int(d.get("acked", 0)),
                                 int(m.get("acked", 0))),
                }
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=1, sort_keys=True)
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._peers = merged

    def cursor(self, peer: str) -> int:
        """Durable delivery cursor — identical to acked(peer)."""
        return self.acked(peer)

    def acked(self, peer: str) -> int:
        """Durable receive cursor the peer confirmed — the send floor."""
        return int(self._peers.get(peer, {}).get("acked", 0))

    def advance(self, peer: str, cursor: int) -> None:
        """Backward-compat alias for ack(): the acked cursor is the only
        position deliver() consults — a send without ack must redeliver,
        so no separate optimistic cursor is kept."""
        self.ack(peer, cursor)

    def ack(self, peer: str, seq: int) -> None:
        rec = self._peers.setdefault(peer, {"cursor": 0, "acked": 0})
        rec["acked"] = max(int(rec["acked"]), int(seq))
        rec["cursor"] = max(int(rec["cursor"]), int(seq))
        self._save()

    def resync(self, peer: str) -> None:
        """Drop the peer cursor: next pull replays the full tail."""
        self._peers[peer] = {"cursor": 0, "acked": 0}
        self._save(reset={peer})


def _since_kwargs(
    ops_since: Callable[..., List[Dict[str, Any]]],
    origin_only: bool,
    exclude_origin: str | None,
    local_origins: Tuple[str, ...],
) -> Dict[str, Any]:
    """Filter kwargs the ops_since callable accepts. Forwarded filters
    let the store reject ops at SQL level; the in-memory check in
    deliver() stays authoritative either way (idempotent)."""
    try:
        params = inspect.signature(ops_since).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_all = any(p.kind is inspect.Parameter.VAR_KEYWORD
                      for p in params.values())
    kw: Dict[str, Any] = {}
    for name, value in (("origin_only", origin_only),
                        ("exclude_origin", exclude_origin),
                        ("local_origins", local_origins)):
        p = params.get(name)
        if accepts_all or (p is not None
                           and p.kind is not
                           inspect.Parameter.POSITIONAL_ONLY):
            kw[name] = value
    return kw


def deliver(
    ops_since: Callable[..., List[Dict[str, Any]]],
    book: PeerBook,
    peer: str,
    node_id: str,
    send: Callable[[Dict[str, Any]], None],
    *,
    limit: int = 1000,
    origin_only: bool = False,
    exclude_origin: str | None = None,
    local_origins: Tuple[str, ...] = (),
    ws: str = "",
) -> int:
    """Pull the local tail since the peer's ACKED cursor and send a batch.

    The send cursor is NEVER advanced optimistically: it moves only when
    the peer acknowledges. A crash after send() must redeliver, so the
    floor is book.acked(peer), moved by PeerBook.ack on the ack path,
    not here.

    The batch envelope carries {"v", "ws", "batch_cursor", "envelopes"};
    the receiver acks batch_cursor back over the same channel once its
    ingest commit lands.

    origin_only=True drops re-broadcast ops (a spoke must never send
    foreign-origin ops back to the hub that delivered them). Ops minted
    locally under a projection actor (proj@/bind@/import@...) still count
    as local when their prefix is listed in local_origins.
    exclude_origin=<peer> drops ops the peer originally minted — a hub
    must never echo a device's own datoms back at it.

    Filtering happens while paging the tail: chunks that come back
    empty-after-filter never starve the scan at `limit` — it keeps
    reading until `limit` sendable ops are collected or the tail runs
    out. A scanned range that is 100% filtered-out is ack-skipped
    (book.ack) so it is never rescanned; delivered-but-unacked ops are
    never skipped — they redeliver until the peer acks batch_cursor.
    """
    since = book.acked(peer)
    kwargs = _since_kwargs(ops_since, origin_only, exclude_origin,
                           local_origins)
    sendable: List[Dict[str, Any]] = []
    scan = since
    max_scanned = since
    while len(sendable) < limit:
        batch = ops_since(scan, **kwargs)
        if not batch:
            break
        hi = max(o["tx"] for o in batch)
        if hi <= scan:
            break  # no forward progress — bail instead of rescanning
        max_scanned = max(max_scanned, hi)
        for o in batch:
            if len(sendable) >= limit:
                break
            if o["tx"] <= scan:
                continue  # defensive: never re-collect a scanned tx
            actor = o.get("origin_actor")
            if origin_only and actor is not None and actor != node_id \
                    and not any(actor.startswith(p) for p in local_origins):
                continue
            if exclude_origin is not None and actor == exclude_origin:
                continue
            sendable.append(o)
        scan = hi
    if not sendable:
        if max_scanned > since:
            # every scanned op was filtered out: skip the dead range
            # durably or the next tick would rescan it forever.
            book.ack(peer, max_scanned)
        return book.acked(peer)
    batch_cursor = max(o["tx"] for o in sendable)
    send({
        "v": ENVELOPE_VERSION,
        "ws": ws,
        "batch_cursor": batch_cursor,
        "envelopes": pack_ops(sendable, node_id),
    })
    return batch_cursor


def receive(
    envelopes: List[Dict[str, Any]],
    ingest: IngestFn,
    seen: BoundedSeen | None = None,
) -> Dict[str, int]:
    """Hand received envelopes to the store ingest callback.

    `seen` is an optional local dedupe set for callers that cannot rely
    on the store's origin index (e.g. dry-run validation); the store
    remains the authoritative deduper.
    """
    stats = {"applied": 0, "dupes": 0, "refused": 0, "envelopes": 0}
    for env in envelopes:
        # Malformed or foreign-version envelopes are counted and skipped
        # one by one: a single bad frame must never abort the batch and
        # poison-block the ack for the valid envelopes behind it.
        if not isinstance(env, dict) or env.get("v") != ENVELOPE_VERSION:
            stats["refused"] += 1
            log.warning("opdist: refused non-v%d envelope", ENVELOPE_VERSION)
            continue
        try:
            key = dedupe_key(env)
            seq = int(env["seq"])
            datoms = [(d["e"], d["a"], d["v"], bool(d["added"]))
                      for d in env.get("datoms", [])]
        except (KeyError, TypeError, ValueError):
            stats["refused"] += 1
            log.warning("opdist: refused malformed envelope")
            continue
        if seen is not None and key in seen:
            stats["dupes"] += len(datoms)
            continue
        stats["envelopes"] += 1
        res = ingest(
            env["actor"], seq, env.get("hlc", ""), datoms,
            op=env.get("op") or "assert", note=env.get("note"),
        )
        applied = getattr(res, "applied", 0)
        dupes = getattr(res, "dupes", 0)
        refused = getattr(res, "refused", 0)
        stats["applied"] += applied
        stats["dupes"] += dupes
        stats["refused"] += refused
        if seen is not None:
            seen.add(key)
    return stats
