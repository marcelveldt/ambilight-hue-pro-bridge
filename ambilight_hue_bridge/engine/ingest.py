"""Latest-state-wins buffer of the target color per virtual light."""

from __future__ import annotations


class ColorBuffer:
    """Holds the most recent target RGB (16-bit) for each virtual light."""

    def __init__(self) -> None:
        """Initialize an empty buffer."""
        self._colors: dict[str, tuple[int, int, int]] = {}

    def set_color(self, light_id: str, rgb: tuple[int, int, int]) -> None:
        """
        Record the latest target color for a light.

        :param light_id: The virtual light id.
        :param rgb: 16-bit RGB tuple (0-65535 per channel).
        """
        self._colors[light_id] = rgb

    def get_color(self, light_id: str) -> tuple[int, int, int]:
        """Return the latest color for a light, or black if unset."""
        return self._colors.get(light_id, (0, 0, 0))
