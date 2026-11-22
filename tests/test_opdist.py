"""Op-log distribution: envelope packing, peer cursors, dedupe, resume."""
import tempfile
import unittest
from pathlib import Path

from agents_sync.opdist import PeerBook, dedupe_key, deliver, pack_ops, receive


def make_ops(n, start=1, actor=None):
    return [{
        "tx": i,
        "origin_actor": actor,
        "origin_seq": i,
        "hlc": f"00000000000{i:02d}.00000.nodeA",
        "op": "assert",
        "note": None,
        "datoms": [{"e": f"e{i}", "a": "title", "v": f"v{i}", "added": True}],
    } for i in range(start, start + n)]


class Capture:
    def __init__(self):
        self.sent = []

    def __call__(self, envelopes):
        self.sent.append(envelopes)


class FakeIngest:
    """Stand-in for agents_store.ingest.ingest — records calls, reports
    all datoms applied. The real ingest is exercised in agents-store's
    own tests; here we verify the wire path only."""

    def __init__(self):
        self.calls = []
        self.deduped = set()

    def __call__(self, actor, seq, hlc, datoms):
        key = (actor, seq)
        if key in self.deduped:
            return type("R", (), {"applied": 0, "dupes": len(datoms), "refused": 0})()
        self.deduped.add(key)
        self.calls.append((actor, seq, hlc, list(datoms)))
        return type("R", (), {"applied": len(datoms), "dupes": 0, "refused": 0})()


class TestPack(unittest.TestCase):
    def test_local_ops_get_sender_actor(self):
        envs = pack_ops(make_ops(2), "nodeA")
        self.assertEqual(envs[0]["actor"], "nodeA")
        self.assertEqual(envs[0]["seq"], 1)
        self.assertEqual(envs[0]["v"], 1)

    def test_remote_ops_keep_true_origin(self):
        envs = pack_ops(make_ops(1, actor="nodeZ"), "nodeA")
        self.assertEqual(envs[0]["actor"], "nodeZ")

    def test_dedupe_key(self):
        env = pack_ops(make_ops(1), "nodeA")[0]
        self.assertEqual(dedupe_key(env), "nodeA:1")


class TestPeerBook(unittest.TestCase):
    def test_cursor_advance_ack_resync(self):
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            self.assertEqual(book.cursor("peer1"), 0)
            book.advance("peer1", 42)
            book.ack("peer1", 41)
            # persistence across instances
            book2 = PeerBook(Path(d) / "peers.json")
            self.assertEqual(book2.cursor("peer1"), 42)
            book2.resync("peer1")
            self.assertEqual(PeerBook(Path(d) / "peers.json").cursor("peer1"), 0)


class TestDeliver(unittest.TestCase):
    def test_deliver_reads_acked_not_optimistic(self):
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(3)
            sent = Capture()
            cursor = deliver(lambda c: [o for o in ops if o["tx"] > c],
                             book, "peer1", "nodeA", sent)
            self.assertEqual(cursor, 3)
            batch = sent.sent[0]
            self.assertEqual(batch["batch_cursor"], 3)
            self.assertEqual(len(batch["envelopes"]), 3)
            # no ack yet -> a crash here must redeliver everything
            sent2 = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "peer1", "nodeA", sent2)
            self.assertEqual(len(sent2.sent[0]["envelopes"]), 3)
            # ack arrives -> next deliver sends nothing
            book.ack("peer1", 3)
            sent3 = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "peer1", "nodeA", sent3)
            self.assertEqual(sent3.sent, [])

    def test_deliver_sends_only_unacked_ops(self):
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(5)
            book.ack("peer1", 3)
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "peer1", "nodeA", sent)
            self.assertEqual(len(sent.sent[0]["envelopes"]), 2)

    def test_origin_only_drops_foreign_ops(self):
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(1, actor=None) + make_ops(1, start=9, actor="nodeZ")
            ops[1]["tx"] = 4
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "hub", "nodeA", sent, origin_only=True)
            envs = sent.sent[0]["envelopes"]
            self.assertEqual(len(envs), 1)
            self.assertEqual(envs[0]["actor"], "nodeA")


class TestReceive(unittest.TestCase):
    def test_receive_applies_and_dedupes(self):
        envs = pack_ops(make_ops(2), "nodeA")
        ingest = FakeIngest()
        stats = receive(envs, ingest)
        self.assertEqual(stats["applied"], 2)
        self.assertEqual(len(ingest.calls), 2)
        # re-delivery: same envelopes again -> dupes at both layers
        stats2 = receive(envs, ingest)
        self.assertEqual(stats2["applied"], 0)
        self.assertEqual(len(ingest.calls), 2)

    def test_receive_seen_set_prefilter(self):
        envs = pack_ops(make_ops(1), "nodeA")
        seen = {dedupe_key(envs[0])}
        ingest = FakeIngest()
        stats = receive(envs, ingest, seen=seen)
        self.assertEqual(stats["envelopes"], 0)
        self.assertEqual(len(ingest.calls), 0)

    def test_full_roundtrip_two_nodes(self):
        """A's tail -> envelopes -> B's ingest; origin survives relay."""
        ops_a = make_ops(2)
        envs = pack_ops(ops_a, "nodeA")
        ingest_b = FakeIngest()
        receive(envs, ingest_b)
        (actor, seq, hlc, datoms) = ingest_b.calls[0]
        self.assertEqual((actor, seq), ("nodeA", 1))
        self.assertEqual(datoms[0][0], "e1")


if __name__ == "__main__":
    unittest.main()
