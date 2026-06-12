# Architecture & Protocol Notes

How the Ambilight+Hue Pro Bridge works, what the older Philips TVs actually speak, and
the module/data design. Distilled from research into diyHue, Home Assistant
`emulated_hue` / `hass-emulated-hue`, Music Assistant's `hue_entertainment`, and
community packet/log analysis. Source citations are at the bottom.

> Status: **built and working.** Discovery (SSDP + mDNS), pairing, the v1 REST surface, the
> outbound + inbound Entertainment streaming, and the web UI all ship and have been verified
> live against both an older (2018, TPM171E) and a newer (2022, Android OLED807) Ambilight TV
> streaming to a Hue Pro. The current implementation is summarised in §9. Packaging (Docker /
> HA-OS add-on) is the main thing still outstanding. This doc also records the protocol
> research the build is based on.

---

## 1. Protocol reality check — what the TV speaks, and what we must implement

**Two TV generations, two transports — and the TV decides which, not us.**

- **Older / non-Android Ambilight+Hue TVs (~2014–2018 firmware):** drive lights
  exclusively over the **legacy Hue v1 REST API at ~1 request/second** — plain HTTP
  `PUT /api/<user>/lights/<id>/state` and/or `PUT /api/<user>/groups/<id>/action` on
  port 80. **No DTLS, ever.** (Confirmed by adversarial verification: diyHue #988
  maintainer statement, diyHue #318 firsthand report, AVForums TP-Vision log analysis,
  and the Toengel blog timeline — entertainment support only arrived with 2018 firmware.)
