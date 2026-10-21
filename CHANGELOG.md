# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-21

### Added
- Unified multi-device cloud synchronization engine across `~/.agents` sub-domains (`memory`, `skills`, `docs`, `rules`, `identity`, `traces`, `known_dids`).
- Decoupled stack-wide sync from `agents-memory`, providing a dedicated CLI and client/server protocol for local and cloud environments.
- Pure standard-library HTTP client (`urllib.request`) with zero external runtime dependencies.
- FastAPI cloud vault server (`agents-sync[server]`) with Dockerfile for deployment on Coolify and VPS.
- Chronological JSONL event stream merging with timestamp-based deduplication for multi-node and multi-user traces.
- 3-way markdown bullet and section merge with tombstone tracking (`.tombstones.json`) to prevent resurrection of deleted items.
- Deterministic multi-user identity merging for `identity.json`.
- Strict host key isolation preventing Ed25519 private keys (`*.ed25519`) from ever leaving the host.

### Security
- Bearer token passed strictly via environment variable (`AGENTS_SYNC_TOKEN`) or persistent config file rather than command-line argument flags, preventing token leakage in process tables.
