# Ambilight+Hue Pro Bridge

Bridge older Philips **Ambilight+Hue** TVs to modern Philips Hue bridges — including
the new **Hue Pro bridge** — over the low-latency **Entertainment API**, with support
for **multi-zone / gradient lights**.

> [!WARNING]
> **Status: early development.** The virtual bridge (SSDP discovery, pairing, v1 REST API),
> the web configuration UI, the outbound Entertainment streaming engine, and the inbound
> DTLS path for newer TVs all work; packaging is still to come, and the TV-side wire
> protocol still needs verification against a real TV. See [Roadmap](#roadmap).

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

## Features

- 🪟 Web interface for configuration
- 🧩 KISS, robust, low-resource always-on background service (cheap when idle)
- 🎬 Multiple entertainment areas
- 🌈 Map a gradient / multi-zone light into multiple virtual lights
- 🎛️ Choose which lights to expose to the virtual bridge
- 📡 Exposes a virtual Hue bridge for the TV to connect to
- 🔌 Works with both the square Hue V2 bridge and the new Hue Pro bridge
- ⚡ Entertainment API streaming with as little latency as possible
- 🐳 Runs as a Docker container and as a Home Assistant OS add-on

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

Packaging as a multi-arch Docker image and a Home Assistant OS add-on is planned — see
[Roadmap](#roadmap). Because the service must answer SSDP multicast and present a
virtual bridge on the LAN, it is expected to run with **host networking**.

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

# run the checks
pre-commit run --all-files
pytest
```

Linting/formatting use [Ruff](https://docs.astral.sh/ruff/) (`select = ["ALL"]`) and the
project follows the conventions of [aiohue](https://github.com/home-assistant-libs/aiohue),
[aiosonos](https://github.com/music-assistant/aiosonos) and
[Music Assistant](https://github.com/music-assistant/server).

## Roadmap

- [x] Hue Entertainment streaming client — the shared [music-assistant/hue-entertainment](https://github.com/music-assistant/hue-entertainment) library
- [x] Virtual bridge: SSDP/UPnP discovery + legacy v1 REST emulation + pairing
- [x] Outbound engine: v1 state → RGB, ingest buffer, channel mapping, on-demand streaming
- [ ] Verify the TV ↔ bridge wire protocol against a real TV (capture via the request log)
- [x] Web configuration UI — bridge discovery + pairing + per-TV area assignment
- [x] Inbound DTLS server (UDP 2100) for newer Android Ambilight TVs (pure-Python DTLS-PSK)
- [x] mDNS `_hue._tcp` advertisement + local N-UPnP endpoint (additive LAN discovery)
- [x] Web UI: per-TV entertainment-area assignment (optional gradient-zone split)
- [ ] Docker image (multi-arch)
- [ ] Home Assistant OS add-on

## Credits

- [diyHue](https://github.com/diyhue/diyHue) — reference for emulating a Hue bridge and
  the entertainment streaming server.
- Home Assistant [`emulated_hue`](https://github.com/home-assistant/core) — SSDP/UPnP and
  v1 API emulation patterns.
- [Music Assistant](https://github.com/music-assistant/server) — the Hue Entertainment
  streaming client this project's client is derived from.

## License

[Apache 2.0](LICENSE)
