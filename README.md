# Samsung Soundbar for Home Assistant

Home Assistant custom integration for Samsung soundbars. It combines the
public SmartThings API with confirmed local JSON-RPC control where the
soundbar supports it.

The project is developed independently and currently focuses on reliable
operation with recent Home Assistant releases and Samsung HW-Q800F.

## Highlights

- SmartThings OAuth setup with access-token refresh and reauthentication.
- Media player control for power, volume, mute, playback, source and sound
  mode when the device exposes the required capability.
- Hybrid LAN control for confirmed Q800F media controls.
- Optional soundbar entities for advanced audio, EQ and woofer control where
  the device profile supports them.
- Diagnostics for SmartThings status, execute payloads and local JSON-RPC.

## Requirements

- Home Assistant `2026.9.4` or newer.
- Integration version `0.7.0b63` uses `pysmartthings 4.0.3`.
- SmartThings OAuth-In application for SmartThings Cloud or Hybrid mode.
- For Hybrid or Local-only mode: the soundbar reachable on the local network
  with IP Control enabled.

## Control Modes

| Mode | Status | Account and OAuth | Primary state and controls | Q800F notes |
| --- | --- | --- | --- | --- |
| SmartThings Cloud | Available | Required | Public SmartThings capabilities | Source and sound-mode write support depends on the device profile. |
| Hybrid Local + SmartThings Cloud | Available | Required | Local JSON-RPC for media controls; SmartThings for setup, fallback and cloud-only features | Recommended for Q800F. |
| Local only | Available | Not required | Local JSON-RPC only | Select during initial setup or in Options; no SmartThings requests during setup, reload or operation in this mode. |

An existing entry can switch to **Local only** in Options after configuring a
working local host. Its existing device and entity IDs are retained. New
Local-only entries skip OAuth. Switching an existing entry to Local-only
requires local identity confirmation and removes its stored OAuth credentials;
returning to a cloud mode requires authorization again. Do not delete the entry
to switch modes. Cloud-only switches and artwork are not available in Local-only
mode.

## Installation

### HACS

1. In HACS, open **Integrations** and add a custom repository.
2. Use `https://github.com/denyslietnikov/ha_samsung_soundbar` with category
   **Integration**.
3. Install **Samsung Soundbar** and restart Home Assistant.
4. Add **Samsung Soundbar** from **Settings -> Devices & services**.

### Manual installation

Copy `custom_components/samsung_soundbar` to the same path in your Home
Assistant configuration directory, restart Home Assistant, then add the
integration from the UI.

## SmartThings Cloud Setup

Create a SmartThings OAuth-In application and register the resulting Client ID
and Client Secret in Home Assistant Application Credentials when prompted.

Configure this exact redirect URI in the SmartThings application:

```text
https://my.home-assistant.io/redirect/oauth
```

Request these scopes:

- `r:devices:*`
- `x:devices:*`
- `r:locations:*`

Do not request `sse`. SmartThings does not grant that privileged scope to
user-created OAuth-In applications, so this integration uses polling.

After credentials are configured, add the integration, complete Samsung sign
in, and select the soundbar.

## Q800F Hybrid Setup

Hybrid mode uses SmartThings OAuth for device discovery and optional cloud
features, while the following media controls use local JSON-RPC over LAN:

- power
- volume and mute
- input source
- sound mode
- codec and streaming-source readback

Q800F also exposes local **Woofer +** and **Woofer -** buttons in Hybrid and
Local-only modes. They adjust the subwoofer relatively; no absolute level is
inferred. The buttons require an available LAN connection and an on-state.
Volume and mute prefer `volumeControl` / `muteControl`, with legacy fallbacks
when the device reports that a method is unsupported. Power commands require
matching local readback rather than only a success acknowledgment.

After adding the integration, open its options and select **Hybrid Local +
SmartThings Cloud**. Configure:

- local soundbar host/IP;
- local RPC port: `1516`;
- local SSL verification: normally off for Samsung's local certificate;
- local RPC timeout;
- optional SmartThings Cloud fallback.

The local AccessToken is created by the soundbar at runtime. It is not the
SmartThings OAuth token and is not stored in the Home Assistant configuration.

For Q800F, Hybrid mode is the recommended currently available mode. It gives
reliable local readback for `Input Preset`, `Sound Mode`, volume, mute and
streaming source labels such as AirPlay, Google Cast and Roon.

### DHCP Address Recovery

Local-only and Hybrid entries can recover a changed IPv4 address when Home
Assistant DHCP discovery sees the soundbar. Discovery matches the confirmed
Q800F MAC prefix `94:E6:BA` or hostnames starting with `tizen` or `soundbar`.
Only events matching an entry's saved `wifiMac` trigger a read-only identity
check on ports `8001`/`9110`; unrelated devices are not probed.

