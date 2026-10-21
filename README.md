<h1 align="center">.agents / sync</h1>

<p align="center">
  <a href="https://github.com/Lolaplex/agents-sync/releases"><img src="https://img.shields.io/badge/version-0.1.0-blue.svg?style=flat-square" alt="Version 0.1.0"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://pypi.org/project/agents-sync/"><img src="https://img.shields.io/pypi/v/agents-sync.svg?style=flat-square" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg?style=flat-square" alt="License"></a>
</p>

<p align="center">
  <strong>Unified multi-device cloud synchronizer for the entire <code>~/.agents</code> ecosystem.</strong><br>
  Zero heavy client dependencies. Standalone sync engine decoupling and replacing legacy memory-only sync.
</p>

---

> [!NOTE]
> **Stack-Wide Sync vs Legacy Memory Sync:**
> Cloud synchronization was originally coupled inside `agents-memory`. To support modern multi-device development workflows, VPS services, and cloud agent environments, sync has been completely extracted into this dedicated standalone package (`agents-sync`).
> `agents-sync` unifies state across all sub-domains of `~/.agents` (`memory`, `skills`, `docs`, `rules`, `identity`, `traces`, `known-dids`), providing a single source of truth across all your machines.

---

## Synchronized Domains

| Domain | Path | Sync Strategy |
|---|---|---|
| **Memory** | `~/.agents/memory/` | 3-way markdown merge with bullet-level tombstone tracking (`.tombstones.json`). |
| **Skills** | `~/.agents/skills/` | Mirror active agent workflows and skills across all workstations. |
| **Docs** | `~/.agents/docs/` | Mirror technical platform documentation and reference sheets. |
| **Rules** | `~/.agents/rules/` | Sync global agent rule definitions. |
| **Identity** | `~/.agents/identity.json` | Deterministic merge for user, session, and alias mappings. |
| **Traces** | `~/.agents/traces/` | Append-only chronological event stream merging with timestamp deduplication. |
| **Known DIDs** | `~/.agents/known-dids.jsonl` | Append-only public keys of verified peers and agents. |

### Security & Key Isolation
> [!IMPORTANT]
> **Strict Host Key Isolation:** Private Ed25519 agent keys located in `~/.agents/keys/*.ed25519` are hard-blocked from bundle collection and synchronization. Private keys never leave the local machine.

---

## Quickstart

### 1. Installation

Install the client (zero external dependencies, pure Python standard library):
```bash
pip install agents-sync
```

### 2. Connect to Cloud Vault

```bash
# Connect to your remote vault server
agents-sync connect https://vps.example.com:8765 --token YOUR_SECRET_TOKEN

# Check connection health and status
agents-sync status
```

### 3. Synchronize State

```bash
# Bi-directional sync (push local changes + pull remote changes)
agents-sync sync

# Or selectively push / pull specific domains:
agents-sync push --domains memory,skills
agents-sync pull --domains memory,skills

# Disconnect and remove local credentials:
agents-sync disconnect
```

---

## Cloud Server Deployment

Run a private cloud vault server on your VPS, server, or Coolify instance:

```bash
pip install "agents-sync[server]"

# Token is provided via environment variable:
export AGENTS_SYNC_TOKEN="your-secure-bearer-token"
export AGENTS_SYNC_VAULT_DIR="/var/lib/agents-sync-vault"
agents-sync serve 8765
```

### Docker / Coolify

```bash
docker run -d \
  --name agents-sync \
  -p 8765:8765 \
  -e AGENTS_SYNC_TOKEN="your-secure-bearer-token" \
  -v agents_sync_vault:/root/.agents-sync-vault \
  ghcr.io/lolaplex/agents-sync:latest
```

---

## Wire Protocol & Path Independence

Project memories and repository facts are keyed strictly by abstract slug (`projects/<slug>/`), never by absolute host paths (`C:\Users\...` or `/home/...`). Each local node independently resolves slugs to its own local checkouts via its configured inventory scan roots.
