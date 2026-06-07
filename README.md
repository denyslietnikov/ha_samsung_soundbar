# YASSI: Yet Another Samsung Soundbar Integration (Home Assistant)

A maintained fork of YASSI focused on compatibility with newer Home Assistant versions while preserving the original functionality and feature set.

This integration provides advanced Samsung Soundbar control through SmartThings, including sound modes, equalizer settings, subwoofer control, advanced audio enhancements, and media player features.

## Fork Goals

This fork mainly focuses on:

- Home Assistant 2026.x compatibility
- Fixing deprecated API usage
- Restoring OAuth/config flow support without SmartThings PATs
- Improving long-term maintainability
- Preserving compatibility with modern pysmartthings versions

## OAuth Beta Setup

Beta `0.7.0b33` targets Home Assistant `2026.1.0` or newer.

This fork uses SmartThings OAuth instead of a Personal Access Token. Before adding the integration, create a SmartThings OAuth-In application and add its `client_id` and `client_secret` in Home Assistant under Application Credentials for the `Samsung Soundbar` integration.

Use these scopes for the first beta:

- `r:devices:*`
- `x:devices:*`
- `r:locations:*`

When SmartThings asks for a redirect URI, add this exact value, with no trailing slash:

```text
https://my.home-assistant.io/redirect/oauth
```

If SmartThings shows `'redirect_uri' could not be validated`, the OAuth-In App does not contain the exact redirect URI above. Update or recreate the SmartThings OAuth-In App, then retry the Home Assistant flow.

After credentials are saved, add the integration from the Home Assistant UI, sign in with Samsung, and select the soundbar device.

## Q800F Hybrid Mode

For Samsung HW-Q800F and compatible 2024/2025 soundbars, the recommended beta mode is `Hybrid Local + SmartThings`.

SmartThings OAuth is still used for account/device setup, fallback, and legacy advanced audio switches. Local control uses the soundbar JSON-RPC API over LAN for media-player state and commands:

- power
- volume
- mute
- input source
- sound mode
- codec readback

To enable it, open the integration options in Home Assistant and set:

- Control mode: `hybrid_local_smartthings`
- Local soundbar host/IP: the soundbar IP address, for example `192.168.88.26`
- Local RPC port: `1516`
- Verify local SSL certificate: off
- Fallback to SmartThings Cloud: on

The soundbar must be added to SmartThings, connected to Wi-Fi, and have IP Control enabled in the SmartThings mobile app. The local AccessToken is created by the soundbar at runtime and is not stored in Home Assistant config.

Diagnostic action:

```yaml
action: samsung_soundbar.dump_local_rpc
data:
  host: 192.168.88.26
```

## Features

- UI-based setup through Home Assistant
- SmartThings OAuth with automatic access-token refresh
- Optional Hybrid Local + SmartThings mode for Q800F media controls
- Media player controls
- Sound mode selection
- Equalizer & subwoofer controls
- Night mode & voice amplification switches
- Advanced audio controls
- SmartThings integration support
- Multiple device support

## Credits

Original project created by @samuelspagl.

Special thanks to:
- @PiotrMachowski
- @thierryBourbon

for the original ideas and groundwork around Samsung Soundbar integrations for Home Assistant.
