# Q800F Playback and Metadata Recon

**Goal:** Execute section3's read/correlation phase without fabricating playback
controls or reviving the removed Artwork entity.

**Design:** Extend existing dump_ocf_status with an explicit media preset and
fixed media href subset. Audio remains the default. A pure allowlisted projection
reports recognized protocol enums, raw bounded counters (units unverified), and
presence/type of title/artist/album/thumbnail only. No arbitrary strings, URLs,
identities or raw payloads enter state, logs or responses. Media reads are opt-in,
serialized through the existing coordinator and never join background polling.
Existing RPC ownership, audio TTL and writes remain unchanged.

**Approaches:** Publishing entities now would infer semantics from empty eARC
responses; guessing POST commands would risk current playback. The approved
roadmap requires action-correlated readback first, so implement the diagnostic
phase and ask the owner to provide AirPlay play/pause/stop samples. Any playback
writes require new consent. No power or volume changes, no deployment or commits.

## Tasks
- [x] Read current Q800F endpoints and schema with identity validation and no writes.
- [x] TDD pure projections: strict enums/integers, explicit missing/empty/invalid,
      sensitive nested fields discarded, bounded output, no invented support.
- [x] Extend existing diagnostic service with media preset/exact href allowlist,
      audio default unchanged, hrefs override preset, no extra polling/cache,
      lifecycle checks and resource-scoped errors, auth/identity hard stop.
- [x] Real HA tests: service request/response, no POST/core/polling regressions,
      RPC-only no Store/DTLS, cancellation/forget and no untrusted output.
- [x] Correlate owner-controlled AirPlay playback. Record actual results
      separately from unverified commands/DSP behavior; update README/main plan.
- [x] Review, full unit/HA tests, lint/JSON/YAML; guarded apply to develop.

AirPlay correlation was initially deferred, then completed with owner-controlled
play/pause/resume/disconnect on 2026-11-07. PLAYING/play becomes STOPPED/stop on
pause, resets position0 and retains title/artist/album; disconnect clears those
fields and returns the input through ARC to E_ARC. Both phases keep RPC power on.
Thumbnail remains empty and duration0. The position rate is roughly1000 units/s,
not yet verified against the phone timeline. Exact observed uppercase statuses
now pass diagnostics with no UI/power inference. No playback/PWR/volume writes.
Cast/Roon, track changes, actual playback commands and artwork remain unverified.

Initial diagnostics verification: 344 unit tests and 266 network-blocked HA tests passed. Ruff,
formatting, service/workflow YAML and translation JSON validated. Independent
read-only review found no concrete blockers; no installed-HA field test claimed.

Uppercase-status follow-up: new unit and HA regressions reproduced the missing
enums before the allowlist fix; 345 unit and 268 HA tests pass afterwards.
