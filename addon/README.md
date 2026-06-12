# Ambilight+Hue Pro Bridge

Bridge older Philips **Ambilight+Hue** TVs to a modern Philips Hue (V2 or **Pro**) bridge.

Philips dropped Ambilight+Hue support from the Hue Pro bridge. This add-on presents a *virtual*
Hue bridge on your LAN that the TV connects to (older models over SSDP + the legacy v1 REST API,
newer Android models over mDNS + inbound DTLS), then forwards the colours to your real bridge
over the low-latency Entertainment API.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu (top right) → **Repositories**, and add:
   `https://github.com/marcelveldt/ambilight-hue-pro-bridge`
3. Install the **Ambilight+Hue Pro Bridge** add-on and start it.
4. Open the web UI — either **Open Web UI** on the add-on page / the **Ambilight Bridge** sidebar
   entry (ingress, no extra port), or directly at `http://<your-ha-host>:80`. Pair your real Hue
   bridge and assign each TV an entertainment area.

## Configuration

The Hue bridge itself (real bridge pairing, per-TV areas) is configured from the web UI. The
add-on has a few options on its **Configuration** tab:

- **`http_port`** (default `80`) — the port the Hue API is served on. Ambilight TVs assume `80`;
  only change it if something else on the host needs that port.
- **`log_level`** (default `info`) — `info` (lifecycle events) / `debug` (the TVs' requests) /
  `verbose` (SSDP + web-UI polling firehose) / `warning` / `error`.
- **`https`** (default `false`) — enable the optional TLS listener on 443. The tested TVs use
  plain HTTP, so leave it off unless a client needs TLS.

The bridge runs with **host networking** (required for SSDP/mDNS discovery and for the virtual
bridge to appear on the LAN), so it binds the host's `http_port`. Make sure nothing else on the
host already uses it.

State (your bridge credentials and the rotating log) is stored in the add-on's persistent
`/data`, so it survives restarts and updates.

## Connecting a TV

If you also run a **real Hue bridge** on the network, the TV may discover *that* instead of the
virtual one (and fail, since the Pro dropped Ambilight+Hue). To force the TV onto the virtual
bridge the first time:

1. Unplug your real Hue bridge.
2. Power-cycle the TV (this clears its cached bridge).
3. Start the Ambilight+Hue setup on the TV — it now finds the virtual bridge.
4. Plug the real bridge back in. The TV is auto-assigned your first entertainment area; adjust
   it (or split its gradient strips) in the web UI.

See the [project README](https://github.com/marcelveldt/ambilight-hue-pro-bridge) for full
details.
