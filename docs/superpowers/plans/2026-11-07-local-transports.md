# Local Transports Implementation Plan

**Goal:** Remove the SmartThings Cloud runtime and expose exactly Local only (RPC) and Hybrid Local + OCF.

**Architecture:** A single existing coordinator owns RPC and optional OCF. RPC owns core state. OCF owns only verified audio-setting fields. Hybrid without a credential keeps RPC working and presents an actionable local warning. No new unproven hardware writes.

**Tech Stack:** Home Assistant 2026.9.4, Python 3.14, aiohttp RPC, smartthings-local 0.1.24, cbor2.

**Spec:** User approved full Cloud removal, two local control modes, preservation of existing identity/history/OCF secrets, and temporary RPC operation in Hybrid without a credential.

## Global Constraints
- No live HA/device writes, power changes, secret inspection, deployment or commits.
- Preserve config entry/unique IDs, device/entity registry identity/history and stored OCF credentials.
- `local_only` means RPC only; `hybrid_local_ocf` enables optional OCF. Existing saved OCF configurations migrate to Hybrid.
- Legacy Cloud-only entries without a host require local reconfiguration, never silent deletion or guessed address.
- Remove OAuth/PAT/API/SSE clients, Cloud-only services and dependencies, not just labels.
- Keep bounded local retry, typed readback, partial TTL, command serialization and cancellation behavior.
- Legacy strings may remain only in migration and migration tests, never executable API code.

## Tasks
- [x] Setup/migration: native local config flow, two options, legacy migration, OCF warning, no credentials required to start RPC.
- [x] State/coordinator: remove Cloud tier and fallback, preserve RPC/OCF transactions, TTL and shutdown.
- [x] Device: remove Cloud commands/status, preserve RPC mappings and guarded OCF controls/diagnostics.
- [x] UI/dependencies/docs: remove obsolete services/selects/auth modules, update translations and documentation.
- [x] Automated verification: migration/identity/secret preservation, RPC-only isolation, Hybrid warning/recovery, all local/OCF regressions, lint and no Cloud runtime source scan. Installed-HA/hardware verification remains separate and was not performed.

## Review Focus
- Migration of hostless legacy entries must leave reconfiguration reachable.
- RPC-only must not read or poll OCF even when an old credential is retained.
- OCF outage must not erase core RPC state or replay a write.
- Removing obsolete cloud rows must be scoped to the owning entry.
- Neither migration nor tests may print or delete the saved secret.

## Review Fixes
- Unbound legacy hosts cannot activate RPC until physical identity is confirmed.
- Pending validation/confirmation checks an immutable entry snapshot before saving.
- Forget drains polling/transactions through cancellation-safe coordinator detach; RPC remains usable and no closed client is retained.
