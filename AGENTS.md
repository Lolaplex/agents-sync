# AGENTS.md — agents-sync Developer Guidelines

## Architecture Principles
- **Unified Node Synchronizer**: One client syncing all sub-domains under `~/.agents/` (`memory`, `skills`, `docs`, `traces`, `identity`, `known-dids.jsonl`).
- **Zero Heavy Client Dependencies**: Client & CLI are pure Python standard library (`urllib.request`, `json`, `pathlib`, `hashlib`). Server requires optional extra `[server]` (FastAPI/Uvicorn).
- **Strict Host Key Isolation (Security Hard Rule)**: Ed25519 private keys in `~/.agents/keys/*.ed25519` are HARD-BLOCKED from collection and syncing. They never leave the host.
- **Path-Independent Wire Format**: Repo memories and project files are keyed strictly by slug (`projects/<slug>/`), never absolute host paths (`C:\Users\...` or `/home/...`). Local path resolution happens per node.
- **Domain-Specific Syncing**:
  - `memory/`: 3-way markdown & bullet-level CRDT/tombstone merge.
  - `skills/`: Directory mirroring (syncs `~/.agents/skills/` so all nodes share active workflows).
  - `docs/`: Technical RAG documentation store mirroring.
  - `traces/`: Append-only daily telemetry streams (partitioned per node).
  - `identity.json`: User/session/alias mappings.

## Commands
```bash
python -m agents_sync connect <url> --token <token>
python -m agents_sync disconnect
python -m agents_sync status
python -m agents_sync push [--domain memory,skills]
python -m agents_sync pull [--domain memory,skills]
python -m agents_sync sync            # bi-directional pull + push
python -m agents_sync serve [PORT]    # cloud server (requires [server] extra)
```

## Test Suite
```bash
python -m unittest discover tests
```
