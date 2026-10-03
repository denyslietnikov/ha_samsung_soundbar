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

- Home Assistant `2026.6.1` or newer.
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

Direct-control candidates can be tested explicitly, for example:

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
write changed anything. These candidates are **not enabled in runtime**;
verified Q800F read/write/readback is required before adopting a model profile.

Bare JSON-RPC errors are reported with their code, including `-32700`. A parse
error alone is not treated as authentication rejection or automatically retried
as a write. Explicit token rejection still gets one authentication retry.
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
and Local-only migration without changing IDs. They do not replace field-tests
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