- **Newer Android Ambilight TVs (~2019+, e.g. 55PUS7354/12):** open a **Hue
  Entertainment DTLS-PSK stream on UDP 2100 at 25–50 pkt/s** after
  `PUT /api/<user>/groups/<id> {"stream":{"active":true}}`. (hass-emulated-hue #61.)
- **Per-lamp split is real:** even on a streaming-capable TV, the stream-vs-legacy
  choice is made *per lamp*. It is driven by **both** a hardcoded `modelid` whitelist in
  TV firmware **and** a read of the bridge's `/capabilities` (the TV only switches to
  Entertainment once a `streaming` capability is present — diyHue #318). Lamps off the
  whitelist fall back to the 1 Hz REST path (AVForums log:
  `PostALState: lampstatus.modelid: LCG002 ... non Hue streaming Hue lamp`).

**Load-bearing insight:** on the **TV-facing** side we are an *inbound control surface*,
not a quality-critical stream consumer. The TV is the source; the **real bridge** is
where we stream out with low latency.

| Layer | Required? | Why |
|---|---|---|
| **SSDP/UPnP responder** (UDP 1900 + `/description.xml`) | **Mandatory, byte-faithful** | hass-emulated-hue #41: a v1+SSDP emulator was *not discovered* until its M-SEARCH reply was byte-compatible with a real 2015 bridge — correct `hue-bridgeid` (MAC with `FFFE` inserted, uppercase), `USN` matching the descriptor UDN `uuid:2f402f80-da50-11e1-9b23-<mac>`, `SERVER: …IpBridge/1.x`, the `uuid:` ST/USN variant, **plus periodic SSDP NOTIFY broadcasts**. A naive responder fails. |
| **mDNS `_hue._tcp.local`** (`modelid`+`bridgeid` TXT) | **Implemented (additive to SSDP)** | `discovery/mdns.py`; gated on `enable_mdns`, points clients at the TLS port when HTTPS is enabled else the HTTP port. The newer discovery path. |
| **v1 REST emulator** (`/api`, pairing, `/config`, `/capabilities`, lights, groups) | **Mandatory** | The universal control path. Must advertise `capabilities.streaming` so streaming-capable TVs engage. Light state fields (`hue/sat/bri/ct`) must be **strictly integer-typed** — TVs reject floats (#61). Also serves a local N-UPnP JSON (`/api/nupnp`, `/nupnp`) mirroring `discovery.meethue.com` for LAN clients like aiohue — the Ambilight TVs don't use it. |
| **`generateclientkey` on pairing** | **Mandatory** | The v1 pairing contract returns a `clientkey` (32-char uppercase hex) when requested; newer TVs need it to open their inbound stream. |
| **Inbound DTLS server on UDP 2100** | **Implemented, gated** | Supports 2019+ Android TVs that stream *into* us. Started at boot when `enable_inbound_dtls` is set (default on); not required for the older-TV target. |
| **Outbound DTLS client to the real bridge** | **Mandatory** | The core value: translate inbound (1 Hz REST *or* 25–50 Hz DTLS) into a low-latency Entertainment stream out to the V2/Pro bridge. |

A virtual bridge emulating **only** v1 REST + faithful SSDP/UPnP is **sufficient for the
older-TV target** — provided the SSDP faithfully mimics a `BSB002` 2015 bridge *and* the
real bridge is absent from `discovery.meethue.com` for the user's public IP (else cloud
N-UPnP shadows us and the TV never falls back to LAN SSDP — #988). Building the inbound
DTLS server makes it a superset that also serves 2019+ Android TVs.

### Residual uncertainty & how to de-risk
1. **Exact legacy write shape** (per-light `state` vs per-group `action`, color encoding
   xy/hs/ct, whether `transitiontime` is sent) — no primary capture exists. **De-risk:**
   `tcpdump -i any -w tv.pcap 'port 80 or port 1900 or port 2100'` against the real TV,
   or have M1's virtual bridge log every request verbatim. **Highest-value early step.**
2. **Cached / cloud bridge shadowing** — once the TV knows a bridge (cached or cloud), it can
   prefer it over a fresh LAN SSDP scan. A **full power-cycle** of the TV clears that cache and
   forces fresh discovery (verified live). We **cannot** publish our virtual bridge to
   `discovery.meethue.com` — it lists only real, cloud-registered bridges matched by the
   requester's public IP, over an undocumented credentialed channel (diyHue and every emulator
   are LAN-only) — and we don't need to: blocking the TV's internet was only a workaround for
   the stale cache. `data.meethue.com` / `diag.meethue.com` are telemetry, not discovery.
3. **`apiversion`/`swversion` floor** — match diyHue's advertised values exactly
   (`swversion 1967054020`, `apiversion 1.67.0`, `datastoreversion 126`, `modelid BSB002`)
   rather than inventing our own.

---

## 2. End-to-end data flow & latency budget

```
                       ┌──────────────────────── VIRTUAL BRIDGE (our service) ────────────────────────┐
 ┌──────────┐ v1 REST  │ ┌──────────┐   ┌──────────┐   ┌─────────────┐   ┌────────────────┐           │  DTLS-PSK
 │ Ambilight│ ~1 Hz PUT│ │ v1 REST  │   │ ingest   │   │ mapping     │   │ entertainment   │           │ 25–50 Hz
 │   TV     │─────────►│─►│ emulator │──►│ buffer   │──►│ engine      │──►│ client (DTLS    │───────────┼─────────► REAL Hue
 │ (older)  │          │ │ (HTTP 80)│   │ latest-  │   │ vlight→chan │   │ to real :2100)  │           │  UDP 2100   bridge
 │          │ DTLS in  │ └──────────┘   │ wins     │   │ +gradient   │   └────────────────┘           │           (V2/Pro)
 │ (newer)  │ 25–50 Hz │ ┌──────────┐    ▲             ▲                                                │
 │          │─────────►│─►│ DTLS srv │────┘     ┌──────┴───────┐                                        │
 └──────────┘ UDP 2100 │ │ (lazy)   │          │ config store │ (bridges, areas, mappings, PSK)        │
                       │ └──────────┘          └──────────────┘                                         │
                       └────────────────────────────────────────────────────────────────────────────────┘
```

**Core move:** decouple inbound rate from outbound rate with a **single-slot
"latest-state-wins" buffer** per virtual light. Inbound writes (1 Hz REST or 50 Hz DTLS)
only update the current target color. A **fixed-rate outbound ticker** (25/50 Hz,
configurable) reads current state and emits an Entertainment frame.

- 1 Hz REST TV → resampled up to a smooth outbound stream (KISS default = hold last
  value; optional interpolation later).
- 50 Hz DTLS TV → decoupled from outbound socket backpressure.

**Latency budget (older-TV path):** the TV's ~1 Hz cadence dominates (0–1000 ms) and we
**cannot** fix it — that is inherent to the legacy path. Everything *we* add is
sub-100 ms (parse+map+enqueue < 1 ms; ticker pickup ≤ 1 frame ~20–40 ms; DTLS→bridge→zigbee
~20–50 ms). **The win isn't making the old TV fast — it physically samples at 1 Hz. The
win is (a) presenting lights the TV will talk to and stream-classify, and (b) rendering
them on the real bridge over the fast Entertainment path instead of the laggy per-lamp
legacy zigbee path.** For the newer-TV (DTLS-in) path we deliver genuine end-to-end low
latency because both halves stream.

---

## 3. Language, libraries & the shared-code decision

**Python 3.13+ with `asyncio`.** Rationale: maintainer fit (MA/aiohue ecosystem), asyncio
is the right model for multiplexing an HTTP server + UDP SSDP + mDNS + an outbound DTLS
stream + a fixed-rate ticker on one loop, and Docker/HA-OS packaging is first-class.

### The outbound entertainment client — extract from Music Assistant, don't rely on aiohue

**Correction to a common assumption: `aiohue` does NOT contain an entertainment streaming
client.** It has CLIP v2 *models* only (`entertainment.py`, `entertainment_configuration.py`)
— no DTLS, no PSK, no `HueStream` encoder, and `create_app_key()` doesn't even request
`generateclientkey`. So "just depend on aiohue" is a non-starter for the streaming path.

The streaming client already exists, battle-tested, in **Music Assistant**:
`music_assistant/providers/hue_entertainment/hue_sendspin_bridge/` —

- `api.py` — `HueEntertainmentAPI`: `pair()` (with `generateclientkey`, 30 s button
  retry), `get_entertainment_areas()` (parses `entertainment_configuration` into
  areas/channels **with positions**), `start_entertainment()` / `stop_entertainment()`,
  `get_bridge_id()`. Raw aiohttp with `ssl=False`.
- `dtls.py` — pure-Python **DTLS 1.2 PSK** (`TLS_PSK_WITH_AES_128_GCM_SHA256`): full
  handshake, TLS 1.2 PRF, key derivation, AES-128-GCM records, multi-datagram parsing,
  daemon sender thread + 5 s keepalive, **`HueStream` v2 encoder**. Only deps: stdlib +
  `cryptography`'s `AESGCM`. No openssl subprocess (unlike diyHue), no external DTLS lib.
- `models.py` — `EntertainmentArea`, `LightChannel` (incl. xyz position),
  `LightColorCommand`.
- README: *"Working and tested on Hue Bridge V2 and Hue Bridge Pro."* The sub-package
  docstring explicitly anticipates extraction into a standalone library.

**Done.** This was extracted into the standalone
[music-assistant/hue-entertainment](https://github.com/music-assistant/hue-entertainment)
package (deps `aiohttp` + `cryptography`), consumed here as a runtime dependency
(`pyproject.toml`). `EntertainmentSession`, `HueEntertainmentAPI`, `EntertainmentArea`, and
`discover_bridges` are imported from it (`outbound/controller.py`, `engine/engine.py`,
`web/server.py`) — there is **no in-repo `hue_entertainment` module**. It ships the
`EntertainmentSession` lifecycle wrapper (open-on-demand, idle-close, single-active-stream
guard). Pro needs no special handling — same CLIP v2 + DTLS.

`aiohue` remains optional, useful only if we later want general CLIP control (rooms,
scenes, richer light metadata, the SSE event stream) beyond entertainment.

### Library table

| Concern | Choice | Notes |
|---|---|---|
| Outbound entertainment (auth, areas, start/stop, DTLS, HueStream) | **`hue-entertainment`** (PyPI, shared with MA) | Pure-Python DTLS-PSK; tested V2 + Pro. |
| HTTP server (v1 REST emulator + web UI API) | **`aiohttp`** | Single async server on port 80. |
| SSDP/UPnP | hand-rolled `asyncio.DatagramProtocol` on UDP 1900 | Copy diyHue's exact response bytes; don't use a generic UPnP lib. |
| mDNS | **`python-zeroconf`** (async) | Mirror diyHue TXT records. |
| Inbound DTLS server (UDP 2100, optional) | reuse `hue_entertainment`'s DTLS primitives in server role, or `python-mbedtls` | Gate behind a flag (§7). |
| Config | **YAML** (debuggable) or SQLite | KISS; atomic writes. |
| Web UI | static SPA served by aiohttp | Keep the build out of the runtime image. |

---

## 4. Component / module map

```
ambilight_hue_bridge/
├── app.py                  # supervisor: build + start/stop all services, signal handling
├── color.py                # v1 light state (on/bri/hue/sat/xy/ct) → 16-bit RGB
├── const.py                # bridge identity, ports, SSDP, streaming bounds
├── identity.py             # bridgeid / UDN / serial from the host MAC
├── config/
│   ├── store.py            # load/save YAML; atomic writes
│   └── models.py           # config dataclasses (see §5)
├── discovery/
│   ├── ssdp.py             # UDP 1900: M-SEARCH replies + NOTIFY broadcaster (60 s)
│   ├── mdns.py             # zeroconf _hue._tcp.local advertisement (TLS port)
│   ├── cert.py             # Hue-style self-signed EC P-256 TLS cert for HTTPS
│   └── description.py      # builds /description.xml (BSB002 template)
├── emulator/               # TV-FACING virtual bridge
│   ├── rest_v1.py          # aiohttp: /api, pairing, /config, /capabilities, lights, groups, /api/nupnp
│   ├── pairing.py          # username + clientkey minting; persisted user store
│   ├── light_repr.py       # v1 light JSON (strict int types, modelid, capabilities.streaming)
│   ├── huestream.py        # HueStream v1/v2 frame decoder (inbound)
│   ├── inbound.py          # inbound DTLS streamer: decode frames → engine.submit_color
│   └── dtls_server.py      # inbound DTLS-PSK server on UDP 2100
├── engine/
│   ├── engine.py           # on-demand outbound stream: smoothing + fixed-rate ticker
│   ├── ingest.py           # latest-state-wins color buffer; one slot per virtual light
│   └── mapping.py          # virtual light → real entertainment channel commands
├── outbound/
│   └── controller.py       # pair/list-areas on the real bridge; per-TV light/area resolution
└── web/
    ├── server.py           # JSON config API (/cfg/*) for the SPA
    └── static/             # single-page web UI
```

`EntertainmentSession`, `HueEntertainmentAPI`, `EntertainmentArea`, and `discover_bridges`
come from the external **`hue-entertainment`** dependency, not an in-repo module.

**Lifecycle:** SSDP + mDNS + `/description.xml` + the v1 REST/web API + the inbound DTLS
server are **started at boot** (cheap, idle sockets). Only the **outbound** entertainment
stream starts lazily — when a TV activates entertainment or pushes its first color — and
tears down on idle (§7). Clean shutdown stops the outbound stream first (so the real bridge
leaves streaming mode and lights resume normal control), then the sockets.

---

## 5. Data model & config schema (sketch)

```yaml
virtual_bridge:
  name: "Ambilight Bridge"
  mac: null                          # null => auto-detect from the host; drives bridgeid/UDN/serial
  enable_inbound_dtls: true          # inbound DTLS server for 2019+ Android TVs (UDP 2100)
  enable_mdns: true                  # advertise _hue._tcp via mDNS (TLS port if HTTPS on, else HTTP)
  stream_rate_hz: 50                 # outbound frame rate to the real bridge (config-only, no UI)

real_bridges:                        # one or more real Hue bridges (V2 square or Pro)
  - id: "192-168-1-50"
    host: "192.168.1.50"
    app_key: "<clip v2 username>"
    client_key: "<32 hex PSK>"       # for the entertainment DTLS stream
    model: "v2"                      # "v2" | "pro" (informational; same code path)
    cached_areas: []                 # last-seen areas; serves the UI while the bridge is down
active_real_bridge: "192-168-1-50"   # which bridge TVs stream to (empty => the first)

users:                               # paired TVs; each is assigned an area in the web UI
  - username: "<hue v1 username>"
    clientkey: "<32 hex PSK>"        # for the TV's inbound DTLS stream
    devicetype: "55POS9002/12"
    created: "2026-06-10T12:00:00"
    entertainment_area: "<v2 rid>"   # the real-bridge area this TV drives ("" => no lights)
    split_gradients: true            # expose each gradient zone as its own virtual light
    stream_smoothing: null           # per-TV temporal easing (null/0 = off, capped at 0.95)
    lights:                          # what the TV sees; rebuilt from the area on assignment
      - id: "1"
        name: "Ambilight Left"
        modelid: "LCX004"
        position: "left"             # informational, for the web UI
        channels: [0]                # real-bridge entertainment channel ids this light drives
```

Notes: lights resolve **per TV** — a TV with no area (`entertainment_area: ""`) sees no lights
and does not stream. A freshly paired TV is auto-assigned the active bridge's first area (from
`cached_areas`, so it works even mid discovery-dance); each TV's `lights` are rebuilt from its
assigned area when you (re)set it in the web UI (split gradients = one virtual light per zone,
else one per device). Temporal easing is **per TV** (`stream_smoothing`) — fast DTLS TVs want 0,
~1 Hz REST TVs want easing to fill the gaps. The advertised
`swversion`/`apiversion`/`datastoreversion` and the bridge model id live in `const.py`; the
HTTP/HTTPS ports are command-line options, not persisted. State fields emitted to the TV are
strictly int-typed, and every exposed light advertises `capabilities.streaming`.

---

## 6. Gradient / multi-zone mapping

Two mappings meet here:

- **(A) Physical gradient strip → N virtual lights (TV-facing).** A gradient strip exposes
  multiple entertainment *channels* (5- or 7-zone segmentation). The TV thinks in discrete
  edge lamps, so we synthesize N virtual v1 lights, each a segment (or a contiguous span),
  which the user assigns to screen edges in the TV's own Ambilight+Hue menu. Granularity
  (per-zone vs left/center/right) is a config choice.
- **(B) N TV lights → entertainment channels (real-bridge-facing).** Each inbound color for
  virtual light *V* resolves to one or more channel IDs. The ticker assembles a full
  channel frame each tick:

  ```
  for each tick (50 Hz):
      frame = {}
      for vlight in area.virtual_lights:
          color = ingest_buffer[vlight.v1_id]      # latest-wins, hold last
          for ch in vlight.target.channels:
              frame[ch] = to_color(color)
      entertainment_session.send(frame)            # → DTLS HueStream to real bridge
  ```

Fan-out (one virtual light → many channels) is supported and matches how the legacy TV
thinks (one color per edge). Unmapped channels get a configurable default. Channel layout
is read from CLIP v2 at config time; the web UI lets the user assign visually with live
preview rather than hardcoding indices.

---

## 7. Idle / resource strategy

Goal: **when no TV is driving lights, the process is a few idle sockets at ~0% CPU.**

1. **No outbound stream held open when idle.** Activating an Entertainment area takes the
   real bridge out of normal control and there's a hard concurrent-session limit. Start the
   outbound stream **lazily** on the first inbound write; **tear down** after an inactivity
   timeout. The ticker runs only while streaming.
2. **Inbound DTLS server.** Enabled by default (`enable_inbound_dtls: true`) and started at
   boot — it's an idle listening socket until a newer TV streams in; the lazy part is the
   *outbound* stream (above), not this listener. Set the flag false for a pure older-TV setup.
   Pure-Python DTLS, so none of diyHue's "openssl subprocess always running" anti-pattern.
3. **Always-on but cheap:** SSDP socket, mDNS, `/description.xml` + v1 REST server, web UI.
   A connected TV polls `GET /lights /groups /config /sensors` heavily — serve from
   in-memory state, no real-bridge I/O for reads.
4. **Lazy real-bridge connection;** single event loop, no thread pool (small footprint on a
   Pi / HA-OS).

State machine: `IDLE → (first inbound write) → STREAMING → (inactivity timeout) → IDLE`.

---

## 8. Top risks (ranked) & mitigations

1. **Uncaptured legacy REST payload from a real older TV.** *Could mis-parse colors.*
   → `tcpdump` the real TV (or log verbatim in M1) **before** writing `mapping.py`; accept
   both `/lights/<id>/state` and `/groups/<id>/action`. (MVP M0.)
2. **Discovery failure / cached-bridge shadowing.** Faithful SSDP is hard; a cached or
   cloud-registered bridge can hide us. → byte-faithful SSDP incl. NOTIFY (done) + mDNS (done);
   a full TV power-cycle clears a stale cache. We can't (and needn't) publish to
   `discovery.meethue.com` — see §1.
