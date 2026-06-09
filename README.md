# Ambilight+Hue Pro Bridge

Bridge older Philips **Ambilight+Hue** TVs to modern Philips Hue bridges — including
the new **Hue Pro bridge** — over the low-latency **Entertainment API**, with support
for **multi-zone / gradient lights**.

> [!WARNING]
> **Status: early development.** This repository currently contains the project
> scaffolding and a service skeleton only. The bridge components described below are
> being built — nothing is functional yet. See [Roadmap](#roadmap).

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
  over DTLS/PSK. This is being extracted from Music Assistant's battle-tested
  `hue_entertainment` core (pure-Python DTLS 1.2 PSK + `HueStream` v2 encoder, already
  *"working and tested on Hue Bridge V2 and Hue Bridge Pro"*) into a standalone library
  that both projects can share.
- **Web UI + config store** — pair the real bridge, pick the entertainment area, choose
  exposed lights, and configure the gradient → multi-zone mapping.

> Detailed protocol notes and the module map live in `docs/` (added as the design is
> finalized).

## Installation

Packaging as a multi-arch Docker image and a Home Assistant OS add-on is planned — see
[Roadmap](#roadmap). Because the service must answer SSDP multicast and present a
virtual bridge on the LAN, it is expected to run with **host networking**.

## Development

Requires Python 3.13+.

```bash
# create a virtualenv, install the package + dev deps, and set up pre-commit
scripts/setup.sh

# run the (skeleton) service
ambilight-hue-bridge --log-level DEBUG
# or
python -m ambilight_hue_bridge

# run the checks
pre-commit run --all-files
pytest
```

Linting/formatting use [Ruff](https://docs.astral.sh/ruff/) (`select = ["ALL"]`) and the
project follows the conventions of [aiohue](https://github.com/home-assistant-libs/aiohue),
[aiosonos](https://github.com/music-assistant/aiosonos) and
[Music Assistant](https://github.com/music-assistant/server).

## Roadmap

- [ ] Finalize the protocol design (verify the TV ↔ bridge wire protocol against a real TV)
- [ ] Hue Entertainment streaming client (extracted/shared library)
- [ ] Virtual bridge: SSDP/UPnP discovery + legacy v1 REST emulation + pairing
- [ ] Mapping engine (incl. gradient → multi-zone)
- [ ] Web configuration UI + persistent config store
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
