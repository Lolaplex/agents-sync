"""Op-log core: HLC ordering, op schema, append-only log, checkpoints, compaction.

Design (WS-2 workshop):
- ops are the source of truth; files are materialized views.
- hlc = "<wall_ms>-<counter>-<actor>" — string-sortable total order without coordination.
- op_id = sha256 of canonical op content — dedupe is natural, replays are free.
- two delete levels: tombstone (merge-visible) vs purge (physical, redaction).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator


class HLC:
    """Hybrid logical clock: wall-ms + per-actor counter + actor tiebreak."""

    def __init__(self, actor: str, clock: Callable[[], float] | None = None) -> None:
        self.actor = actor
        self._clock = clock or time.time
        self._last_ms = 0
        self._counter = 0

    def tick(self) -> str:
        ms = int(self._clock() * 1000)
        if ms <= self._last_ms:
            self._counter += 1
        else:
            self._last_ms = ms
            self._counter = 0
        return self.pack(self._last_ms, self._counter)

    def recv(self, remote: str) -> str:
        """Merge a remote HLC; next tick stays strictly after both."""
        r_ms, r_ctr, _ = self.unpack(remote)
        ms = int(self._clock() * 1000)
        self._last_ms = max(self._last_ms, ms, r_ms)
        self._counter = max(self._counter, r_ctr) + 1 if self._last_ms == max(ms, r_ms) else 0
        return self.tick()

    def pack(self, ms: int, ctr: int) -> str:
        return f"{ms:013d}-{ctr:04d}-{self.actor}"

    @staticmethod
    def unpack(hlc: str) -> tuple[int, int, str]:
        ms, ctr, actor = hlc.rsplit("-", 2)
        return int(ms), int(ctr), actor


@dataclass(frozen=True)
class Op:
    ws: str
    actor: str
    hlc: str
    domain: str
    type: str
    payload: dict[str, Any]
    ts: float = 0.0

    @property
    def op_id(self) -> str:
        canon = json.dumps(
            {"w": self.ws, "a": self.actor, "h": self.hlc, "d": self.domain,
             "t": self.type, "p": self.payload},
            sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:24]

    def to_json(self) -> dict[str, Any]:
        return {"op_id": self.op_id, "ws": self.ws, "actor": self.actor,
                "hlc": self.hlc, "domain": self.domain, "type": self.type,
                "payload": self.payload, "ts": self.ts}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Op":
        return cls(ws=d["ws"], actor=d["actor"], hlc=d["hlc"],
                   domain=d["domain"], type=d["type"],
                   payload=dict(d.get("payload") or {}), ts=float(d.get("ts") or 0))


@dataclass
class OpLog:
    """Append-only per-ws op log with checkpoint + compaction + purge.

    ``seq`` = local append order (channel cursor); ``hlc`` = global order.
    Both kept: seq for replay windows, hlc for merge order.
    """
    ws: str
    ops: list[Op] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)
    checkpoint_seq: int = 0
    checkpoint_state: dict[str, Any] = field(default_factory=dict)

    def append(self, op: Op) -> bool:
        """Idempotent append. Returns False when the op was already applied."""
        if op.op_id in self._seen:
            return False
        self._seen.add(op.op_id)
        self.ops.append(op)
        return True

    def merge(self, other: "OpLog | list[Op]") -> int:
        incoming = other.ops if isinstance(other, OpLog) else other
        n = 0
        for op in sorted(incoming, key=lambda o: o.hlc):
            n += int(self.append(op))
        return n

    def replay(self, since_seq: int = 0) -> list[Op]:
        return self.ops[max(0, since_seq - self.checkpoint_seq):]

    @property
    def seq(self) -> int:
        """Absolute append order — survives compaction (channel cursor)."""
        return self.checkpoint_seq + len(self.ops)

    def checkpoint(self) -> dict[str, Any]:
        self.compact(upto_seq=self.seq)
        return self.checkpoint_state

    def compact(self, upto_seq: int | None = None) -> None:
        """Fold ops up to ``upto_seq`` (absolute) into the checkpoint, drop them."""
        if upto_seq is None or upto_seq > self.seq:
            upto_seq = self.seq
        if upto_seq <= self.checkpoint_seq:
            return
        rel = upto_seq - self.checkpoint_seq
        self.checkpoint_state = fold(self.ops[:rel], base=self.checkpoint_state or None)
        self.checkpoint_seq = upto_seq
        self.ops = self.ops[rel:]

    def purge(self, pred: Callable[[Op], bool]) -> int:
        """Physical removal (redaction). ``_seen`` keeps purged op_ids so a
        re-delivery over the channel can never resurrect redacted content.
        Caller must re-fold the checkpoint afterwards."""
        before = len(self.ops)
        self.ops = [o for o in self.ops if not pred(o)]
        return before - len(self.ops)

    def iter(self) -> Iterator[Op]:
        return iter(self.ops)


def fold(ops: list[Op], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fold ops (any arrival order) into domain state — merge rules table.

    Deterministic: sorted by (hlc, op_id) so every replica folds identically.
    """
    state: dict[str, Any] = base if base is not None else {}
    for op in sorted(ops, key=lambda o: (o.hlc, o.op_id)):
        d = state.setdefault(op.domain, {})
        t, p = op.type, op.payload
        if op.domain == "memory":
            key = p["key"]
            adds = d.setdefault("adds", {})
            if t == "bullet.add":
                adds[key] = (op.hlc, p["text"])
            elif t == "bullet.remove":
                d.setdefault("removed", set()).add(key)
            live = {k: v for k, v in adds.items() if k not in d.get("removed", set())}
            d["live"] = dict(sorted(live.items(), key=lambda kv: (kv[1][0], kv[0])))
        elif op.domain == "settings":
            if t == "field.set":
                cur = d.setdefault("fields", {}).get(p["key"])
                if cur is None or op.hlc > cur[0]:
                    d["fields"][p["key"]] = (op.hlc, p["value"])
        elif op.domain in ("sessions", "store", "decisions", "events"):
            if t == "event.append":
                items = d.setdefault("items", {})
                if op.op_id not in items:
                    items[op.op_id] = (op.hlc, p)
        elif op.domain == "personas":
            # LWW per entity over upsert+delete events — delete wins ties only
            # by hlc order like any other event, so a later upsert revives.
            eid = p["entity_id"]
            meta = d.setdefault("meta", {})
            cur = meta.get(eid)
            if cur is None or op.hlc > cur[0]:
                meta[eid] = (op.hlc, t)
                if t == "entity.upsert":
                    d.setdefault("entities", {})[eid] = (op.hlc, p["data"])
                else:
                    d.setdefault("entities", {}).pop(eid, None)
        elif op.domain == "runlock":
            if t == "run.start":
                d.setdefault("runs", {})[p["run"]] = (op.hlc, op.actor)
            elif t == "run.end":
                d.setdefault("runs", {}).pop(p["run"], None)
    return state


def live_view(state: dict[str, Any]) -> dict[str, Any]:
    """Simplify folded state into per-domain live views."""
    out: dict[str, Any] = {}
    if "memory" in state:
        out["memory"] = [v[1] for v in state["memory"].get("live", {}).values()]
    if "settings" in state:
        out["settings"] = {k: v[1] for k, v in state["settings"].get("fields", {}).items()}
    for dom in ("sessions", "store", "decisions", "events"):
        if dom in state:
            items = state[dom].get("items", {})
            out[dom] = [v for _, v in sorted(items.values())]
    if "personas" in state:
        out["personas"] = {k: v[1] for k, v in state["personas"].get("entities", {}).items()}
    if "runlock" in state:
        out["runlock"] = {k: v[1] for k, v in state["runlock"].get("runs", {}).items()}
    return out


def render_memory_md(state: dict[str, Any]) -> str:
    """Deterministic materialized view of the memory domain."""
    bullets = live_view(state).get("memory", [])
    return "# memory\n\n" + "".join(f"- {b}\n" for b in bullets)
