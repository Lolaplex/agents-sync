# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Deprecated

### Removed

### Fixed

### Security
- The cloud server no longer accepts the bearer token on the command line (`serve --token`). Set `AGENTS_SYNC_TOKEN` in the environment. The Docker entrypoint does not copy that value into process arguments.

## [0.1.0] - 2026-10-11

### Added
- Unified multi-device cloud synchronization engine across `~/.agents` sub-domains (`memory`, `skills`, `docs`, `identity`, `traces`, `known_dids`).
- Pure standard-library HTTP client (`urllib.request`) with zero external runtime dependencies.
- FastAPI cloud vault server (`agents-sync[server]`) for deployment on Coolify and VPS.
- Chronological JSONL event stream merging with timestamp-based deduplication for multi-node and multi-user traces.
- 3-way markdown bullet and section merge with tombstone tracking (`.tombstones.json`) to prevent resurrection of deleted items.
- Deterministic multi-user identity merging for `identity.json`.
- Strict host key isolation preventing Ed25519 private keys (`*.ed25519`) from ever leaving the host.
