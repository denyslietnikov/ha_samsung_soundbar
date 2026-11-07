# Samsung Soundbar for Home Assistant

Local Home Assistant integration focused on Samsung HW-Q800F. Both modes use
LAN JSON-RPC for power, volume, mute, input and sound mode. Hybrid adds
authenticated local OCF audio controls when a matching credential and validated
readback are available. No account sign-in or remote API is used by the integration.

## Requirements

- Home Assistant 2026.9.4 or newer.
- Soundbar reachable from Home Assistant with IP Control enabled.
- Local JSON-RPC HTTPS port 1516; stable physical identity metadata on port
  8001 or 9110 is required when binding a host.
- For OCF audio settings: a previously saved credential for that soundbar and
  access to its local DTLS endpoint.

The local OCF dependencies are
`smartthings-local==0.1.24` and `cbor2>=6.2.0.post2`.

## Control Modes

| Mode | Stored value | Behavior |
| --- | --- | --- |
| Local only (RPC) | `local_only` | RPC only. A saved OCF credential is retained but is not used. |
| Hybrid Local + OCF | `hybrid_local_ocf` | RPC plus authenticated OCF audio readback and profile-validated controls. |

**Hybrid without a credential is RPC operation with a warning, not working OCF.**
Import your saved credential through Options to enable OCF. Missing, invalid or
offline OCF access does not block RPC startup. OCF controls are discovered only
after their model-scoped profile and typed local readback have been validated;
an unavailable setting is never presented as a confirmed Off.

## Installation and Setup

1. Add `https://github.com/denyslietnikov/ha_samsung_soundbar` to HACS as an
   Integration repository, install Samsung Soundbar, and restart Home Assistant.
   For manual installation, place `custom_components/samsung_soundbar` at the
   matching path in your Home Assistant configuration directory.
2. Add Samsung Soundbar in **Settings -> Devices & services**.
3. Select **Local only (RPC)** or **Hybrid Local + OCF** and configure the local
   host, RPC port, timeout and certificate verification. Samsung's local
   certificate normally requires verification to be disabled.
4. Home Assistant verifies RPC and the physical identity before saving the host.
   `getIdentifier` is model information, not a unique device identity.

The local RPC access token is created at runtime. Do not copy it into configuration
or submit it in a support request.

## Existing Entries

Back up Home Assistant before updating. Keep the existing entry. Migration retains its device ID, entity unique IDs,
entity IDs, custom names and history, and preserves its separate OCF credential
Store. No replacement entry is created. Obsolete authorization and fallback
settings are removed; a saved OCF credential is retained even in RPC-only mode.

A legacy entry without a usable local host or confirmed physical identity needs local reconfiguration. Follow
the **Local soundbar configuration required** Repair or open the existing entry's
Reconfigure/Options. The integration does not guess an address or delete the entry.
A host change is checked against saved physical identity; mismatches are rejected.

The obsolete EQ Preset select and former Woofer +/- buttons are removed only
from the owning entry's exact registry rows. Other entities and custom names
are not changed. Dashboards and automations using removed controls must be
updated manually; use the Woofer number instead of the relative buttons.

### DHCP Address Recovery

For a saved Wi-Fi MAC, matching Home Assistant DHCP discovery can verify a new
address against stable identity metadata on ports 8001/9110 and update the existing
entry. Contradictory identities are rejected; incomplete metadata does not erase
known identifiers. Discovery never creates or merges entries. Unmatched devices
are not probed. A DHCP reservation remains recommended; real address changes
still need field-testing.

## Controls and Readback

| Control | Local only (RPC) | Hybrid Local + OCF |
| --- | --- | --- |
| Power, volume, mute | RPC with local readback | Same RPC route |
| Source: Input, Sound: Mode | RPC capability-dependent | Same RPC route |
| Volume and Sound From sensors | Local values when available | Same local values |
| Tone: Woofer | Not exposed | Validated Q800F OCF profile, -12 to +6 dB, step 1 |
| Tone: Bass / Treble | Not exposed | Independently validated OCF profiles, -6 to +6, step 1 |
| Sound: Night Mode, Bass Boost, Voice Amplifier, Virtual, Moderate Bass, SpaceFit Sound | Not exposed | Discovered independently after validated OCF readback |
| EQ Preset / seven-band EQ UI | Not exposed | Not exposed; backend diagnostics only |

