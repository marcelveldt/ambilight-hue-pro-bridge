# Ambilight+Hue Pro Bridge

**Ambilight+Hue for older Philips TVs, with any modern Hue setup.**

The Ambilight+Hue feature of these TVs only works with the old Hue bridge. This bridge takes its
place and passes the TV's colours on to your lights, wherever they are:

| Your Hue lights are on | What you need |
|---|---|
| A **Hue bridge**, the square V2 or the new Pro | An entertainment area in the Hue app |
| **Zigbee in Home Assistant**, no Hue bridge at all | ZHA running on [Ziggurat](https://github.com/zigpy/ziggurat) |

Gradient lights (a Signe or a gradient lightstrip) show a colour per zone, and several TVs can each
have their own lights.

[![Add the repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmarcelveldt%2Fambilight-hue-pro-bridge)

## Quick start

1. **Install** it as a Home Assistant app (the button above), or run the [Docker image](#docker).
2. **Add your lights** in the web UI: pair your [Hue bridge](#hue-bridge), or connect your
   [Zigbee lamps](#zigbee-lamps-without-a-hue-bridge).
3. **Pair the TV**: run the Ambilight+Hue setup on the TV. It shows up in the web UI, where you pick
   its lights. Then place them around the screen in the TV's Ambilight+Hue menu.

Does the TV find your real Hue bridge instead? See
[The TV connects to the real Hue bridge](#the-tv-connects-to-the-real-hue-bridge).

## Installation

### Home Assistant

Click the button above, or go to **Settings → Apps → Install app → ⋮ → Repositories** and add
`https://github.com/marcelveldt/ambilight-hue-pro-bridge`. Install **Ambilight+Hue Pro Bridge**,
start it and open **Ambilight Bridge** in the sidebar.

### Docker

```bash
docker run -d --name ambilight-hue-bridge \
  --network host \
  --restart unless-stopped \
  -v ambilight-hue-bridge-data:/data \
  ghcr.io/marcelveldt/ambilight-hue-pro-bridge:latest
```

Open the web UI at `http://<host>`. The bridge needs host networking and port 80, because that is
where the TV looks for a Hue bridge. Settings are kept in `/data`.

## Hue bridge

- In the web UI, enter the bridge's IP address or click **Find bridge**, click **Pair**, and press
  the link button on the bridge within 30 seconds.
- A new TV gets your first entertainment area. Pick another one on the TV's card.
- A Hue bridge streams to one TV at a time.

## Zigbee lamps without a Hue bridge

- This needs Home Assistant's ZHA running on Ziggurat. The Home Assistant app connects by itself;
  run elsewhere, enter Home Assistant's address and the long-lived access token of an administrator
  under **Home Assistant connection**.
- On the TV's card, set **Lights** to **Zigbee lamps (Home Assistant)** and tick up to 10 Hue colour
  lamps. A gradient lamp shows up on the TV as three lights.
- Pick lamps close to each other, such as the lamps in one room: one of them passes the colours on to
  the others.
- The lamps go back to how they were when the TV stops.
- When another app (such as Music Assistant's Hue Lights Sync) uses one of the lamps, the TV waits
  and tries again every minute.

## Several TVs

- Every TV has an **Enabled** switch. Under **Advanced** you find **Smoothing**, where **Auto**
  suits most TVs, and with two or more TVs a **Priority**: when TVs want the same lights, the
  higher priority wins.
- Zigbee runs two streams at once: 25 frames a second for the highest priority, 10 for the next. An
  older TV only sends about once a second and gets 5, which leaves the full rate to the other.

## Troubleshooting

<a name="the-two-bridge-catch-important"></a>

### The TV connects to the real Hue bridge

A newer TV can find your real Hue bridge instead of this one, and then does nothing. To fix it:
**unplug the real bridge, restart the TV, run the Ambilight+Hue setup, then plug the bridge back
in.**

This can come back whenever this bridge restarts, for example for an update. To prevent it for
good, put the real bridge (or the TV) on its own VLAN that this bridge can still reach. A guest
network does not work, because it cuts this bridge off too.

<details>
<summary><b>Control from Home Assistant</b></summary>

The per-TV settings have a small JSON API, for a REST command or REST sensor:

```bash
curl http://<bridge-host>/cfg/tvs                 # every paired TV and its settings
curl http://<bridge-host>/cfg/tvs/<username>      # one TV
curl -X PUT http://<bridge-host>/cfg/tvs/<username> \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false, "stream_priority": 25}'
```

`stream_smoothing` takes `null` for Auto, or a value from 0 (off) to 0.95. As a Home Assistant app
the API only answers Home Assistant itself, so use `127.0.0.1` as `<bridge-host>`. Run standalone it
has no authentication, so keep it on a trusted network.

</details>

<details>
<summary><b>How it works</b></summary>

The bridge presents a virtual Hue bridge on your network. Older TVs find it over SSDP and send
their colours about once a second over HTTP; newer Android TVs find it over mDNS and stream over
DTLS. The bridge maps the colours onto an entertainment area or onto the Zigbee lamps and streams
them out with the [hue-entertainment](https://github.com/music-assistant/hue-entertainment) library
it shares with Music Assistant. Protocol notes and the module map are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

</details>

<details>
<summary><b>Development</b></summary>

Requires Python 3.13+.

```bash
scripts/setup.sh                                    # venv, dependencies and pre-commit
python -m ambilight_hue_bridge --log-level debug    # web UI and Hue API on port 8080
sudo ambilight-hue-bridge --http-port 80            # the TVs expect port 80
pre-commit run --all-files
pytest
```

Every flag also has an environment variable (`HTTP_PORT`, `HTTPS_PORT`, `LOG_LEVEL`, `LOG_FILE`,
`DATA_DIR`); a flag wins over the app's options, which win over the environment. The `pair` and
`areas` commands help with a headless setup. Logs go to the console and to `<data-dir>/bridge.log`.

</details>

## Credits

- [diyHue](https://github.com/diyhue/diyHue): reference for emulating a Hue bridge and its
  entertainment streaming server.
- Home Assistant's [`emulated_hue`](https://github.com/home-assistant/core): SSDP/UPnP and v1 API
  emulation patterns.
- [Music Assistant](https://github.com/music-assistant/server): the Hue Entertainment streaming
  client this project's client is derived from.

## License

[Apache 2.0](LICENSE)
