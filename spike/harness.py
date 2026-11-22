"""Two-host fixture + channel stub + scenario answers for spike questions Q2-Q6.

Channel is function-call stubbed (no network — transport is agents-relay#40's
job); it injects seeded drop/dup/reorder so convergence is tested under real
interleavings, not just clean delivery.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from .oplog import HLC, Op, OpLog, fold, live_view, render_memory_md

WS = "test-ws"


@dataclass
class Host:
    id: str
    hlc: HLC = field(init=False)
    log: OpLog = field(init=False)

    def __post_init__(self) -> None:
        self.hlc = HLC(self.id)
        self.log = OpLog(WS)

    def emit(self, domain: str, type: str, payload: dict) -> Op:
        op = Op(ws=WS, actor=self.id, hlc=self.hlc.tick(),
                domain=domain, type=type, payload=payload)
        self.log.append(op)
        return op


class Channel:
    """Fault-injecting pipe host->host. drop/dup/reorder under a seed."""

    def __init__(self, seed: int, drop_p: float = 0.15, dup_p: float = 0.15) -> None:
        self.rng = random.Random(seed)
        self.drop_p = drop_p
        self.dup_p = dup_p

    def deliver(self, src: Host, dst: Host, ops: list[Op]) -> int:
        delivered = []
        for op in ops:
            if self.rng.random() < self.drop_p:
                continue
            delivered.append(op)
            if self.rng.random() < self.dup_p:
                delivered.append(op)  # duplicate frame
        self.rng.shuffle(delivered)
        n = 0
        for op in delivered:
            n += int(dst.log.append(op))
            dst.hlc.recv(op.hlc)
        return n


def converge(a: Host, b: Host, ch: Channel, rounds: int = 3) -> None:
    """Exchange until quiescent: replay full logs each round (dupes are cheap,
    dedupe by op_id). A `?since=` cursor test lives in scenario_resume."""
    for _ in range(rounds):
        ch.deliver(a, b, a.log.ops)
        ch.deliver(b, a, b.log.ops)


def view(h: Host) -> dict:
    return live_view(fold(list(h.log.iter()), base=dict(h.log.checkpoint_state or {})))


def eq(a: Host, b: Host) -> bool:
    return json.dumps(view(a), sort_keys=True, default=str) == \
           json.dumps(view(b), sort_keys=True, default=str)


# --- scenarios ---------------------------------------------------------------

def scenario_convergence(seed: int, n_ops: int = 60) -> dict:
    """Q3: random interleaved ops on all domains -> identical folded state."""
    a, b = Host("hostA"), Host("hostB")
    ch = Channel(seed)
    rng = random.Random(seed)
    kinds = [
        ("memory", "bullet.add", lambda i: {"key": f"k{i%15}", "text": f"fact {i}"}),
        ("memory", "bullet.remove", lambda i: {"key": f"k{i%15}"}),
        ("settings", "field.set", lambda i: {"key": f"f{i%6}", "value": i}),
        ("sessions", "event.append", lambda i: {"sid": f"s{i%3}", "event": f"e{i}"}),
        ("personas", "entity.upsert", lambda i: {"entity_id": f"p{i%4}", "data": {"n": i}}),
        ("personas", "entity.delete", lambda i: {"entity_id": f"p{i%4}"}),
        ("runlock", "run.start", lambda i: {"run": f"r{i%2}"}),
        ("runlock", "run.end", lambda i: {"run": f"r{i%2}"}),
    ]
    for i in range(n_ops):
        host = a if rng.random() < 0.5 else b
        dom, t, mk = kinds[rng.randrange(len(kinds))]
        host.emit(dom, t, mk(i))
        if rng.random() < 0.3:  # partial sync mid-stream
            converge(a, b, ch, rounds=1)
    converge(a, b, ch)
    return {"converged": eq(a, b), "ops_a": a.log.seq, "ops_b": b.log.seq,
            "view": view(a)}


def scenario_deterministic_view() -> dict:
    """Q2: memory markdown re-render byte-identical across replicas/orders."""
    a, b = Host("mA"), Host("mB")
    ch = Channel(7, drop_p=0.0, dup_p=0.0)
    for i in range(20):
        (a if i % 2 else b).emit("memory", "bullet.add",
                                 {"key": f"k{i}", "text": f"bullet {i}"})
    converge(a, b, ch)
    sa = fold(list(a.log.iter()))
    sb = fold(list(b.log.iter()))
    return {"identical": render_memory_md(sa) == render_memory_md(sb),
            "sample": render_memory_md(sa)[:120]}


def scenario_lww() -> dict:
    """Q3b: same-field race — deterministic winner = highest hlc."""
    a, b = Host("lA"), Host("lB")
    ch = Channel(3, drop_p=0.0, dup_p=0.0)
    a.emit("settings", "field.set", {"key": "model", "value": "A-model"})
    b.emit("settings", "field.set", {"key": "model", "value": "B-model"})
    converge(a, b, ch)
    va, vb = view(a)["settings"]["model"], view(b)["settings"]["model"]
    winner = max(a.log.ops[0].hlc, b.log.ops[0].hlc)
    expected = "B-model" if winner == b.log.ops[0].hlc else "A-model"
    return {"same": va == vb == expected, "value": va}


def scenario_resume(seed: int = 11, buffer: int = 20) -> dict:
    """Q4: replay window + resync marker when offline > buffer."""
    a, b = Host("rA"), Host("rB")
    ch = Channel(seed, drop_p=0.0, dup_p=0.0)
    for i in range(5):
        a.emit("sessions", "event.append", {"sid": "s", "event": f"e{i}"})
    ch.deliver(a, b, a.log.ops)
    cursor = b.log.seq
    for i in range(5, 5 + buffer + 10):  # b goes offline, buffer overflows
        a.emit("sessions", "event.append", {"sid": "s", "event": f"e{i}"})
    tail = a.log.replay(cursor)
    in_window = len(tail) <= buffer
    if in_window:
        ch.deliver(a, b, tail)
        resumed_ok = eq(a, b)
        resync_needed = False
    else:
        resync_needed = True
        ch.deliver(a, b, a.log.ops)  # resync: full state transfer
        resumed_ok = eq(a, b)
    return {"in_window": in_window, "resync_needed": resync_needed,
            "resumed_ok": resumed_ok, "tail": len(tail), "buffer": buffer}


def scenario_purge() -> dict:
    """Q6: purge removes redacted content physically; _seen blocks resurrection."""
    a, b = Host("pA"), Host("pB")
    ch = Channel(5, drop_p=0.0, dup_p=0.0)
    a.emit("memory", "bullet.add", {"key": "k1", "text": "key sk-SECRET-123 leaked"})
    a.emit("memory", "bullet.add", {"key": "k2", "text": "normal fact"})
    converge(a, b, ch)
    pred = lambda o: "sk-SECRET" in json.dumps(o.payload)
    na, nb = a.log.purge(pred), b.log.purge(pred)
    # re-fold state post-purge
    for h in (a, b):
        h.log.checkpoint_state = {}
        h.log.checkpoint()
    va = json.dumps(view(a))
    # re-delivery of purged op must not resurrect (seen-set keeps op_ids)
    resurrect = b.log.append(a.log.ops[0]) if a.log.ops else False
    return {"purged": na == nb == 1, "gone": "sk-SECRET" not in va,
            "resurrected": resurrect}


def scenario_migration() -> dict:
    """Q5: existing ws state -> import ops; cost + dedupe.

    Finding: re-import must use deterministic provenance (actor="import",
    hlc derived from source mtime) — else identical content re-imports as
    NEW ops (dedupe is by op_id). Fold-level dedupe by entity key still
    protects the view, but the log doubles. Spec: import ops = deterministic.
    """
    h = Host("mig")
    state = {"memory": [f"existing fact {i}" for i in range(50)],
             "settings": {f"key{i}": i for i in range(20)}}
    import_hlc = HLC("import", clock=lambda: 1000.0)

    def import_into(host: Host) -> None:
        for k, text in enumerate(state["memory"]):
            op = Op(ws=WS, actor="import", hlc=import_hlc.pack(1000000 + k, 0),
                    domain="memory", type="bullet.add",
                    payload={"key": f"imp-{k}", "text": text})
            host.log.append(op)
        for i, (k, v) in enumerate(state["settings"].items()):
            op = Op(ws=WS, actor="import", hlc=import_hlc.pack(2000000 + i, 0),
                    domain="settings", type="field.set",
                    payload={"key": k, "value": v})
            host.log.append(op)

    import_into(h)
    before = h.log.seq
    h2 = Host("mig2")
    import_into(h2)  # same deterministic provenance -> identical op_ids
    n = h.log.merge(h2.log.ops)
    return {"import_ops": before, "remerge_added": n,
            "view_ok": len(view(h)["memory"]) == 50 and len(view(h)["settings"]) == 20}


def run_all() -> dict:
    return {
        "Q2_deterministic_view": scenario_deterministic_view(),
        "Q3_convergence": scenario_convergence(seed=42),
        "Q3_convergence_s2": scenario_convergence(seed=7),
        "Q3_convergence_s3": scenario_convergence(seed=99),
        "Q3b_lww": scenario_lww(),
        "Q4_resume": scenario_resume(),
        "Q5_migration": scenario_migration(),
        "Q6_purge": scenario_purge(),
    }


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run_all(), indent=1, ensure_ascii=False, default=str))
