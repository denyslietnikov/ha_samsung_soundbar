# Q800F Channels and Audio Sync Implementation Plan

**Goal:** Add locally verified channel trims and audio delay to Hybrid Local + OCF, with no controls in RPC-only.

**Architecture:** Existing coordinator/queue/TTL owns all state and writes. Canonical model profiles require authenticated Q800F observations. The channel endpoint accepts a one-entry name/value patch; full lists containing status ACK without effect. Never publish requested values without typed GET confirmation.

**Spec:** Q800F_FEATURES_IMPLEMENTATION_PLAN.md section 2 and the user's request to implement it. Owner approved bounded device probes with restore and NO power changes. No deployment or commits requested.

## Constraints
- Preserve existing identities, entity IDs, modes and credential isolation; no Cloud code or new public arbitrary POST service.
- Known raw channel names Spk_Center/Side/Wide/Front_Top/Rear/Rear_Top/Rear_Side map to existing safe canonical channel_level_* keys.
- status1 means available;0/-1 do not authorize controls. Do not guess rear hardware from model name. Unsupported fields invalidate only their controls.
- Only verified ranges/profiles become writable. Q800F manual English pages5–6: channels -6..+6; delay0..300ms. Probes confirmed five channels, Center boundaries, Sync0/25/300; raw1 rounds to25, so step25. All original values restored, no power writes. No invented channel dB.
- Single-entry channel write leaves all other entries and fields unsent; compare fresh neighbors after command. No full stale-list replay.
- Guard fresh resource/support/availability and fresh RPC power inside the shared lock; no Sound Mode prerequisite is changed implicitly. Unknown types/duplicates/support0, partial preflight, mismatch/timeout/cancellation must not send additional POSTs or wake the soundbar.

## Tasks
- [x] Confirm range/payload/readback/restore on Q800F without power changes; record bounded evidence, not a universal model claim.
- [x] Correct read projection and strict per-channel support/schema metadata; preserve partial TTL and sensitive-data allowlist.
- [x] Add canonical profiles, safe local write grants, device accessors/transaction guards and neighbor readback checks.
- [x] Native translated Channel group numbers and Sound: Audio Sync slider; late discovery, independent availability, stable new IDs.
- [x] Unit and real HA tests for schema/types/boundaries/readback/no-replay/no-wake, mode switch/reload/off/partial/unsupported, queue races and forgotten credentials.
- [x] Update README/main plan, review and run full baseline regressions before guarded transfer to main checkout.

Verification: 317 unit tests and 225 network-blocked HA tests pass; Ruff,
translation JSON and workflow YAML pass. Review found and fixed cancellation
before POST and per-field preflight failure isolation. Installed-HA and subjective
AV/DSP behavior remain field tests, not claimed by this harness.
