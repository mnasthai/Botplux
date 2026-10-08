"""Reply helpers shared by the example plugins.

A handler must never raise for an environmental reason: a failed handler keeps
its input pending, so a deterministic failure would be retried on every cycle.
These helpers therefore report "no live conversation" as a rejected outcome
instead of an exception.
"""
from __future__ import annotations

from plux.api import Outcome, PluginContext, ReplyIntent


def reply(context: PluginContext, *, text: str | None = None, asset: object | None = None,
          mentions: tuple = (), quote: object | None = None,
          suffix: str = "reply") -> ReplyIntent | None:
    """Build a reply for the conversation that produced this call.

    Returns None when the call has no account, conversation or native session to
    answer — for example a task call, or an observer that is not connected.
    """
    if not context.account or not context.conversation or not context.native_session:
        return None
    return ReplyIntent(
        f"{context.event_key}:{suffix}", context.account, context.conversation,
        context.native_session, text=text, asset=asset, mentions=tuple(mentions),
        quote=quote, source_event_key=context.event_key)


def respond(context: PluginContext, *, text: str | None = None, asset: object | None = None,
            mentions: tuple = (), quote: object | None = None, result: object = None,
            suffix: str = "reply") -> Outcome:
    """Answer with exactly one reply, or a rejection when that is impossible."""
    intent = reply(context, text=text, asset=asset, mentions=mentions, quote=quote, suffix=suffix)
    if intent is None:
        return Outcome.rejected("native_session_unavailable")
    return Outcome.success(result, replies=(intent,))


def reject(context: PluginContext, code: str, *, text: str | None = None,
           suffix: str = "reject") -> Outcome:
    """Reject the business call, answering the sender when the channel is live."""
    if text is None:
        return Outcome.rejected(code)
    intent = reply(context, text=text, suffix=suffix)
    return Outcome.rejected(code) if intent is None else Outcome.rejected(code, replies=(intent,))