The new address is accepted only if a saved MAC/UUID matches the metadata and
no shared identifier contradicts it. The existing entry reloads with the new
host, retaining its device/entity IDs, history, OAuth credentials and other
options. Partial metadata does not erase previously saved identifiers. DHCP
discovery never creates an entry or merges devices through MAC connections.

For older Hybrid entries, save Options once with the working local host to
record its identity. If metadata is unavailable, Hybrid can keep working but
automatic recovery needs a saved MAC. UUID-only entries, other MAC prefixes
with unmatched hostnames, and networks where HA cannot observe DHCP discovery
still need a manual host change in Options. Failed identity checks leave the
old address unchanged; a later DHCP event can retry. A DHCP reservation remains
recommended. Actual router/device address changes still need field-testing.

## Optional OCF Credential Recovery

Q800F research has confirmed authenticated local OCF access and Woofer
read/write/readback. **OCF credential import and storage are available in the
integration options. OCF-backed polling and additional controls are a later
stage; existing RPC controls are unchanged.** The offline extractor below only validates or explicitly exports
an existing credential from a selected local file. It makes no network requests
and cannot prove that the credential still authenticates. The optional Q800F
launcher adds read-only LAN authentication. Neither tool pairs/resets the soundbar.
Do not delete your existing integration or remove the soundbar from SmartThings.

### Import the Saved Credential into Home Assistant

1. Install the updated integration and restart HA. HA installs the pinned
   `smartthings-local` dependency automatically; do not install the tool venv in HA.
2. In the existing soundbar entry, save **Local only** or **Hybrid** with the
   working host first. Older Hybrid entries should save Options once to record
   the physical device identity. Do not remove/re-add the soundbar.
3. Reopen Options, select **Import or replace credential** under **Local OCF
   credential**, then submit. This action does not save other changes in that form.
4. Paste the full contents of `q800f.ocf-credential.json` into the masked field,
   locally in HA, and submit. Never paste the credential into chat or an issue.
   Identity and DTLS authentication are checked without changing soundbar settings.

The PSK and both UUIDs are stored in a private, atomic HA Store under
`.storage/samsung_soundbar.ocf.<entry_id>` with file permissions `0600`, not in
config-entry data, Options, entity state or diagnostic output. This is **not
encryption**: anyone with filesystem/root access to HA can read it. Keep your
independent encrypted backup and HA backup emergency kit.

Replacement is serialized with other imports and removal. A private `.rollback`
Store temporarily retains the previous credential until a disk-verified commit;
failed or cancelled writes restore it, and an interrupted transaction is recovered
on the next load. The recovery record is removed after a verified commit/recovery.
No plaintext credential export service is provided.

