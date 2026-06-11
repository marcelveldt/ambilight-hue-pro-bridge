"""Pushlink pairing: minting Hue usernames and client keys for clients."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ambilight_hue_bridge.config.models import PairedUser
from ambilight_hue_bridge.outbound.controller import active_bridge, lights_from_area

if TYPE_CHECKING:
    from ambilight_hue_bridge.config.store import ConfigStore

LOGGER = logging.getLogger(__name__)

_KEY_BYTES = 16


class PairingManager:
    """Handles the legacy Hue pushlink pairing flow and the persisted user store."""

    def __init__(self, store: ConfigStore) -> None:
        """
        Initialize the pairing manager.

        :param store: Config store holding the persisted paired users.
        """
        self._store = store

    def is_known_user(self, username: str) -> bool:
        """Return whether the given username has completed pairing."""
        return any(user.username == username for user in self._store.config.users)

    def create_user(self, devicetype: str, *, generate_clientkey: bool) -> PairedUser:
        """
        Create and persist a new paired user.

        :param devicetype: The client-supplied device type string.
        :param generate_clientkey: Whether to also generate a DTLS client key.
        """
        user = PairedUser(
            username=secrets.token_hex(_KEY_BYTES),
            clientkey=secrets.token_hex(_KEY_BYTES).upper() if generate_clientkey else "",
            devicetype=devicetype,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._auto_assign_area(user)
        self._store.config.users.append(user)
        self._store.save()
        LOGGER.info("Paired new client %r (username %s)", devicetype, user.username)
        return user

    def clientkey_for(self, username: str) -> str | None:
        """Return the stored client key for a username, or None if unset/unknown."""
        for user in self._store.config.users:
            if user.username == username:
                return user.clientkey or None
        return None

    def _auto_assign_area(self, user: PairedUser) -> None:
        """
        Give a freshly paired TV the active bridge's first entertainment area by default.

        This spares the user the "no lights found" dead end on the TV right after pairing: a TV
        with no area exposes no lights. They can reassign it in the web UI afterwards. Uses the
        cached areas so it works even during the discovery dance (real bridge briefly unplugged).
        """
        bridge = active_bridge(self._store)
        if bridge is None or not bridge.cached_areas:
            return
        area = bridge.cached_areas[0]
        user.entertainment_area = area.id
        user.lights = lights_from_area(area, split_gradients=user.split_gradients)
        LOGGER.info("Auto-assigned area %r (%s) to new TV", area.name, area.id)
