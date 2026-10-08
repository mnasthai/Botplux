"""Reply with a packaged image through the asset pipeline."""
from __future__ import annotations

from pathlib import Path

from plux.api import CommandSpec, Outcome, Plugin, PluginContext, PluginManifest
from .support import respond

_IMAGE = Path(__file__).resolve().parent / "assets" / "poster.png"


class PosterPlugin(Plugin):
    manifest = PluginManifest(
        "poster", capabilities=frozenset({"messages", "delivery", "assets"}),
        resources=(_IMAGE,))

    def __init__(self, services) -> None:
        super().__init__(services)
        self.asset = None

    def start(self) -> None:
        # Staging and publication are refused inside a write transaction, so the
        # image is published once here and every reply reuses the same AssetRef.
        assets = self.services.data.assets
        self.asset = assets.publish(assets.stage(_IMAGE, kind="image"))

    def register(self, registry) -> None:
        registry.command(CommandSpec("poster", "/poster", self.poster, mode="atomic"))

    def poster(self, argument: str, context: PluginContext) -> Outcome:
        if self.asset is None:
            return Outcome.rejected("asset_unavailable")
        return respond(context, asset=self.asset, suffix="poster")
