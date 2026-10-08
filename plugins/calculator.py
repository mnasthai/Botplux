"""Group calculator command requiring a verified explicit self mention."""

from __future__ import annotations

import re

from wechat_receiver.math_eval import MathEvaluationError, evaluate, format_result

NAME = "calculator"

# WeChat inserts U+2005 in ordinary mentions; some clients use U+200A.  The
# display name is deliberately ignored for authorization: mention_state is the
# normalized atuserlist evidence.  Names may contain ordinary spaces, but a
# mention name cannot cross another @, a line break, or a mention separator.
_LEADING_MENTION = re.compile(r"^@[^@\r\n\u2005\u200a]+[\u2005\u200a]+")
_TRAILING_MENTION = re.compile(
    r"(?:[ \t\r\n\u2005\u200a]+)@[^@\r\n\u2005\u200a]+(?:[\u2005\u200a])?$"
)


def _expression(content: str) -> str | None:
    # Do not use bare str.strip(): U+2005/U+200A are whitespace to Python but
    # are also the protocol-visible terminators of a trailing WeChat mention.
    text = content.strip(" \t\r\n")
    while match := _LEADING_MENTION.match(text):
        text = text[match.end():].strip(" \t\r\n")
    while match := _TRAILING_MENTION.search(text):
        text = text[:match.start()].strip(" \t\r\n")
    if not text.startswith("计算"):
        return None
    rest = text[2:]
    if not rest:
        return None
    stripped = rest.lstrip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped[1:-1].strip()
    return stripped


def on_message(message):
    if message.mention_state != "explicit_self":
        return None
    if not isinstance(message.conversation_id, str) or not message.conversation_id.endswith("@chatroom"):
        return None
    if not isinstance(message.content, str):
        return None
    expression = _expression(message.content)
    if expression is None:
        return None
    try:
        return f"结果：{format_result(evaluate(expression))}"
    except MathEvaluationError as error:
        return f"计算失败：{error}"
