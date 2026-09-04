# Nebula for Home Assistant

> **Beta.** Part of the **Nebula Home** system — this integration is the *hub*.
> The companion pieces (Nebula HOST config backend, the Nebula Home iOS app, an
> optional wall panel) live in [`kamtechsol/nebula-control`](https://github.com/kamtechsol/nebula-control);
> start with its `host/SETUP.md`. Firmware updates for panels running Nebula
> Cosmos UI are served by a separate integration,
> [`kamtechsol/nebula-ota-hass`](https://github.com/kamtechsol/nebula-ota-hass).
> Please file issues with your HA install type (OS / Container / Supervised) and
> version.

Custom integration that pairs the **Nebula** app + panel with Home Assistant and
keeps the connection alive from the HA side.

It gives Nebula clients one thing to talk to:

- **Combined feed** — a single `nebula/subscribe` WebSocket command returns a
  snapshot with rooms (entities pre-grouped by area), scenes, scripts and
  automations, then streams deltas. No more `get_states` + three registry calls +
  a `state_changed` subscription on every client.
- **LAN discovery** — advertises `_nebula._tcp` with the instance URL, so the app
  finds Home Assistant without anyone typing an address.
- **One-scan pairing** — `nebula.pair_code` (or the panel's pairing screen)
  produces a single-use PIN; the app posts it to `/api/nebula/pair` and gets a
  long-lived token back, shown in your profile as `Nebula: <device>` and
  revocable there.
- **Connection sensors** — `binary_sensor.nebula_app_connected` and
  `binary_sensor.nebula_panel_connected`.

## Install

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/kamtechsol/nebula-hass`, category **Integration**
3. Install **Nebula**, restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Nebula**

### Manual

Copy `custom_components/nebula` into your HA `config/custom_components/`, restart,
then add the integration.

## Pairing the app

**With the panel:** open the panel's pairing screen and scan the QR in the app —
the QR now carries the HA link too, so one scan configures both.

**Without the panel:** Developer Tools → Actions → `nebula.pair_code` → Run.
A PIN appears as a notification; enter it in the app under
*More → Home Assistant → Link*.

## HTTP surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET  | `/api/nebula/snapshot` | HA token | Full room / scene / automation picture |
| GET  | `/api/nebula/clients`  | HA token | Currently connected clients |
| POST | `/api/nebula/pair`     | PIN in body | Exchange a PIN for a long-lived token |

## WebSocket commands

`nebula/subscribe`, `nebula/heartbeat`, `nebula/pair_code`, `nebula/call` —
served over Home Assistant's authenticated `/api/websocket`.

## License

MIT