Credentials survive reload, restart, Cloud reauth and mode changes. Pure Cloud
mode retains but does not use them. Optional OCF starts lazily, so an offline
soundbar or invalid credential does not block working RPC controls. Local OCF
Repairs offer import, not Samsung OAuth reauthentication. If a Store is damaged,
back up your credential, use **Forget local credential**, then import again.
Forget requires confirmation and removes only this entry's HA copy; it does not
reset/revoke the soundbar's key. Deleting the integration entry removes its Store.

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
read-only `/oic/d` probe. It is not the SmartThings cloud device ID, the Tizen
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
- Once HA credential storage is implemented, include it in an encrypted HA
  backup and keep the [backup emergency kit](https://www.home-assistant.io/more-info/backup-emergency-kit/)
  outside HA. A saved PSK cannot restore a device's ACL or guarantee access after
  a reset or incompatible firmware update.

## Feature Availability

Features are created only when the device exposes the corresponding capability
or confirmed local transport support. Exact availability varies by model and
firmware.

| Feature | SmartThings Cloud | Hybrid Q800F |
| --- | --- | --- |
| Power, volume and mute | Available when publicly exposed | Local first with Cloud fallback |
| Input Preset | Read-only for Q800F | Writable through local RPC |
| Sound Mode | Capability-dependent | Writable with local readback |
| Sound From | SmartThings status when available | Local streaming-source mapping |
| Bass Mode, Night Mode, Voice Amplifier, Virtual | Optional optimistic cloud controls | Same cloud-backed behavior |
| Woofer, EQ and other advanced controls | Device/profile-dependent | Device/profile-dependent |
| Album artwork | Depends on media metadata supplied by SmartThings | No confirmed local artwork source |

## Diagnostics

Cloud problems appear under **Settings > System > Repairs**, separately for
each soundbar. Authorization failures offer **Reconnect SmartThings account**;
access denied (403), payment required (402), quota/rate limit (429), and repeated
temporary outages have their own messages. A generic 403 is not proof that a
subscription is required, and these non-auth failures do not trigger reauth.

**Open control mode options** opens the existing entry's Options. Switching to
Local only verifies the host and saved identity before removing OAuth data;
device and entity IDs are retained. Opening or cancelling a flow does not
resolve the issue: it clears after successful Cloud recovery, switching to
Local only, or deleting that entry. Reachable LAN controls in Hybrid continue
to work during a Cloud failure; cloud-only controls become unavailable.

Cloud polling backs off for access failures and respects numeric `Retry-After`
on 429 (15-3600 seconds; 60 seconds if absent or invalid). Forced coordinator
refreshes also respect this cooldown. A 401 gets at most one token refresh
and retry per request. Routine token rotation does not reload the integration.

Each entry uses one shared Home Assistant state coordinator. Local core state
is polled every 2 seconds; cloud state every 15 seconds; full local status every
60 seconds. Entities do not run their own network polls. Fresh local fields take
priority in Hybrid, while partial responses retain each field's own age and TTL.
Commands are serialized and followed by targeted local readback. A short write
settling guard prevents stale replies from immediately undoing a successful command.

The integration provides diagnostic actions under the `samsung_soundbar`
domain. They are intended for investigating capability differences between
models and firmware versions.

Example local JSON-RPC probe:

```yaml
action: samsung_soundbar.dump_local_rpc
data:
  host: 192.168.0.10
```

The result includes power, volume, mute, input source, sound mode, codec and
local connection errors. Do not publish diagnostic output containing tokens or
private network details.

`dump_discovery_snapshot` includes coordinator field sources, ages, TTLs and
pending writes. It also works in Local-only without issuing SmartThings requests;
cloud-only diagnostic sections are omitted in that mode. Advanced Audio switches
remain optimistic when the device does not provide readable cloud state.

Cloud status respects explicit `custom.disabledComponents` and
`custom.disabledCapabilities` lists. Missing/null optional metadata does not
disable existing features. Disabled Cloud features do not disable LAN controls
in Hybrid; their Cloud commands are blocked and existing controls become
unavailable rather than deleting entity-registry entries.

Direct controls can also be tested explicitly, for example:

```yaml
action: samsung_soundbar.dump_local_rpc
data:
  host: 192.168.0.10
  write_method: volumeControl
  write_params:
    volume: 8
```

This changes the volume to the requested integer (0-100). For a mute probe use
`write_method: muteControl` and `write_params: {mute: true}` (a boolean).
The service automatically includes `getVolume`/`getMute` before and after
the write and returns `write_verification` with `readback_matches` and
`state_change_observed`. Matching an unchanged value does not prove that the
write changed anything. A diagnostic probe does not change the integration's
runtime configuration. Direct volume/mute and relative Woofer keys have been
verified on Q800F; this does not establish support for every other model.

Bare JSON-RPC errors are reported with their code, including `-32700`. A parse
error alone is not automatically retried as a write. Known read-only requests
can refresh a rejected token and retry once, including Q800F's `-32700` response.
Relative remote-key commands are never automatically replayed. Failed individual
fields are exposed in diagnostic `local.field_errors` without refreshing their
cached timestamps; transport/auth failures still mark the LAN connection down.
The model-only `getIdentifier` is cached in memory after two matching reads;
explicit diagnostic calls still read it directly. It is never a device identity.

## Limitations

- The SmartThings mobile app can use private Samsung APIs that are not
  available to this integration through public OAuth.
- A command returning `COMPLETED` is not sufficient to create a stateful Home
  Assistant entity; the integration also requires reliable state readback.
- SmartThings Cloud features require valid OAuth credentials and remain subject
  to Samsung API availability and policy.

## Development Tests

Use Python 3.14 in a separate virtual environment:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m pytest tests/ha -q
```

The pinned HA harness runs Home Assistant 2026.9.4. Tests in `tests/ha` use the
real config-entry manager, entity/device registries, platforms, service calls,
Repairs, Options and reauth flows. Only external OAuth, SmartThings and LAN
transports are replaced; network access is blocked. They cover setup,
reload/unload, external state updates, readback, DHCP recovery, Cloud failures,
and Local-only migration without changing IDs. API compatibility tests also
cover successful reauth with changed or unchanged credentials, entry-scoped
runtime/client isolation, scoped device lookup, OAuth retry classification, and
entity actions registered before platform setup and retained after unload.
They do not replace field-tests
on a Q800F. GitHub Actions runs both suites on pushes and pull requests.

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
