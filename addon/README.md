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
4. Open the web UI at `http://<your-ha-host>:80` to pair your real Hue bridge and assign each
   TV an entertainment area.

## Configuration

The add-on has no options to set — everything is configured from its web UI. The bridge runs
with **host networking** (required for SSDP/mDNS discovery and for the virtual bridge to appear
on the LAN), so it binds host port **80** (Hue API + web UI). Make sure nothing else on the host
already uses port 80. (A TLS listener on 443 is available but off by default — the TVs use plain
HTTP.)

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
