from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Protocol
from .models import CommandSpec, EventSpec, PluginManifest, ScheduleSpec, TaskSpec
from .services import PluginServices

class PluginRegistry(Protocol):
    def command(self, spec: CommandSpec) -> None: ...
    def event(self, spec: EventSpec) -> None: ...
    def schedule(self, spec: ScheduleSpec) -> None: ...
    def task(self, spec: TaskSpec) -> None: ...

class Plugin(ABC):
    manifest: PluginManifest
    def __init__(self, services: PluginServices) -> None:
        self.services = services

    @abstractmethod
    def register(self, registry: PluginRegistry) -> None:
        """Declare handlers without executing business IO."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
