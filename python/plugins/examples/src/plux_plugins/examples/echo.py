"""Mirror ordinary chat text, letting the platform policy decide what is answerable."""
from __future__ import annotations

from plux.api import (EventSpec, Outcome, Plugin, PluginContext, PluginManifest,
                      TextMessage)
from .support import respond


class EchoPlugin(Plugin):
    manifest = PluginManifest("echo", capabilities=frozenset({"messages", "delivery"}))

    def register(self, registry) -> None:
        # requires_command_policy=True is what keeps this safe: only live,
        # complete, inbound text with a confirmed sender reaches the handler.
        # Replayed backlog, damaged content and group messages without an
        # explicit self mention are filtered by the runtime, not by the plugin.
        registry.event(EventSpec("echo", self.echo, message_type=TextMessage,
                                 mode="atomic", requires_command_policy=True))

    def echo(self, message: TextMessage, context: PluginContext) -> Outcome:
        text = message.text.strip()
        if not text or text.startswith("/"):
            return Outcome.noop()
        return respond(context, text=f"你说：{text}", suffix="echo")
