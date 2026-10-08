"""Example reply plugin for a single explicit affection phrase."""

NAME = "affection"


def on_message(message):
    content = message.content
    if isinstance(content, str) and content.strip() == "我喜欢你":
        return "收到你的喜欢啦🙂"
    return None