Advanced audio controls are discovered at startup or later when local profiles
become available. Their existing IDs are reused. Once discovered, an outage makes
them unavailable instead of removing/recreating them. A missing field does not
disable its neighbors. Names use the native translated Sound/Source/Tone groups;
user-customized names take precedence.

OCF writes run through guarded device methods: check fresh RPC power and target
readback inside the shared command queue, send one typed property, then require
matching readback. They do not turn on the soundbar, silently change Sound Mode
or replay ambiguous writes through another transport. Delayed acknowledgments
allow bounded read-only convergence checks, not repeated writes.
Voice Amplifier is Voice Enhancement, not Active Voice Amplifier.

Q800F/1103.1 protocol probes previously confirmed Woofer samples -12/-7/-6/+6,
Bass and Treble samples -6/-1/+1/+6 in Surround, and individual audio-switch
write/readback. Those are bounded protocol results, not proof of every mode,
firmware or installed-HA UI behavior. Field verification remains necessary.

Playback play/pause/stop/next/previous, individual speaker channel levels, rear
speaker mode and Active Voice Amplifier controls are removed. Their integration
actions are no longer registered. Media title, artist, duration, position and
artwork remain unknown (`None`); no placeholder artwork or invented metadata
is published. Available local model/manufacturer/firmware metadata is used
without overwriting existing registry metadata with unknown values.

The retained actions are `select_soundmode`, `set_woofer_level`,
`set_night_mode`, `set_bass_enhancer`, `set_voice_enhancer` and
`set_space_fit_sound`, targeting the media player. They use the same guarded
device methods as native controls. Unsupported local actions fail rather than
being sent elsewhere.

### EQ Diagnostics

Local EQ metadata does not create a select. The guarded backend retains reported
`NONE`, `POP`, `JAZZ`, `CLASSIC`, `CUSTOM` presets for diagnostics only.
Preset protocol write/readback was observed, but useful app/DSP behavior remains
unconfirmed. Diagnostic prerequisites include fresh RPC power and Standard sound
mode, Bass Boost off, matching OCF context and supported options. No prerequisite
is changed implicitly. EQ band writes/sliders remain disabled: an ACK without a
changed curve is not evidence of working band control.

## Local OCF Credentials

Choose **Import or replace credential** in the existing entry's Options, then
paste your complete `q800f.ocf-credential.json` backup into the masked local
field. Import verifies device identity and DTLS authentication before saving.
Selecting import/forget does not save unrelated edits in that form. A successful import selects **Hybrid Local + OCF** for this entry. Audio
controls still require matching profiles and fresh readback; saving a credential
alone does not prove that every audio setting works.

The PSK and client/device UUIDs are stored separately in
`.storage/samsung_soundbar.ocf.<entry_id>` using atomic HA storage and private
`0600` file permissions, never in entry options, entity state or diagnostics.
This is **not encryption**. Keep an independent encrypted backup and the HA
backup emergency kit. A temporary private rollback Store protects replacement;
an interrupted transaction is recovered on the next load.

Credentials survive reload, restart and mode changes. **Forget local OCF
credential** requires confirmation and removes only this entry's HA copy. It
does not reset the soundbar or revoke its key. Back up first; reimport needs
your saved bundle. Deleting the integration entry removes its Store.

Missing or invalid credentials produce local Repairs that open import in the
existing entry. Working RPC remains available. OCF reads run in the background,
not as a startup requirement. Each sweep has bounded reads and 30 seconds between
completed sweeps; per-field OCF freshness expires after 120 seconds. A successful
ACK alone is not state readback.

### Already Copied the Hashed Q800F Store?

