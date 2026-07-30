"""Stable metadata boundary for optional device integrations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class PluginDescriptor:
    plugin_id: str
    version: int
    device_type: str
    capabilities: tuple[str, ...]
    authoritative_for: tuple[str, ...] = ()
    supplements: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class DevicePlugin(Protocol):
    """Minimum interface implemented by every in-process device plugin."""

    descriptor: PluginDescriptor

    def run(self, stop) -> None:
        """Run until the shared stop event is set."""


class PluginRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, PluginDescriptor] = {}

    def register(self, descriptor: PluginDescriptor) -> None:
        if descriptor.version != 1:
            raise ValueError("unsupported device plugin interface version")
        if descriptor.plugin_id in self._descriptors:
            raise ValueError(f"duplicate plugin id: {descriptor.plugin_id}")
        self._descriptors[descriptor.plugin_id] = descriptor

    def descriptors(self) -> list[dict[str, object]]:
        return [
            value.as_dict()
            for _, value in sorted(self._descriptors.items())
        ]
