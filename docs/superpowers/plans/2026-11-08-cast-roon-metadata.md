# Cast and Native Roon Metadata Implementation Plan

**Goal:** Extend verified Q800F OCF text metadata to native Roon, not Google Cast.
**Spec:** Q800F_FEATURES_IMPLEMENTATION_PLAN.md section3 and the owner's request
to implement Cast/Roon metadata, with read-only source-correlated probes.

## Evidence and Scope
- Roon -> Cast: RPC WIFI_GOOGLE, Google Cast detail; two authenticated GETs have
  empty title/artist/album/thumbnail/trackUri, duration0. musicinfo contains only
  controls/provider, not hidden text. Other HA Cast entity has text via another
  protocol; do not bind/copy its cached attributes into this integration.
- Native Roon: RPC CD, Roon detail; two GETs have title/artist/album. Pause gives
  exact PAUSED and modes=[pause], retains text, position0. On selecting TV ARC/eARC,
  RPC leaves Roon but OCF retains the old text. Never trust text presence alone.
- Changes stay in the existing state/context gate and diagnostics. No new client,
  dependency, coordinator, cloud, playback writes, inferred player state, URLs,
  artwork, duration or position. No deploy/commit or device writes by the agent.

## Tasks
- [x] RED: state tests for native Roon ownership/TTL, provider transitions, stale
  Roon text on Cast and invalid detail types; HA tests for both providers,
  pause/type/privacy, delayed cross-provider and roundtrip responses.
- [x] GREEN: make MEDIA_CONTEXT values tuples of exact allowed values: power on,
  input WIFI, detail AirPlay or Roon. Preserve keys, actual-observation generation,
  pending-write guards, TTL15s and single owned OCF task cadence5s/settings30s/RPC2s.
- [x] RED/GREEN: add exact observed PAUSED diagnostic enum, never case-fold unknown
  strings or derive device/playback state from the diagnostic value.
- [x] Full unit/HA suites and lint; independent read-only review; update README
  and main roadmap. Transfer only with baseline-hash checks and backup, preserving
  existing dirty changes.

## Verification
- 376 unit tests and 332 real-HA tests passed with network blocked; Ruff check
  and format passed. New Roon/PAUSED tests were observed failing before the fix.
- Independent read-only review found no actionable issues. Hardware observations
  above were GET-only; all playback/source changes were performed by the owner.
- Installed HA Roon metadata after this extension has not been field-tested.

## Review Focus
- Leaving Roon for TV/Cast must hide retained device text; Cast is not approved.
- AirPlay -> Roon and Roon -> AirPlay, including A->B->A, revoke in-flight reads
  even though both ends are eligible and the normalized input remains WIFI.
- Partial/malformed/expired text must not retain old artist/album/type.
- Keep native music content type, no extra playback features or title concatenation.
- User-facing text must not leak into diagnostics; paused metadata must not change
  RPC power ownership or open Store/DTLS in local-only mode.