Use `scripts/recover_q800f_credential.py` directly with the copied file; no
renaming, new backup, Manifest lookup or manual UUID input is needed. This
launcher requires Python 3.11+ (tested with 3.14). It reads the live Q800F UUID,
selects that device's credential and checks
PSK authentication against the protected `/oic/d` identity. Only then can it
export. The host must be a numeric LAN IP; keep the soundbar reachable.

Run these commands from the repository root. `.venv-ocf` is a dedicated local
environment for the recovery tools, separate from Home Assistant's environment.

```sh
python3 -m venv .venv-ocf
.venv-ocf/bin/python -m pip install -r scripts/requirements-ocf-recovery.txt
.venv-ocf/bin/python scripts/recover_q800f_credential.py \
  --store /path/to/copied/f838d92eedf51cc435481f58169d8288cc505895 \
  --host 192.0.2.26
```

Without `--output`, it prints only whether the Q800F credential authenticated.
To save a portable file explicitly, append
`--output "$HOME/q800f.ocf-credential.json"`. The same private, atomic,
no-overwrite export is used. **The output is plaintext and needs external
encryption before archiving.** No device settings or ownership are changed.

### Find the Store in a Completed iPhone Backup

Use a completed, unencrypted Finder backup from an iPhone whose SmartThings app
already controls the soundbar. This route was verified on one Q800F; another
phone/app version or an encrypted backup may have a different outcome. Never
upload a whole phone backup to HA, a web extractor, an issue or an AI chat.

The backup index identifies the file by:

- domain: `AppDomain-com.samsung.oneconnect4ios`
- relative path: `Library/Security/oic_svr_db_client.dat`

Query a **static copy** of `Manifest.db`, replacing the path below:

```sql
SELECT fileID, substr(fileID, 1, 2) || '/' || fileID AS backup_path
FROM Files
WHERE domain = 'AppDomain-com.samsung.oneconnect4ios'
  AND relativePath = 'Library/Security/oic_svr_db_client.dat';
```

For example, run `sqlite3 -readonly
'file:/path/to/copied/Manifest.db?mode=ro&immutable=1'` and enter the query.
The result names the hashed file to copy from the original backup's two-character
subdirectory. `immutable=1` is only for a completed, unchanged snapshot: it does
not bypass macOS privacy permissions, decrypt a backup or recover missing WAL
transactions. If macOS denies access, use Finder to copy the completed files
or explicitly grant your terminal Full Disk Access; do not modify the original
backup's permissions. Keep the copied store private: it can contain credentials
for other devices as well.

You also need the soundbar's **OCF device UUID**, obtained from a trusted
read-only `/oic/d` probe. It is not the integration's saved device ID, the Tizen
DUID or the client/owner UUID. This offline tool deliberately does not discover
the device or print any UUIDs.

### Validate or Export Only the Selected Device

Run from the repository root on macOS/Linux with Python 3.10+ (tested with
Python 3.14). Install the separate tool dependency, not the OCF runtime in HA:

```sh
python3 -m venv .venv-ocf
.venv-ocf/bin/python -m pip install -r scripts/requirements-ocf.txt
.venv-ocf/bin/python scripts/extract_ocf_credential.py \
  --store /path/to/copied/oic_svr_db_client.dat \
  --target-device-uuid YOUR_OCF_DEVICE_UUID
```

Without `--output`, this only validates structure and exact-device matching in
memory. It does not save or print the PSK or identities. Exactly one matching
`credtype=1` record is required. The client identity comes from `cred.rowneruuid`,
not the selected device UUID. Raw/base64 PSKs of 16 or 32 bytes are supported.

To explicitly export, first create a private directory **outside the repository**:

```sh
mkdir -m 700 "$HOME/q800f-recovery"
.venv-ocf/bin/python scripts/extract_ocf_credential.py \
  --store /path/to/copied/oic_svr_db_client.dat \
  --target-device-uuid YOUR_OCF_DEVICE_UUID \
  --output "$HOME/q800f-recovery/q800f.ocf-credential.json"
```

