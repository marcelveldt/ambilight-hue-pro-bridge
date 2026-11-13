# Ambilight+Hue Pro Bridge

**Ambilight+Hue for older Philips TVs, with any modern Hue setup.**

This app takes the place of the old Hue bridge these TVs need, and passes their colours on to:

- a **Hue bridge**, the square V2 or the new Pro, or
- **Hue lamps on Zigbee in Home Assistant**, with no Hue bridge at all, when ZHA runs on Ziggurat.

## Quick start

1. Install this app, start it and open **Ambilight Bridge** in the sidebar.
2. Add your lights: pair your Hue bridge (enter its IP address or click **Find bridge**, click
   **Pair**, press the link button within 30 seconds), or skip that for Zigbee lamps.
3. Run the Ambilight+Hue setup on the TV. It shows up in the web UI, where you pick its lights: an
   entertainment area, or **Zigbee lamps (Home Assistant)**. Then place them around the screen in
   the TV's Ambilight+Hue menu.

## Zigbee lamps

- Tick up to 10 Hue colour lamps per TV. Pick lamps close to each other, such as the lamps in one
  room.
- The lamps go back to how they were when the TV stops.
- When another app (such as Music Assistant's Hue Lights Sync) uses one of the lamps, the TV waits
  and tries again every minute.

## Options

- **`http_port`** (default `80`): the TVs look for a Hue bridge on port 80, so only change it if
  something else on the host needs that port.
- **`log_level`** (default `info`): `debug` adds the TVs' requests, `verbose` adds even more;
  `warning` and `error` show less.
- **`https`** (default `false`): an extra TLS listener on port 443. The TVs use plain HTTP, so
  leave it off.

## The TV connects to the real Hue bridge

A newer TV can find your real Hue bridge instead of this one, and then does nothing. To fix it:
**unplug the real bridge, restart the TV, run the Ambilight+Hue setup, then plug the bridge back
in.** The [project page](https://github.com/marcelveldt/ambilight-hue-pro-bridge#the-tv-connects-to-the-real-hue-bridge)
explains how to prevent it for good.
