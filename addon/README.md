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

Your real Hue bridge is also a Hue bridge on the LAN — it advertises `_hue._tcp` over **mDNS** just
like this virtual one. Newer Ambilight TVs discover Hue over that local mDNS, so the TV can find the
*real* bridge and connect to it instead — and since the Hue Bridge Pro dropped Ambilight+Hue
support, it then sits there doing nothing. A TV is sticky once locked to a bridge but re-discovers
whenever it loses it, so this can recur whenever the add-on restarts (updates/reboots) while the
real bridge is reachable. This is a purely **local** collision — the TV finds the real bridge via
mDNS on your own network, *not* through Philips' cloud — so blocking `discovery.meethue.com` does
not help.

**Durable fix:** stop the TV from *discovering* the real bridge on the LAN while the add-on still
reaches it by IP. Put the real bridge (or the TV) on its own VLAN / isolated subnet the add-on's
host can still route to — the clean fix, if your router can do VLANs or client isolation. (A guest
network won't do: its isolation is symmetric and would also cut the add-on off from the real bridge.
And on a flat LAN with no VLANs — e.g. a FRITZ!Box — mDNS is link-local multicast between devices,
so it never reaches the router and there's nothing to filter; use the manual step below and unplug
the real bridge during add-on updates.)

**First-time setup / recovery:** unplug the real bridge → power-cycle the TV → run the Ambilight+Hue
setup (it finds only this bridge and locks to it) → plug the real bridge back in. The TV is
auto-assigned your first entertainment area; adjust it in the web UI.

See the [project README](https://github.com/marcelveldt/ambilight-hue-pro-bridge) for the full
explanation.
