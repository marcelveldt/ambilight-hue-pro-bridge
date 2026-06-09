# Architecture & Protocol Notes

How the Ambilight+Hue Pro Bridge works, what the older Philips TVs actually speak, and
the module/data design. Distilled from research into diyHue, Home Assistant
`emulated_hue` / `hass-emulated-hue`, Music Assistant's `hue_entertainment`, and
community packet/log analysis. Source citations are at the bottom.

> Status: **design document for an in-progress build.** Implementation lands milestone
> by milestone (see §9). The one unknown that still needs a real-hardware check is the
> exact legacy REST payload a real older TV sends (§1, §8 risk #1).

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
| **mDNS `_hue._tcp.local`** (`modelid`+`bridgeid` TXT) | Recommended | Newer discovery path; cheap (diyHue `services/mdns.py`). |
| **v1 REST emulator** (`/api`, pairing, `/config`, `/capabilities`, lights, groups) | **Mandatory** | The universal control path. Must advertise `capabilities.streaming` so streaming-capable TVs engage. Light state fields (`hue/sat/bri/ct`) must be **strictly integer-typed** — TVs reject floats (#61). |
| **`generateclientkey` on pairing** | **Mandatory** | The v1 pairing contract returns a `clientkey` (32-char uppercase hex) when requested; newer TVs need it to open their inbound stream. |
| **Inbound DTLS server on UDP 2100** | **Conditional — build it, gate it** | Needed only to support 2019+ Android TVs that stream *into* us. **Not required** for the older-TV target. Keep lazy + behind a flag. |
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
2. **N-UPnP cloud shadowing** (#988) — document the "real bridge must not be the one the
   TV finds via cloud" constraint; LAN-only for v1.
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

**Plan (the user's proposed 2-step):**
1. **Extract → extend:** lift the four files above into an in-repo module
   (`ambilight_hue_bridge/hue_entertainment/`), splitting `constants.py` so only the
   protocol constants come along (drop the Sendspin/mel-spectrum bits). Extend with the
   gaps below.
2. **Libraryize → MA consumes it:** publish as a standalone package (working name
   `hue_entertainment`, deps `aiohttp` + `cryptography` only) and point MA at it, deleting
   its vendored copy. MA's own README already anticipates this.

**Gaps to close during "extend"** (none are in aiohue either, so net-new regardless):
- an `EntertainmentSession` lifecycle wrapper: open-on-demand, debounced idle-close, and
  the **single-active-stream constraint** (the bridge allows one stream; stop others
  before starting) — MA has this logic but welded to Sendspin events in `bridge.py`.
- HueStream **v1** + **xy/gamut** color space — encoder is currently v2-RGB only
  (`COLOR_SPACE_XY` is defined but unused). Add only if needed; RGB-and-let-the-bridge-map
  is fine to start.
- **Pro bridge needs no special handling** — same CLIP v2 + DTLS; MA confirms it works.

`aiohue` remains optional, useful only if we later want general CLIP control (rooms,
scenes, richer light metadata, the SSE event stream) beyond entertainment.

### Library table

| Concern | Choice | Notes |
|---|---|---|
| Outbound entertainment (auth, areas, start/stop, DTLS, HueStream) | **extracted `hue_entertainment` (from MA)** | Pure-Python DTLS-PSK; tested V2 + Pro. |
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
├── app.py                  # supervisor: build loop, start/stop services, signals
├── config/
│   ├── store.py            # load/save YAML; atomic writes
│   └── models.py           # config dataclasses (see §5)
├── discovery/
│   ├── ssdp.py             # UDP 1900: M-SEARCH replies + NOTIFY broadcaster (60 s)
│   ├── mdns.py             # zeroconf _hue._tcp.local registration
│   └── description.py      # serves /description.xml (BSB002 template)
├── emulator/               # TV-FACING virtual bridge
│   ├── rest_v1.py          # aiohttp: /api, pairing, /config, /capabilities, lights, groups
│   ├── pairing.py          # 30 s linkbutton window; username + clientkey minting
│   ├── light_repr.py       # v1 light JSON (strict int types, modelid, capabilities.streaming)
│   └── dtls_server.py      # OPTIONAL inbound DTLS on 2100; HueStream parse → ingest buffer
├── engine/
│   ├── ingest.py           # latest-state-wins buffer; one slot per virtual light
│   ├── mapping.py          # virtual light ↔ real entertainment channel; gradient split
│   └── ticker.py           # fixed-rate outbound loop; buffer → frames → entertainment client
├── hue_entertainment/      # EXTRACTED from MA (later a standalone library)
│   ├── api.py  dtls.py  models.py  constants.py
├── outbound/
│   └── session.py          # EntertainmentSession: lazy connect, idle teardown, single-stream
├── web/
│   ├── api.py              # JSON config API for the SPA
│   └── static/             # built SPA
└── identity.py             # bridgeid/UDN/serial from host MAC
```

**Lifecycle:** SSDP + mDNS + description + v1 REST + web API are **always on** (cheap, idle
sockets). The inbound DTLS server and the outbound entertainment stream **start lazily**
and tear down on idle (§7). Clean shutdown stops the outbound stream first (so the real
bridge leaves streaming mode and lights resume normal control), then the sockets.

---

## 5. Data model & config schema (sketch)

```yaml
virtual_bridge:
  name: "Ambilight Bridge"
  mac: "auto"                      # drives bridgeid/UDN/serial
  swversion: "1967054020"          # match diyHue to clear the TV apiversion floor
  apiversion: "1.67.0"
  datastoreversion: "126"
  http_port: 80
  enable_inbound_dtls: false       # only for 2019+ Android TVs

real_bridges:                       # supports V2 square + Pro
  - id: "bridge-living"
    ip: "192.168.1.50"
    model: "v2"                    # "v2" | "pro"
    app_key: "<clip v2 username>"
    client_key: "<32 hex PSK>"     # for the entertainment DTLS stream

entertainment_areas:                # an "area" = one mapping profile the TV connects to
  - id: "area-tv"
    name: "TV Room"
    real_bridge: "bridge-living"
    real_entertainment_area: "<v2 rid>"
    outbound_rate_hz: 50

virtual_lights:                     # what the TV sees in GET /api/<user>/lights
  - v1_id: "1"
    name: "Ambilight Left"
    area: "area-tv"
    type: "Extended color light"   # capabilities.streaming.{proxy,renderer}=true
    position: "left"               # informational, for the web UI
    target: { kind: "channel", channels: ["7"] }
  - v1_id: "2"
    name: "Ambilight Gradient"
    area: "area-tv"
    target:                         # one virtual light → a slice of a gradient strip
      kind: "gradient_segment"
      gradient_light: "<v2 rid>"
      segments: [0, 1]
      channels: ["3", "4"]
```

Baked-in constraints: **int-coerce** all emitted state fields; advertise
`capabilities.streaming` per exposed light; user exposure choice == which `virtual_lights`
exist (web UI is CRUD over this list).

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
2. **No inbound DTLS server held open.** For newer TVs, `PUT groups/<id> {stream:{active:true}}`
   is the trigger to spin up the listener; tear down on `{active:false}` / idle. For the
   older-TV target it's never started (`enable_inbound_dtls: false`). Avoids diyHue's
   "openssl subprocess always running, stopped via `killall`" anti-pattern.
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
2. **Discovery failure / N-UPnP shadowing.** Faithful SSDP is hard; cloud N-UPnP can hide
   us. → Copy diyHue's exact SSDP bytes incl. NOTIFY; document the cloud-registration
   constraint; add a discovery self-test in the web UI.
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

## 9. Incremental MVP plan (riskiest assumption first)

- **M0 — Capture ground truth** (de-risk #1; ~½ day, little code): point the real older TV
  at a logging stub / `tcpdump`. Record discovery sequence, pairing body, the exact
  light/group write endpoints, color encoding, cadence. *Exit: a byte-level trace of one
  real TV.*
- **M1 — Discoverable, pairable, enumerable virtual bridge** (de-risk #2/#3): `identity.py`,
  SSDP (+NOTIFY), `/description.xml`, mDNS, and the v1 REST surface (pairing with
  `generateclientkey`, `/config`, `/capabilities` with `streaming`, `/lights`, `/groups`)
  serving static in-memory virtual lights with strict int typing. *Exit: the real TV
  discovers, pairs, lists the virtual lights, and polls them.* No real bridge yet — and M1
  doubles as M0's capture by logging every request.
- **M2 — Outbound entertainment to a real bridge** (de-risk #4): extract `hue_entertainment`
  from MA; auth, fetch area + channels, activate stream, push a hand-fed 50 Hz color frame
  to a real **V2 and Pro** bridge. *Exit: CLI pushes colors to real lights with sub-100 ms
  added latency.*
- **M3 — Join the halves:** REST writes → ingest buffer → ticker → entertainment client,
  1 virtual light → 1 channel. *Exit: moving Ambilight on the real TV moves a real Hue
  light over the fast path. End-to-end MVP.*
- **M4 — Mapping engine + web UI:** gradient split, multiple areas, exposure toggles,
  discovery/stream self-tests; lazy start/stop + idle teardown hardened.
- **M5 (optional) — Inbound DTLS server** for 2019+ Android TVs, behind
  `enable_inbound_dtls`. Same ingest buffer + ticker; only the frame source changes.
- **M6 — Packaging:** multi-arch Docker (host networking for SSDP/mDNS + UDP 2100) and a
  HA-OS add-on (S6, `config.yaml`/`bashio`, ingress for the web UI, `host_network: true`,
  privileged only if inbound DTLS / tcpdump needed).

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
