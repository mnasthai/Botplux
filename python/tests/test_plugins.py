from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from wechat_receiver.models import Message
from wechat_receiver.plugins import load_plugins


def message(content: str | None) -> Message:
    return Message(
        session_id="session",
        event_key="session:1",
        seq=1,
        call_id=1,
        source="receive_batch",
        event_kind="item",
        observed_at_ms=1,
        message_type=1,
        message_kind="text",
        app_message_type=None,
        content=content,
        raw_content=content,
        conversation_id="wxid_friend",
        sender_id="wxid_friend",
        direction="incoming",
        message_time_candidate=None,
        message_id_candidate=None,
        mentioned_ids=(),
        mention_state="none",
        history_state="live_candidate",
    )


class PluginTests(unittest.TestCase):
    def test_affection_example_matches_only_trimmed_phrase(self) -> None:
        directory = Path(__file__).resolve().parents[2] / "plugins"
        plugin = load_plugins(directory, ("affection",))[0]
        self.assertEqual("affection", plugin.name)
        self.assertEqual(("收到你的喜欢啦🙂",), plugin.reply(message("  我喜欢你\n")))
        self.assertEqual((), plugin.reply(message("我也喜欢你")))
        self.assertEqual((), plugin.reply(message(None)))

    def test_explicit_loading_and_valid_multi_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chosen.py").write_text(
                "NAME = 'chosen'\n"
                "def on_message(message):\n"
                "    return ['第一条', 'second🙂']\n",
                encoding="utf-8",
            )
            (root / "not_loaded.py").write_text("raise RuntimeError('must not load')\n", encoding="utf-8")
            plugin = load_plugins(root, ("chosen",))[0]
            self.assertEqual(("第一条", "second🙂"), plugin.reply(message("anything")))

    def test_invalid_result_is_rejected_as_a_whole_and_exceptions_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "invalid.py").write_text(
                "NAME = 'invalid'\n"
                "def on_message(message):\n"
                "    return ['valid first', 'bad\\0reply']\n",
                encoding="utf-8",
            )
            (root / "failing.py").write_text(
                "NAME = 'failing'\n"
                "def on_message(message):\n"
                "    raise RuntimeError('plugin failure')\n",
                encoding="utf-8",
            )
            invalid, failing = load_plugins(root, ("invalid", "failing"))
            with self.assertRaisesRegex(ValueError, "contains NUL"):
                invalid.reply(message("anything"))
            with self.assertRaisesRegex(RuntimeError, "plugin failure"):
                failing.reply(message("anything"))

    def test_load_contract_and_reply_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "wrong.py").write_text("NAME = 'different'\ndef on_message(message): return None\n",
                                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must declare NAME"):
                load_plugins(root, ("wrong",))

            (root / "bounded.py").write_text(
                "NAME = 'bounded'\n"
                "def on_message(message):\n"
                "    return ['a', 'b', 'c', 'd']\n",
                encoding="utf-8",
            )
            plugin = load_plugins(root, ("bounded",))[0]
            with self.assertRaisesRegex(ValueError, "more than 3"):
                plugin.reply(message("anything"))

            (root / "echo.py").write_text(
                "NAME = 'echo'\n"
                "def on_message(message):\n"
                "    return message.content\n",
                encoding="utf-8",
            )
            echo = load_plugins(root, ("echo",))[0]
            exact = "🙂" * 4096
            self.assertEqual((exact,), echo.reply(message(exact)))
            with self.assertRaisesRegex(ValueError, "exceeds 16384"):
                echo.reply(message(exact + "a"))


if __name__ == "__main__":
    unittest.main()
