"""Op-log distribution: envelope packing, peer cursors, dedupe, resume."""
import json
import tempfile
import unittest
from pathlib import Path

from agents_sync.opdist import (
    BoundedSeen, PeerBook, dedupe_key, deliver, pack_ops, receive)


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

    def __call__(self, actor, seq, hlc, datoms, *, op="assert", note=None):
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


class TestBoundedSeen(unittest.TestCase):
    def test_repeat_add_and_capacity(self):
        seen = BoundedSeen(4)
        seen.add("k1")
        seen.add("k1")  # repeat key -> LRU refresh, must not crash
        for i in range(20):  # > capacity -> LRU eviction, must not crash
            seen.add(f"d{i}")
        self.assertEqual(len(seen._keys), 4)
        for k in ("d16", "d17", "d18", "d19"):
            self.assertIn(k, seen)
        self.assertNotIn("k1", seen)
        self.assertNotIn("d0", seen)

    def test_repeat_key_refreshes_lru_position(self):
        seen = BoundedSeen(3)
        for k in ("a", "b", "c"):
            seen.add(k)
        seen.add("a")  # refresh: b is now oldest
        seen.add("d")
        self.assertIn("a", seen)
        self.assertNotIn("b", seen)

    def test_size_must_be_positive(self):
        with self.assertRaises(ValueError):
            BoundedSeen(0)


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

    def test_sibling_instances_never_clobber(self):
        """Two PeerBook objects on one file: each ack() must merge the
        other's persisted peers instead of overwriting the whole file."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sync_peers.json"
            book_a = PeerBook(path)
            book_b = PeerBook(path)
            book_a.ack("dev1", 50)
            book_b.ack("dev2", 30)
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["dev1"]["acked"], 50)
            self.assertEqual(disk["dev2"]["acked"], 30)
            # the later writer merged the sibling's peers into memory too
            self.assertEqual(book_b.acked("dev1"), 50)
            # monotonic: a lower seq from a stale instance never regresses
            book_b2 = PeerBook(path)
            book_b2.ack("dev1", 40)
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(disk["dev1"]["acked"], 50)

    def test_acked_reloads_disk_writes(self):
        """A sibling's (or another process's) write must be picked up by
        read-only acked()/cursor() calls — the book reloads whenever the
        file's (mtime_ns, size) changed."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sync_peers.json"
            book_a = PeerBook(path)
            book_b = PeerBook(path)
            book_a.ack("dev1", 7)
            # never saved anything itself -> purely in-memory would be 0
            self.assertEqual(book_b.acked("dev1"), 7)
            self.assertEqual(book_b.cursor("dev1"), 7)
            # raw external write (different size + mtime) -> reload too
            path.write_text(json.dumps(
                {"dev1": {"cursor": 9, "acked": 9},
                 "dev2": {"cursor": 3, "acked": 3}}), encoding="utf-8")
            self.assertEqual(book_a.acked("dev1"), 9)
            self.assertEqual(book_a.acked("dev2"), 3)

    def test_advance_moves_cursor_only(self):
        """advance() is the legacy optimistic cursor: it must never lift
        the acked floor, or a crash after a bare send would lose ops."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            book.advance("peer1", 10)
            self.assertEqual(book.cursor("peer1"), 10)
            self.assertEqual(book.acked("peer1"), 0)  # floor untouched
            ops = make_ops(3)
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "peer1", "nodeA", sent)
            self.assertEqual(len(sent.sent[0]["envelopes"]), 3)
            sent2 = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "peer1", "nodeA", sent2)
            self.assertEqual(len(sent2.sent[0]["envelopes"]), 3)

    def test_threaded_acks_merge(self):
        """Sibling instances + threads: the per-path RLock and the
        advisory lockfile serialize read-modify-write so no ack is lost."""
        import threading
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sync_peers.json"
            books = [PeerBook(path), PeerBook(path)]

            def hammer(book, n):
                for i in range(15):
                    book.ack(f"peer{n}", i)

            threads = [threading.Thread(target=hammer, args=(books[n % 2], n))
                       for n in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for n in range(6):
                self.assertEqual(PeerBook(path).acked(f"peer{n}"), 14)


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

    def test_deliver_scans_past_filtered_run(self):
        """1500 foreign ops then 1 local op: the sendable tx must be
        found past the first `limit` slice, not starve at tx 1000."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(1500, actor="nodeZ") + make_ops(1, start=1501)
            sent = Capture()
            cursor = deliver(lambda c: [o for o in ops if o["tx"] > c],
                             book, "hub", "nodeA", sent, origin_only=True)
            self.assertEqual(cursor, 1501)
            envs = sent.sent[0]["envelopes"]
            self.assertEqual(len(envs), 1)
            self.assertEqual(envs[0]["seq"], 1501)

    def test_deliver_scans_paged_tail(self):
        """Same starvation via a chunked ops_since (like a store-side
        LIMIT): the scan must keep paging across empty-after-filter
        chunks until the sendable op shows up."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(1500, actor="nodeZ") + make_ops(1, start=1501)
            sent = Capture()

            def paged(c, n=400):
                return [o for o in ops if o["tx"] > c][:n]

            cursor = deliver(paged, book, "hub", "nodeA", sent,
                             origin_only=True)
            self.assertEqual(cursor, 1501)
            self.assertEqual(len(sent.sent[0]["envelopes"]), 1)

    def test_deliver_skips_pure_filtered_tail(self):
        """A tail that is 100% filtered-out is ack-skipped durably so
        the next tick never rescans it — without that, every drain
        would re-read the same dead range forever."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(1200, actor="nodeZ")
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "hub", "nodeA", sent, origin_only=True)
            self.assertEqual(sent.sent, [])
            self.assertEqual(book.acked("hub"), 1200)
            # next tick: nothing left to scan, nothing sent
            sent2 = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "hub", "nodeA", sent2, origin_only=True)
            self.assertEqual(sent2.sent, [])

    def test_origin_only_includes_local_origins(self):
        """Locally-minted projection ops (proj@/bind@/import@ actors)
        count as local when their prefix is in local_origins."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = (make_ops(1, actor=None)
                   + make_ops(1, start=2, actor="proj@host:ses")
                   + make_ops(1, start=3, actor="nodeZ"))
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "hub", "nodeA", sent, origin_only=True,
                    local_origins=("proj@",))
            envs = sent.sent[0]["envelopes"]
            self.assertEqual(len(envs), 2)
            self.assertEqual(envs[0]["actor"], "nodeA")
            self.assertEqual(envs[1]["actor"], "proj@host:ses")

    def test_deliver_forwards_kwargs_to_ops_since(self):
        """When ops_since accepts filter kwargs they go straight down:
        the store can then filter at SQL level instead of shipping raw
        ops for in-memory filtering."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = make_ops(2, actor="nodeZ") + make_ops(1, start=3)
            sent = Capture()

            def filtered(c, *, origin_only=False, exclude_origin=None,
                         local_origins=()):
                assert origin_only is True
                assert exclude_origin == "hub"
                return [o for o in ops
                        if o["tx"] > c and o.get("origin_actor") is None]

            cursor = deliver(filtered, book, "hub", "nodeA", sent,
                             origin_only=True, exclude_origin="hub",
                             local_origins=("proj@",))
            self.assertEqual(cursor, 3)
            self.assertEqual(len(sent.sent[0]["envelopes"]), 1)

    def test_bare_str_origins_raise_typeerror(self):
        """A bare str/bytes silently iterates single characters — the
        most damaging config typo — so it is a hard TypeError."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops_since = lambda c: []
            for bad_kwargs in ({"local_origins": "proj@"},
                               {"local_origins": b"proj@"},
                               {"exclude_origin_prefixes": "proj@peerB:"},
                               {"exclude_origin_prefixes": b"proj@"}):
                with self.assertRaises(TypeError):
                    deliver(ops_since, book, "peer1", "nodeA",
                            Capture(), **bad_kwargs)

    def test_origin_only_empty_actor_is_local(self):
        """agents-store mints local ops with origin_actor='' — deliver
        must count '' as local exactly like the store's own filter."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            ops = (make_ops(1, actor="")
                   + make_ops(1, start=2, actor="nodeZ"))
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "hub", "nodeA", sent, origin_only=True)
            envs = sent.sent[0]["envelopes"]
            self.assertEqual(len(envs), 1)
            self.assertEqual(envs[0]["actor"], "nodeA")

    def test_node_id_auto_added_to_forwarded_local_origins(self):
        """When local_origins go down to ops_since, node_id rides along:
        otherwise a store that treats local_origins as the complete
        local-actor set would drop our own node's ops."""
        with tempfile.TemporaryDirectory() as d:
            book = PeerBook(Path(d) / "peers.json")
            captured = []

            def ops_since(c, *, local_origins=(), **_kw):
                captured.append(tuple(local_origins))
                return []

            deliver(ops_since, book, "peer1", "nodeA", Capture(),
                    origin_only=True, local_origins=("proj@",))
            self.assertEqual(captured[0], ("proj@", "nodeA"))
            # already present -> not duplicated
            captured.clear()
            deliver(ops_since, book, "peer1", "nodeA", Capture(),
                    origin_only=True, local_origins=("proj@", "nodeA"))
            self.assertEqual(captured[0], ("proj@", "nodeA"))


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

    def test_receive_passes_op_and_note(self):
        """scrub tombstones need op='scrub' + note=<entity> on the wire."""
        envs = [{"v": 1, "actor": "hub", "seq": 7, "hlc": "x",
                 "op": "scrub", "note": "e99", "datoms": []}]

        class Recorder:
            def __init__(self):
                self.kw = None

            def __call__(self, actor, seq, hlc, datoms, *, op="assert", note=None):
                self.kw = (op, note)
                return type("R", (), {"applied": 0, "dupes": 0, "refused": 0})()

        rec = Recorder()
        receive(envs, rec)
        self.assertEqual(rec.kw, ("scrub", "e99"))

    def test_receive_skips_malformed_mid_batch(self):
        """A malformed envelope must be counted/skipped, never abort the
        batch — otherwise one bad frame poison-blocks the ack for every
        valid envelope behind it."""
        good1 = pack_ops(make_ops(1, start=1), "nodeA")
        good2 = pack_ops(make_ops(1, start=4), "nodeA")
        missing_actor = {"v": 1, "seq": 2, "datoms": []}
        bad_version = {"v": 99, "actor": "n", "seq": 3, "datoms": []}
        non_dict = "not-an-envelope"
        ingest = FakeIngest()
        stats = receive(
            good1 + [missing_actor, bad_version, non_dict] + good2, ingest)
        self.assertEqual(len(ingest.calls), 2)
        self.assertEqual(stats["applied"], 2)
        self.assertEqual(stats["refused"], 3)
        self.assertEqual(stats["envelopes"], 2)


    def test_receive_refuses_invalid_fields(self):
        """actor=''/None, seq=True (bool), hlc=None and non-str datom
        fields are refused per-envelope — a batch must never reach
        ingest with a frame that would crash it mid-commit."""
        ingest = FakeIngest()
        good = pack_ops(make_ops(1, start=1), "nodeA")

        def mutated(tx, **changes):
            env = pack_ops(make_ops(1, start=tx), "nodeA")[0]
            env.update(changes)
            return env

        bad = [
            mutated(2, actor=""),
            mutated(3, actor=None),
            mutated(4, actor=7),
            mutated(5, seq=True),
            mutated(6, seq="7"),
            mutated(7, hlc=None),
            mutated(8, datoms=[{"e": "e8", "a": 123, "v": "v8",
                                "added": True}]),
            mutated(9, datoms=[{"e": "e9", "a": "a9", "v": None,
                                "added": True}]),
        ]
        stats = receive(good + bad, ingest)
        self.assertEqual(len(ingest.calls), 1)
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["refused"], len(bad))
        self.assertEqual(stats["envelopes"], 1)

    def test_receive_fully_refused_op_not_marked_seen(self):
        """applied==0 & dupes==0 & refused>0 -> skip the seen mark: a
        redelivery must get another chance once the refusal clears."""
        envs = pack_ops(make_ops(1), "nodeA")
        key = dedupe_key(envs[0])
        seen = BoundedSeen()

        def refusing(*_a, **_k):
            return type("R", (), {"applied": 0, "dupes": 0,
                                  "refused": 1})()

        stats = receive(envs, refusing, seen=seen)
        self.assertEqual(stats["refused"], 1)
        self.assertNotIn(key, seen)
        # a real delivery still marks seen -> next receive dedupes
        receive(envs, FakeIngest(), seen=seen)
        self.assertIn(key, seen)


