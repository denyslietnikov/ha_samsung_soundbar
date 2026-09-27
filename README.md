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
| Local only | Available for existing entries | Required only for the initial entry creation | Local JSON-RPC only | Select in Options after the soundbar has been added; no SmartThings requests during setup, reload or operation in this mode. |

An existing entry can switch to **Local only** in Options after configuring a
working local host. Its existing device and entity IDs are retained. OAuth-free
first-time setup and removal of stored OAuth credentials are not implemented
yet; do not delete the entry to switch modes. Cloud-only switches and artwork
are not available in Local-only mode.

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

## Limitations

- The SmartThings mobile app can use private Samsung APIs that are not
  available to this integration through public OAuth.
- A command returning `COMPLETED` is not sufficient to create a stateful Home
  Assistant entity; the integration also requires reliable state readback.
- SmartThings Cloud features require valid OAuth credentials and remain subject
  to Samsung API availability and policy.

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
