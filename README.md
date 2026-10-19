# agents-sync

Unified multi-device cloud synchronizer for the `~/.agents` ecosystem.

Synchronizes:
- **Memory** (`~/.agents/memory/`): Markdown vault notes, facts, and project facts via 3-way merge and bullet tombstones.
- **Skills** (`~/.agents/skills/`): Workflows, agent skills (Caveman, Ponytail, etc.) across all machines.
- **Docs** (`~/.agents/docs/`): Technical documentation and platform playbooks.
- **Identity** (`~/.agents/identity.json`): User/session/alias maps.
- **Traces** (`~/.agents/traces/`): Observability and session history.
- **Known DIDs** (`~/.agents/known-dids.jsonl`): Public keys of trusted peers.

### Security
Private keys in `~/.agents/keys/*.ed25519` are strictly excluded from syncing and never leave the host machine.

### Quick Start

```bash
# Connect to your cloud vault
agents-sync connect https://vps.example.com:8765 --token YOUR_TOKEN

# Push local state
agents-sync push

# Pull remote state
agents-sync pull

# Full 2-way sync
agents-sync sync

# Run the cloud server on VPS
pip install "agents-sync[server]"
# Token is AGENTS_SYNC_TOKEN in the environment, not a command-line flag.
agents-sync serve 8765
```