The new file has permissions `0600`. A private temporary directory is used until
the complete file is ready; atomic publication requires a local filesystem with
hard-link support and fails safely if unsupported. Existing files and symlinks
are never overwritten. Only schema version, target device UUID, client identity UUID,
encoding and PSK are exported; other device records are excluded. A previously
exported bundle can be validated offline without decoding the original store:

```sh
.venv-ocf/bin/python scripts/extract_ocf_credential.py \
  --credential "$HOME/q800f-recovery/q800f.ocf-credential.json" \
  --target-device-uuid YOUR_OCF_DEVICE_UUID
```

**This JSON is plaintext.** Permissions and base64 are not encryption. Never
print the file, paste it into service YAML/chat, pass the PSK through command-line
arguments/environment variables, or commit it. The repository ignores
`ocf-secrets/`, `*.ocf-credential.json`, `oic_svr_db_client.dat` and `Manifest.db*`,
but a differently named file can still be accidentally committed.

### Preserve Access Before Updating or Resetting

- Put the selected credential bundle and the original completed backup/store
  into encrypted storage. The full store is more sensitive than the one-device
  bundle. Keep two independent encrypted copies, including one offline, and keep
  their password separately. Do not leave the only copy in a temporary directory.
- Preserve **PSK + client identity UUID + target OCF UUID + schema version**;
  the PSK alone is insufficient. Keep a known-good copy of the extraction script
  and `scripts/requirements-ocf.txt`. Validate a restored bundle offline; a
  separate, explicitly authorized read-only device probe is required to confirm
  authentication. This step does not create an encrypted backup automatically.
- Do not reset, remove/re-add or transfer ownership of the soundbar just to
  troubleshoot. These operations may invalidate the credential. Preserve working
  firmware/app versions in your notes; updates may change the protocol or access.
  A phone backup is not a guarantee that an old App Store app can be reinstalled.
- Include the HA credential Store in an encrypted HA
  backup and keep the [backup emergency kit](https://www.home-assistant.io/more-info/backup-emergency-kit/)
  outside HA. A saved PSK cannot restore a device's ACL or guarantee access after
  a reset or incompatible firmware update.

## Diagnostics

`samsung_soundbar.dump_ocf_status` reads a fixed allowlist of local audio
resources in Hybrid with a valid credential. Its optional `hrefs: []` returns
cached diagnostics without new reads. The result contains projected values,
source/age/TTL and errors, never raw OCF responses, PSK or identity UUIDs.
Use a Home Assistant `device_id` when multiple soundbars are loaded.

`samsung_soundbar.dump_discovery_snapshot` returns a safe local coordinator
snapshot. Remote execute/status diagnostic actions have been removed.

`samsung_soundbar.dump_local_rpc` probes the configured LAN endpoint directly:

```yaml
action: samsung_soundbar.dump_local_rpc
data:
  host: 192.0.2.26
```

Optional write probes change settings and must be requested explicitly.
Commands are not automatically repeated after ambiguous write failures.
Do not publish tokens, credentials, UUIDs or private network details in reports.

## Development Tests

Use Python 3.14 in a separate environment:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m pytest tests/ha -q
```

The pinned HA harness uses real registries, platforms, services and flows with
mocked LAN transports and blocked network access. Tests cover local migration,
entry/identity preservation, discovery, availability and guarded readback.
They do not replace physical-device or installed-HA field tests.

## Attribution

This project retains code and design foundations from the following MIT
licensed Samsung soundbar integrations:

- [samuelspagl/ha_samsung_soundbar](https://github.com/samuelspagl/ha_samsung_soundbar)
- [LEOBOESE/ha_samsung_soundbar](https://github.com/LEOBOESE/ha_samsung_soundbar)
- [ZtF/hass-samsung-soundbar-local](https://github.com/ZtF/hass-samsung-soundbar-local)

Original contributors include Samuel Spagl, Piotr Machowski and
Thierry Bourbon. Their attribution and the repository's MIT license are
preserved.

## License

This project is licensed under the [MIT License](LICENSE).
