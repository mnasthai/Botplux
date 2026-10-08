"""Persist per-conversation votes in a conditionally updated state snapshot."""
from __future__ import annotations

from typing import Any

from plux.api import (CommandSpec, Outcome, Plugin, PluginContext, PluginManifest,
                      StateSnapshot)
from .support import reject, respond

_MAX_CHOICE = 32


class PollPlugin(Plugin):
    manifest = PluginManifest(
        "poll", capabilities=frozenset({"messages", "delivery", "snapshots", "transactions"}))

    def register(self, registry) -> None:
        registry.command(CommandSpec("vote", "/vote", self.vote, mode="atomic"))
        # A read-only command does not need the handler transaction.
        registry.command(CommandSpec("poll", "/poll", self.poll, mode="stateless"))

    @staticmethod
    def _key(context: PluginContext) -> str:
        return f"poll:{context.conversation}"

    def _state(self, context: PluginContext) -> tuple[StateSnapshot | None, dict[str, Any]]:
        snapshot = self.services.data.snapshots.get(self._key(context), context.uow)
        if snapshot is None:
            return None, {"options": {}, "voters": {}}
        data = dict(snapshot.data)
        return snapshot, {"options": dict(data.get("options") or {}),
                          "voters": dict(data.get("voters") or {})}

    def vote(self, argument: str, context: PluginContext) -> Outcome:
        choice = argument.strip()
        if not choice or len(choice) > _MAX_CHOICE:
            return reject(context, "invalid_choice", text=f"用法：/vote <选项>（不超过 {_MAX_CHOICE} 字）")
        snapshot, state = self._state(context)
        voter = context.actor or context.conversation
        previous = state["voters"].get(voter)
        counts = state["options"]
        if previous is not None:
            counts[previous] = max(0, counts.get(previous, 1) - 1)
        counts[choice] = counts.get(choice, 0) + 1
        state["voters"][voter] = choice
        # expected_revision turns a lost update into a ConflictError instead of
        # silently overwriting another writer's vote.
        self.services.data.snapshots.put(
            StateSnapshot(self._key(context), 1, snapshot.revision if snapshot else 0, state),
            expected_revision=snapshot.revision if snapshot else None, uow=context.uow)
        return respond(context, text=f"已记录：{choice}", result=choice)

    def poll(self, argument: str, context: PluginContext) -> Outcome:
        _, state = self._state(context)
        counts = state["options"]
        if not counts:
            return respond(context, text="还没有人投票，使用 /vote <选项> 开始")
        lines = [f"{name}：{count}" for name, count
                 in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
        return respond(context, text="\n".join(lines))
