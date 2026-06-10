"""Loading and persisting the configuration to a YAML file."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import Config

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


class ConfigStore:
    """Loads and atomically persists the bridge configuration as YAML."""

    def __init__(self, path: Path) -> None:
        """
        Initialize the store (no I/O until :meth:`load`).

        :param path: Path to the YAML configuration file.
        """
        self._path = path
        self._config = Config()

    @property
    def config(self) -> Config:
        """Return the in-memory configuration."""
        return self._config

    def load(self) -> Config:
        """Load the configuration from disk, writing defaults if the file is absent."""
        if self._path.exists():
            self._config = Config.from_yaml(self._path.read_text(encoding="utf-8"))
            LOGGER.debug("Loaded configuration from %s", self._path)
        else:
            self._config = Config()
            self.save()
            LOGGER.info("Wrote default configuration to %s", self._path)
        return self._config

    def save(self) -> None:
        """
        Persist the current configuration to disk atomically.

        Synchronous on purpose: it is only called from control-plane actions (pairing, a
        settings change, adding/removing a bridge, assigning a TV) and never the streaming
        hot path. The YAML is a few KB, so the write is sub-millisecond and offloading it off
        the event loop would add complexity without a measurable benefit.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self._config.to_yaml()
        data = serialized.encode("utf-8") if isinstance(serialized, str) else serialized
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(self._path)
