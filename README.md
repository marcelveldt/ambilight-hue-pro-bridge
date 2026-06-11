# Ambilight+Hue Pro Bridge

Bridge older Philips **Ambilight+Hue** TVs to modern Philips Hue bridges — including
the new **Hue Pro bridge** — over the low-latency **Entertainment API**, with support
for **multi-zone / gradient lights**.

> **Status:** working and in real use. Discovery (SSDP + mDNS), pairing, the v1 REST API, the
> web UI, per-TV configuration, and the outbound + inbound Entertainment streaming are all
> implemented and verified live against both an older (2018) and a newer (2022, Android)
> Ambilight TV. Not yet packaged as a Docker image / Home Assistant add-on (see [Status](#status)).

## Why this exists

Older Philips Ambilight TVs (TP Vision) can drive Philips Hue lights directly via the
built-in **Ambilight+Hue** feature. The new Philips **Hue Pro bridge** dropped support
for that legacy protocol and instead requires a separate **Hue Sync box** — which adds
cost and forces your video sources through that box's HDMI inputs.

This project restores Ambilight+Hue for those TVs against any modern Hue bridge, and
adds two things the original feature never had:

- it works with the **Pro bridge** (and the square V2 bridge), and
- it can map a single **gradient / multi-zone** light strip into multiple addressable
  zones for a far richer Ambilight effect.

## How it works

The app sits on your LAN between the TV and your real Hue bridge:

```
   ┌──────────────┐         ┌───────────────────────────────────────┐         ┌────────────────┐
   │ Philips TV    │  LAN    │        Ambilight+Hue Pro Bridge         │  DTLS   │  Real Hue       │
   │ (Ambilight    │ ──────► │                                         │ ──────► │  bridge          │
   │  +Hue)        │         │  virtual Hue bridge  →  mapping engine  │  (UDP   │  (V2 / Pro)      │
   │               │         │  (discovery + v1 API)   →  entertainment │  2100)  │                  │
   └──────────────┘         │                            client        │         └────────────────┘
                            └───────────────────────────────────────┘
                                          ▲
                                          │  web UI (configuration)
                                       ┌─────┐
                                       │ you │
                                       └─────┘
```

1. The app advertises a **virtual Hue bridge** on the network. The TV discovers and
   pairs with it exactly as it would a real bridge, and sees the lights you chose to
   expose (including gradient strips split into multiple virtual lights).
2. When Ambilight runs, the TV streams light updates to the virtual bridge.
3. A **mapping engine** translates those updates onto the channels of an
   **entertainment area** on your real bridge.
4. An **entertainment client** streams the result to the real bridge over DTLS for
   minimal latency.

## Connecting a TV

Pair your real Hue bridge in the web UI first, then run the **Ambilight+Hue** setup on the TV.

If you also have a **real Hue bridge on the network** (e.g. a Hue Pro), the TV may discover
*that* instead of the virtual one — and because the Pro dropped Ambilight+Hue support, that path
just fails. To force the TV onto the virtual bridge:

1. **Unplug your real Hue bridge.**
2. **Restart (power-cycle) the TV** — this clears its cached bridge.
3. **Start the Ambilight+Hue setup wizard on the TV** — it now discovers the virtual bridge.
4. Once paired, **plug the real bridge back in** (it's where the colours are streamed to) and
   assign the TV an entertainment area in the web UI.

The bridge it's cached to sticks across reboots, so this is a one-time step per TV.

## Features

- 🪟 Web interface for configuration
- 🧩 KISS, robust, low-resource always-on background service (cheap when idle)
- 📺 Per-TV setup — assign each TV an entertainment area, with its own smoothing
- 🌈 Map a gradient / multi-zone light into multiple addressable virtual lights
- 📡 Discovered by both older (SSDP/UPnP) and newer (mDNS) Ambilight TVs
- 🔌 Works with both the square Hue V2 bridge and the new Hue Pro bridge
- ⚡ Low-latency streaming — inbound DTLS from newer TVs, ~1 Hz REST from older ones, forwarded over the Entertainment API

## Architecture

The service is built from a few focused, decoupled components:

- **Virtual bridge** — SSDP/UPnP discovery responder + an emulator of the legacy Hue v1
  REST API the TV speaks (inspired by [diyHue](https://github.com/diyhue/diyHue) and
  Home Assistant's [`emulated_hue`](https://github.com/home-assistant/core)).
- **Mapping engine** — maps the TV's exposed (virtual) lights onto the channels of a
  real entertainment area, including splitting one gradient strip into N zones.
- **Entertainment client** — connects to the real bridge and streams `HueStream` frames
  over DTLS/PSK, via the shared
  [music-assistant/hue-entertainment](https://github.com/music-assistant/hue-entertainment)
  library (pure-Python DTLS 1.2 PSK + `HueStream` encoder, *"working and tested on Hue
  Bridge V2 and Hue Bridge Pro"*).
- **Web UI + config store** — pair the real bridge, then assign each paired TV an
  entertainment area (optionally splitting its gradient strips into per-zone virtual
  lights). A TV has no lights until it's assigned an area.

> Detailed protocol notes and the module map live in
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Installation

Run it from source (see [Development](#development) below). Packaging as a multi-arch Docker
image and a Home Assistant OS add-on is planned. Because the service answers SSDP/mDNS multicast
and presents a virtual bridge on the LAN, it needs to run with **host networking**.

## Development

Requires Python 3.13+.

```bash
# create a virtualenv, install the package + dev deps, and set up pre-commit
scripts/setup.sh

# run the service — the Hue API and web UI share one port (--http-port, default 8080),
# plus a TLS listener with a Hue-style cert (--https-port, default 443; 0 disables it).
# Discovery (SSDP on UDP 1900 + mDNS _hue._tcp) runs alongside, always on.
python -m ambilight_hue_bridge --log-level DEBUG

# older Ambilight TVs assume the Hue API is on port 80; newer firmware connects over
# HTTPS on 443. Serve both (binding 80/443 needs privileges):
sudo ambilight-hue-bridge --http-port 80 --https-port 443 --log-level DEBUG

# then open the web UI at http://<host>:<http-port> (e.g. http://<host>:8080) to pair
# your Hue bridge (press the link button first). A freshly paired TV has no lights;
# assign it an entertainment area in the UI to expose that area's lights. A
# `pair` / `areas` CLI is also available for headless setup.
#
# logs go to the console and a rotating file at <data-dir>/bridge.log (override with
# --log-file); HTTPS is optional (--https-port 0) — discovery still works over mDNS + HTTP.

# run the checks
pre-commit run --all-files
pytest
```

Linting/formatting use [Ruff](https://docs.astral.sh/ruff/) (`select = ["ALL"]`) and the
project follows the conventions of [aiohue](https://github.com/home-assistant-libs/aiohue),
[aiosonos](https://github.com/music-assistant/aiosonos) and
[Music Assistant](https://github.com/music-assistant/server).

## Status

Implemented and working end-to-end:

- **Discovery** — SSDP/UPnP responder + descriptor and an mDNS `_hue._tcp` advertisement, so both
  older (SSDP) and newer Android (mDNS) Ambilight TVs find the bridge. Optional HTTPS listener.
- **Virtual bridge** — the legacy Hue v1 REST API, pairing (with `generateclientkey`), and a local
  N-UPnP endpoint for other LAN clients.
- **Streaming** — inbound DTLS server (UDP 2100) for newer TVs and the ~1 Hz REST path for older
  ones, forwarded on demand to a V2 or Pro bridge over the Entertainment API via the shared
  [music-assistant/hue-entertainment](https://github.com/music-assistant/hue-entertainment) client.
- **Engine** — per-TV light/area mapping, gradient-zone split, per-TV temporal smoothing, light
  identify (blink), and idle teardown.
- **Web UI** — bridge pairing, per-TV area assignment + smoothing, and TV removal.

Verified live against an older (2018) and a newer (2022, Android) Ambilight TV, streaming to a
Hue Pro bridge.

Planned: a multi-arch Docker image and a Home Assistant OS add-on, plus continued verification of
the TV wire protocol across more models.

## Credits

- [diyHue](https://github.com/diyhue/diyHue) — reference for emulating a Hue bridge and
  the entertainment streaming server.
- Home Assistant [`emulated_hue`](https://github.com/home-assistant/core) — SSDP/UPnP and
  v1 API emulation patterns.
- [Music Assistant](https://github.com/music-assistant/server) — the Hue Entertainment
  streaming client this project's client is derived from.

## License

[Apache 2.0](LICENSE)