class TestDeliverExcludeOrigin(unittest.TestCase):
    def test_hub_never_echoes_peer_ops(self):
        """Hub tail holds dev-B ops; deliver to B must drop them."""
        with tempfile.TemporaryDirectory() as td:
            book = PeerBook(Path(td) / "peers.json")
            ops = make_ops(1, start=1) + make_ops(2, start=2, actor="devB")
            sent = Capture()
            deliver(lambda since: ops, book, "devB", "hub", sent,
                    exclude_origin="devB")
            envs = sent.sent[0]["envelopes"]
            self.assertEqual(len(envs), 1)
            self.assertEqual(envs[0]["seq"], 1)

    def test_exclude_origin_prefixes_suppresses_echoes(self):
        """WsSync drain: proj@<peer>:/bind@<peer>:/import@<peer>: actors
        are the peer's own echoes — suppress by prefix, keep the rest."""
        with tempfile.TemporaryDirectory() as td:
            book = PeerBook(Path(td) / "peers.json")
            ops = (make_ops(1, start=1)
                   + make_ops(1, start=2, actor="proj@peerB:s1")
                   + make_ops(1, start=3, actor="bind@peerB:s2")
                   + make_ops(1, start=4, actor="proj@peerA:s3")
                   + make_ops(1, start=5))
            sent = Capture()
            cursor = deliver(
                lambda c: [o for o in ops if o["tx"] > c],
                book, "peerB", "nodeA", sent,
                exclude_origin_prefixes=("proj@peerB:", "bind@peerB:"))
            self.assertEqual(cursor, 5)
            actors = [e["actor"] for e in sent.sent[0]["envelopes"]]
            self.assertEqual(len(actors), 3)
            self.assertNotIn("proj@peerB:s1", actors)
            self.assertNotIn("bind@peerB:s2", actors)
            self.assertIn("proj@peerA:s3", actors)
            self.assertEqual(actors.count("nodeA"), 2)

    def test_exclude_origin_prefixes_dead_range_acks_forward(self):
        """A tail that is 100% suppressed echoes is ack-skipped forward —
        suppression must never move the cursor backwards or rescan."""
        with tempfile.TemporaryDirectory() as td:
            book = PeerBook(Path(td) / "peers.json")
            book.ack("peerB", 2)
            ops = make_ops(3, start=3, actor="import@peerB:x")
            sent = Capture()
            deliver(lambda c: [o for o in ops if o["tx"] > c],
                    book, "peerB", "nodeA", sent,
                    exclude_origin_prefixes=("import@peerB:",))
            self.assertEqual(sent.sent, [])
            self.assertEqual(book.acked("peerB"), 5)


if __name__ == "__main__":
    unittest.main()
