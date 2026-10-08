"""Send a periodic summary of recent chat from a scheduled background task."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from plux.api import (CommandSpec, ConfigurationError, HistoryQuery, Outcome, Plugin,
                      PluginContext, PluginManifest, ReplyIntent, ScheduleSpec,
                      TaskIntent, TaskSpec, utc_now)


def _digest_config(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if set(raw) - {"conversation", "limit", "window_seconds"}:
        raise ConfigurationError("digest config supports conversation, limit and window_seconds")
    conversation = raw.get("conversation")
    if not isinstance(conversation, str) or not 0 < len(conversation) <= 128:
        raise ConfigurationError("digest requires a target conversation ID")
    limit = raw.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ConfigurationError("digest limit must be an integer in 1..200")
    window = raw.get("window_seconds", 3600)
    if type(window) is not int or not 60 <= window <= 86400:
        raise ConfigurationError("digest window_seconds must be an integer in 60..86400")
    return {"conversation": conversation, "limit": limit, "window_seconds": window}


class DigestPlugin(Plugin):
    manifest = PluginManifest(
        "digest",
        capabilities=frozenset({"messages", "delivery", "tasks", "schedules", "history"}),
        config_validator=_digest_config)

    def register(self, registry) -> None:
        registry.task(TaskSpec("digest", self.work, self.commit, idempotent=True, max_attempts=2))
        registry.schedule(ScheduleSpec("hourly", "digest", 3600, missed_policy="skip"))
        registry.command(CommandSpec("digest", "/digest", self.digest, mode="atomic"))

    def digest(self, argument: str, context: PluginContext) -> Outcome:
        # Accepting the work is one transaction; the work itself runs outside it.
        return Outcome.success(tasks=(TaskIntent(f"digest:manual:{context.event_key}", "digest", None),))

    def work(self, payload: Any, context: PluginContext) -> Any:
        """Runs without a transaction, so read and compute here.

        The native session is read now rather than captured earlier: a reply
        bound to a retired session is refused instead of being sent twice.
        """
        config = self.services.config
        connection = self.services.messages.connection()
        if not connection.account or not connection.native_session:
            return {"deliverable": False, "reason": "native_session_unavailable"}
        page = self.services.messages.history(
            HistoryQuery(connection.account, config["conversation"], config["limit"]))
        cutoff = utc_now() - timedelta(seconds=config["window_seconds"])
        recent = [item for item in page.items
                  if item.observed_at >= cutoff and item.identity.direction == "inbound"]
        members = {item.identity.actor for item in recent if item.identity.actor}
        minutes = config["window_seconds"] // 60
        return {"deliverable": True, "account": connection.account,
                "native_session": connection.native_session,
                "text": f"最近 {minutes} 分钟：{len(recent)} 条消息，{len(members)} 位成员参与"}

    def commit(self, result: Any, context: PluginContext) -> Outcome:
        if not result.get("deliverable"):
            # Committing a rejection closes the task; it is never retried.
            return Outcome.rejected(result.get("reason", "digest_unavailable"))
        return Outcome.success(replies=(ReplyIntent(
            f"{context.event_key}:digest", result["account"],
            self.services.config["conversation"], result["native_session"],
            text=result["text"]),))
