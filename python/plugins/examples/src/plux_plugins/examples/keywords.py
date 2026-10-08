"""Answer whole-message keywords from a versioned content catalog."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from plux.api import (CatalogSpec, ConfigurationError, EventSpec, Outcome, Plugin,
                      PluginContext, PluginManifest, TextMessage)
from .support import respond

# The catalog source path must be absolute: a relative default would resolve
# against the process working directory, not against this package.
_CONTENT = Path(__file__).resolve().parent / "content" / "keywords.toml"


def _rules(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"rules"}:
        raise ConfigurationError("keyword content requires exactly one rules table")
    rules = raw["rules"]
    if not isinstance(rules, Mapping) or not rules:
        raise ConfigurationError("keyword rules must be a non-empty table")
    for trigger, answer in rules.items():
        if not isinstance(trigger, str) or not 0 < len(trigger) <= 64:
            raise ConfigurationError("a keyword must be a string of 1..64 characters")
        if not isinstance(answer, str) or not 0 < len(answer) <= 1024:
            raise ConfigurationError("a keyword answer must be a string of 1..1024 characters")
    return raw


class KeywordPlugin(Plugin):
    manifest = PluginManifest(
        "keywords", capabilities=frozenset({"messages", "delivery", "catalogs"}),
        catalogs=(CatalogSpec("keywords", "1", _CONTENT, validator=_rules),),
        resources=(_CONTENT,))

    def __init__(self, services) -> None:
        super().__init__(services)
        self.rules: dict[str, str] = {}
        self.version: str | None = None

    def start(self) -> None:
        # Catalogs are published and validated before start(), so the rules are
        # frozen in memory here and never re-read per message.
        snapshot = self.services.data.catalogs.get("keywords")
        self.rules = dict(snapshot.data["rules"])
        self.version = snapshot.ref.version

    def register(self, registry) -> None:
        registry.event(EventSpec("keywords", self.answer, message_type=TextMessage,
                                 mode="atomic", requires_command_policy=True))

    def answer(self, message: TextMessage, context: PluginContext) -> Outcome:
        text = self.rules.get(message.text.strip())
        if text is None:
            return Outcome.noop()
        return respond(context, text=text, suffix=f"keyword:{self.version}")
