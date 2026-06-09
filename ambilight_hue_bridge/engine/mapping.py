"""Maps virtual-light colors onto real entertainment channels."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hue_entertainment import LightColorCommand

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ambilight_hue_bridge.config.models import VirtualLight

    from .ingest import ColorBuffer


def map_to_commands(lights: Iterable[VirtualLight], buffer: ColorBuffer) -> list[LightColorCommand]:
    """
    Build per-channel color commands for the active entertainment area.

    Each virtual light paints all of its mapped channels with the same color.

    :param lights: The configured virtual lights.
    :param buffer: The latest-color buffer.
    """
    commands: list[LightColorCommand] = []
    for light in lights:
        red, green, blue = buffer.get_color(light.id)
        commands.extend(
            LightColorCommand(channel_id=channel, red=red, green=green, blue=blue)
            for channel in light.channels
        )
    return commands