3. **Strict JSON typing / config-version floor.** TVs reject float `hue`; may enforce min
   `apiversion`. → int-coerce; match diyHue's advertised versions + `BSB002`.
4. **Outbound session limits / Pro specifics.** → validate the extracted client against a
   real Pro bridge early (M2); lazy start/stop; clear errors when another app holds the
   session. (No protocol difference expected — MA confirms Pro works on the same code.)
5. **Newer-Android inbound DTLS** (if supported). Trickiest inbound piece. → later
   milestone behind `enable_inbound_dtls`; reuse the extracted DTLS primitives in server
   role; older-TV target doesn't need it.
6. **Gradient channel-index drift across firmware/strip models.** → read the real area's
   channel list from CLIP v2 at config time; assign visually in the UI.

---

## 9. Current implementation

What's built and verified live against both Ambilight generations:

- **Discovery** — `identity.py` + a byte-faithful SSDP/UPnP responder (`/description.xml`,
  periodic NOTIFY) and an mDNS `_hue._tcp` advertisement, plus an optional self-signed-cert
  HTTPS listener. Older TVs find us over SSDP; the 2022 Android set finds us over mDNS.
- **Virtual bridge** — the v1 REST surface (pairing with `generateclientkey`, `/config`,
  `/capabilities` with `streaming`, `/lights`, `/groups`, strict int typing) plus a local
  N-UPnP endpoint for other LAN clients.
