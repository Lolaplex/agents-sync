# AGENTS.md — agents-sync Developer Guidelines

## Architecture Principles

- **Two planes.** Plane 1 is the legacy file-bundle domain sync
  (`bundle.py`, `merge.py`, `tombstones.py`, `client.py`/`server.py` +
  `connect`/`sync`/`push`/`pull`/`serve` CLI) — being deprecated.
  Plane 2 is `opdist.py`: store op-log distribution, the forward path.
- **Op-log distribution (`opdist.py`).** `pack_ops` serializes a
  store's `ops_since()` tail into wire envelopes
  `{v:1, actor, seq, hlc, op:"assert", note, datoms:[{e,a,v,added}]}`.
  Dedupe identity is `(actor, seq)`: locally minted ops get the
  sender's node id stamped at send, re-broadcast ops keep their true
  origin — echo and re-delivery collapse in `BoundedSeen`. `PeerBook`
  persists per-peer cursors in `sync_peers.json`
  (`cursor`/`acked`/`advance`/`ack`/`resync`); delivery is ack-gated so
  re-delivery after loss is automatic. `deliver`/`receive` hand
  envelopes to a caller-supplied ingest callback — agents-sync owns
  transport, agents-store owns semantics (ord, dedupe by origin, view
  resolution). No agents-store import at the wire layer.
- **Workspace domain sync (Plane 1).** One client syncing workspace
  domains between hosts. Registered syncable domains
  (`domains.py:DOMAINS`): `memory`, `rules`, `skills`, `traces`,
  `personas`, `schedules`, `config` (`config.json`), `members`
  (`members.json`). There is no `docs` domain and no `identity.json` —
  those were dropped with the Host/Workspace split (host-only or gone).
- **Strict Host Key Isolation (Security Hard Rule).** Ed25519 private
  keys in `<host>/keys/*.ed25519` are HARD-BLOCKED from collection and
  syncing; `secrets/`, `run/` and ephemeral state are forbidden parts
  (`FORBIDDEN_*` + `is_path_safe` in `domains.py`). `known-dids.jsonl`
  is the sole exemption (public DIDs only). Host-only data — keys,
  `users.json`, `host_paths.json`, secrets vault — never syncs.
- **Path-Independent Wire Format.** Repo memories and project files
  are keyed strictly by slug (`projects/<slug>/`), never absolute host
  paths. Local path resolution happens per node.
- **Dependencies.** Client/CLI depend only on `agents-workspace`
  (workspace resolution, `AGENTS_WORKSPACE`); the wire is stdlib
  (`urllib.request`, `json`, `pathlib`, `hashlib`). The cloud server
  requires optional extra `[server]` (FastAPI/Uvicorn); `[test]`
  carries pytest and Starlette TestClient's `httpx2` runtime.

## Commands

```bash
python -m agents_sync connect <url> --token <token>
python -m agents_sync disconnect
python -m agents_sync status
python -m agents_sync push [--domain memory,skills]
python -m agents_sync pull [--domain memory,skills]
python -m agents_sync sync            # bi-directional pull + push
python -m agents_sync peers [--reset PEER | --reset-all]  # op-sync peer cursors (sync_peers.json)
python -m agents_sync serve [PORT]    # cloud server (requires [server] extra)
```

## Test Suite

```bash
python -m unittest discover tests
```
