# AirPlay Text Metadata Implementation Plan

**Goal:** Show locally confirmed title/artist/album in the existing HA media player.
**Spec:** Q800F_FEATURES_IMPLEMENTATION_PLAN.md section3 and the owner's explicit
go-ahead after play/pause/resume/disconnect probes on 2026-11-07.

**Design:** Reuse the one coordinator and authenticated OCF client. Add a 5-second
text GET tier for Q800F + fresh RPC power on/WIFI/AirPlay only; keep audio settings
at 30 seconds, RPC core at 2 seconds. Use the same owned task, queue and shutdown.
Three text fields have OCF ownership and 15-second TTL in SoundbarState, not a
second cache or per-entity polling. A context-generation token discards a read
that crosses source/power/outage/command transitions, including A->B->A.

**Non-goals:** No new entities, Artwork, URLs, duration, position, playback state,
playback commands, device writes, live probes, deployment or commits. Previous
availability/last-value proposal was cancelled and remains untouched.

## Interfaces and Boundaries
- New ocf_track.py: TRACK_HREF, MEDIA_TEXT_FIELDS, MEDIA_TEXT_TTL=15.0,
  MEDIA_POLL_INTERVAL=5.0, project_track_text(payload)->dict. Exact strings only,
  trim edges, <=1024 chars, reject control/surrogate characters. A valid nonempty
  title is required; artist/album are optional. Full snapshots clear omitted,
  empty and malformed fields; no mixed old/new metadata.
- State: media_context()->int|None checks fresh RPC on/WIFI/AirPlay, configured
  healthy OCF and no pending context writes; clear_media() invalidates the three
  fields and advances generation. RPC input/power/detail changes or stale recovery,
  context invalidation and begin_write of context fields clear text. resolve(text)
  requires valid context. RPC must never own these fields or provide fallback.
- Coordinator: media_text_value(field) is pure and model/client/context gated.
  One _ocf_task schedules independent audio and text due times. Capture context
  before GET, verify same context/client/model/health afterwards, then atomically
  replace text. Failures, cancellation, detach, off/source change clear text.
- Facade: media_title/media_artist/media_album_name delegate to coordinator.
  MediaPlayer properties expose these without extra feature flags or state changes.
- Privacy: only normal HA media attributes contain track text. OCF diagnostics,
  discovery resolved data and support dumps exclude it. No raw text logs/errors,
  no URL reads, no Store persistence of text, no secret changes.

## Tasks
- [x] TDD pure text projection and state ownership/context/TTL/generation contracts.
- [x] TDD coordinator scheduling, context-race rejection and independent errors.
- [x] Facade/native HA attributes and redacted diagnostics; preserve entity IDs.
- [x] Real HA tests: text/track changes/pause/disconnect/off/reload, partial data,
      failed/stale/delayed reads, A->B->A, local-only, key forget, diagnostic privacy.
- [x] Independent review, full unit/HA suites, lint and metadata validation;
      update README/main roadmap while preserving prior uncommitted fixes.

## Verification
- Offline unit suite: 373 passed; real-HA harness: 324 passed, network blocked.
- Ruff check/format and JSON/YAML validation passed.
- Review regressions reproduced before fixes: eager task reentrancy, pending
  context-command publication and cancellation during audio GET/lock wait/queued
  volume command. RPC core, queue ownership and audio polling remain intact.
- No hardware reads/writes or deployment in this implementation. Installed HA
  field-test of native text remains the next step in the main roadmap.

## Review Focus
- In-flight reads must not repopulate text after context reset or key removal.
- A metadata-only sweep must not postpone the 30-second audio-settings sweep.
- Diagnostic media reads remain ephemeral and cannot inject UI text.
- Missing artist/album must clear old values, not refresh their TTL.
- No title leaks through retrieve_data/discovery or newly extended OCF_FIELDS.
