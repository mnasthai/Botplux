from __future__ import annotations

from pathlib import Path
import unittest

from wechat_receiver.models import Message
from wechat_receiver.plugins import ReplyImage, load_plugins


def message(content: str | None, *, mention_state: str = "explicit_self", conversation_id: str | None = "room@chatroom") -> Message:
    return Message(
        session_id="s", event_key="s:1", seq=1, call_id=1, source="receive_batch", event_kind="item",
        observed_at_ms=1, message_type=1, message_kind="text", app_message_type=None, content=content,
        raw_content=content, conversation_id=conversation_id, sender_id="member", direction="incoming",
        message_time_candidate=None, message_id_candidate=None, mentioned_ids=("wxid_bot",),
        mention_state=mention_state, history_state="live_candidate",
    )


class UtilityPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2] / "plugins"
        cls.help, cls.calculator = load_plugins(root, ("help", "calculator"))

    def test_help_matches_only_exact_command_after_outer_whitespace(self) -> None:
        reply = self.help.reply(message(" \n#帮助\t"))
        self.assertEqual(1, len(reply))
        self.assertEqual(self.help.reply(message("#帮助")), reply)
        self.assertIsInstance(reply[0], ReplyImage)
        self.assertEqual("help.png", reply[0].image_path.name)
        self.assertTrue(reply[0].image_path.is_file())
        self.assertEqual((), self.help.reply(message("请看 #帮助")))

    def test_calculator_requires_verified_self_mention_in_a_group(self) -> None:
        content = "@盈月仰角\u2005计算 1+2"
        self.assertEqual(("结果：3",), self.calculator.reply(message(content)))
        self.assertEqual((), self.calculator.reply(message(content, mention_state="explicit_other")))
        self.assertEqual((), self.calculator.reply(message(content, mention_state="none")))
        self.assertEqual((), self.calculator.reply(message(content, conversation_id="wxid_friend")))

    def test_calculator_accepts_trailing_verified_mention_and_braces(self) -> None:
        self.assertEqual(("结果：9",), self.calculator.reply(message("计算 {(1+2)*3} @盈月 仰角\u200a")))
        self.assertEqual(("结果：3",), self.calculator.reply(message("@第一 位\u2005@盈月 仰角\u2005计算 1+2")))
        self.assertEqual(("结果：3",), self.calculator.reply(message("计算 1+2 @第一 位\u2005 @盈月 仰角\u200a")))
        self.assertEqual(("结果：3",), self.calculator.reply(message("计算 1+2\u2005@畜生美国🇺🇸\u2005\u2005@大山烤肉")))
        self.assertEqual(("结果：3",), self.calculator.reply(message("@畜生 美国🇺🇸\u2005\u2005@大山 烤肉\u2005计算 1+2\u2005@畜生 美国🇺🇸\u2005\u2005@大山 烤肉")))
        self.assertEqual((), self.calculator.reply(message("请帮我计算 1+2")))

    def test_calculator_arithmetic_and_scientific_functions(self) -> None:
        self.assertEqual(("结果：512",), self.calculator.reply(message("计算 2^3^2")))
        self.assertEqual(("结果：1",), self.calculator.reply(message("计算 sin(90) + 1e-12")))
        self.assertEqual(("结果：3",), self.calculator.reply(message("计算 sqrt(9)")))
        self.assertEqual(("结果：2.68811714182e+43",), self.calculator.reply(message("计算 exp(100)")))
        self.assertEqual(("结果：1234567890121",), self.calculator.reply(message("计算 123456789012*10+1")))

    def test_calculator_accepts_expression_without_a_command_space(self) -> None:
        for content, expected in (
            ("@一个无感情的bot\u2005计算1+1", "结果：2"),
            ("@一个无感情的bot\u2005计算 1+1", "结果：2"),
            ("计算sqrt(9) @一个无感情的bot\u2005", "结果：3"),
            ("计算(1+2)*3 @一个无感情的bot", "结果：9"),
            ("@一个无感情的bot\u2005计算{1+1}", "结果：2"),
        ):
            with self.subTest(content=content):
                self.assertEqual((expected,), self.calculator.reply(message(content)))
        self.assertEqual((), self.calculator.reply(message(
            "@一个无感情的bot\u2005计算1+1", mention_state="none")))

    def test_calculator_exposes_friendly_degree_calculation_errors(self) -> None:
        for expression, detail in (
            ("tan(90)", "tan 在 90° 的奇数倍处无意义"),
            ("1 / cos(90)", "除数不能为0"),
            ("sqrt(-4)", "函数定义域错误"),
            ("exp(1000)", "函数计算溢出"),
        ):
            reply = self.calculator.reply(message(f"计算 {expression}"))
            self.assertEqual(1, len(reply))
            self.assertTrue(reply[0].startswith("计算失败："), reply)
            self.assertIn(detail, reply[0])

    def test_calculator_returns_friendly_errors_for_unsafe_or_costly_input(self) -> None:
        for expression in (
            "__import__('os').system('x')", "(1).real", "(1,)[0]", "1 / 0", "(-1) ** 0.5",
            "NaN", "sqrt(1e999)", "2 ** 1000", "1+(" * 21 + "1" + ")" * 21, "9" * 201,
        ):
            reply = self.calculator.reply(message(f"计算 {expression}"))
            self.assertEqual(1, len(reply))
            self.assertTrue(reply[0].startswith("计算失败："), reply)


if __name__ == "__main__":
    unittest.main()