- **Streaming** — inbound on both paths (the ~1 Hz v1 REST writes *and* the newer TVs' DTLS
  HueStream on UDP 2100) into a latest-wins ingest buffer; a fixed-rate ticker applies per-TV
  smoothing and forwards on demand to a **V2 / Pro** bridge via the shared `hue-entertainment`
  client, with idle teardown.
- **Engine** — per-TV light↔channel mapping, gradient-zone split, per-TV smoothing, light
  identify (blink over the stream), and a single-active-stream guard.
- **Web UI** — bridge pairing, per-TV area assignment + smoothing, and TV removal.

The exact older-TV write shape that §1/§8 flag as uncaptured is now largely confirmed from live
request logs of both TV generations (see the memory/protocol notes).

**Still outstanding:** packaging — a multi-arch Docker image (host networking for SSDP/mDNS +
UDP 2100) and a HA-OS add-on (S6, `config.yaml`/`bashio`, ingress for the web UI,
`host_network: true`) — and continued verification across more TV models.

---

## Sources

- **diyHue** — #988 (older TVs use 1 Hz REST), #318 (capability check + real-TV HTTP log),
  `services/{ssdp,mdns,entertainment}.py`, `flaskUI/restful.py`,
  `flaskUI/templates/description.xml`.
- **hass-emulated-hue** — #41 (SSDP must be byte-faithful + NOTIFY broadcast), #61 (newer
  Android TV DTLS on 2100; strict int typing), `controllers/entertainment.py`, `discovery.py`.
- **HA `emulated_hue`** — v1 emulation + UPnP scaffolding patterns.
- **Music Assistant** — `music_assistant/providers/hue_entertainment/hue_sendspin_bridge/`
  (`api.py`, `dtls.py`, `models.py`, `constants.py`) — the extractable entertainment client.
- **AVForums** PUS9005 thread pp. 9–12 (per-lamp `modelid` whitelist; TP-Vision log
  analysis; ~1–2 s "normal" delay). **Toengel Philips blog** (2018 firmware = entertainment
  support). **Hueblog 2025-09** (Pro `modelid`; Aug-2025 HTTPS-for-external-apps = cloud
  path only).
- *Correction:* the "diyHue #434" whitelist citation seen in some notes is wrong (#434 is a
  Dependabot bump); the whitelist evidence is the AVForums thread.
```
