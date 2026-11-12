# Ambilight+Hue Pro Bridge

Bring **Ambilight+Hue** back to older Philips TVs. Philips dropped the feature from the new Hue
Pro bridge; this app stands in for the Hue bridge the TV expects and passes its colours on to a
modern Hue bridge (V2 or Pro), or straight to Hue lamps in Home Assistant's ZHA.

## Installation

1. In Home Assistant, go to **Settings → Apps → Install app**.
2. Open the **⋮** menu (top right) → **Repositories**, and add:
   `https://github.com/marcelveldt/ambilight-hue-pro-bridge`
3. Install the **Ambilight+Hue Pro Bridge** app and start it.
4. Open **Ambilight Bridge** in the sidebar.

## Setting up

1. Pair your Hue bridge in the web UI: enter its IP address or click **Find bridge**, click
   **Pair**, then press the link button on the bridge within 30 seconds. Using only Zigbee lamps?
   Skip this step.
2. Run the Ambilight+Hue setup on the TV. It finds this bridge and shows up in the web UI.
3. Give the TV its lights on its card: an entertainment area, or **Zigbee lamps (Home Assistant)**.
   Then place the lights around the screen in the TV's Ambilight+Hue menu.

**Zigbee lamps:** when ZHA runs on Ziggurat, a TV can stream straight to up to 10 Hue colour lamps,
with no Hue bridge. Pick lamps within radio range of each other; they go back to how they were when
the TV stops. While another app (such as Music Assistant's Hue Lights Sync) streams to one of the
lamps, the TV waits and tries again every minute.

## Options

- **`http_port`** (default `80`): the port of the Hue API the TVs use. They expect `80`, so change
  it only if something else on the host needs that port.
- **`log_level`** (default `info`): `debug` adds the TVs' requests, `verbose` also discovery and
  the web UI's own polling; `warning` and `error` show less.
- **`https`** (default `false`): an extra TLS listener on port 443. The TVs use plain HTTP, so
  leave it off.

The app uses host networking, so the TVs can find it. On your network it only answers the Hue API
the TVs use; the web UI only answers Home Assistant. Settings and the log survive restarts and
updates.

## The TV connects to the real Hue bridge

Your real Hue bridge announces itself just like this one, so a newer TV can pick the real one, and
then does nothing. To fix it: **unplug the real bridge, restart the TV, run the Ambilight+Hue
setup, then plug the real bridge back in.** It can happen again when this app restarts for an
update. The [project README](https://github.com/marcelveldt/ambilight-hue-pro-bridge#the-tv-connects-to-the-real-hue-bridge)
explains how to prevent it for good.
