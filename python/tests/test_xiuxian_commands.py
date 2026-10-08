from __future__ import annotations

from collections import Counter
from dataclasses import replace
from types import MappingProxyType
import unittest

from wechat_receiver.games.catalog import CATALOG, RARITY_NAMES, REALM_NAMES, ArtifactTemplate
from wechat_receiver.games.commands import Command, parse_command
from wechat_receiver.models import Message


def message(content: object, *, group: bool = True, mention_state: str = "none",
            mentioned_ids: tuple[str, ...] = ("wxid_bot",)) -> Message:
    return Message(
        session_id="s", event_key="s:1", seq=1, call_id=1, source="receive_batch", event_kind="item",
        observed_at_ms=1, message_type=1, message_kind="text", app_message_type=None, content=content,
        raw_content=content if isinstance(content, str) else None,
        conversation_id="room@chatroom" if group else "wxid_friend", sender_id="member", direction="incoming",
        message_time_candidate=None, message_id_candidate=None, mentioned_ids=mentioned_ids,
        mention_state=mention_state, history_state="live_candidate",
    )


class CatalogTests(unittest.TestCase):
    def test_complete_immutable_catalog_has_expected_rarities(self) -> None:
        self.assertIsInstance(CATALOG, MappingProxyType)
        self.assertEqual(36, len(CATALOG))
        self.assertEqual(Counter(template.rarity for template in CATALOG.values()),
                         {"artifact": 14, "spirit": 9, "ancient": 8, "treasure": 5})
        self.assertEqual(set(RARITY_NAMES), {"artifact", "spirit", "ancient", "treasure"})
        self.assertEqual(set(REALM_NAMES), {"qi", "foundation", "core", "nascent"})
        for template_id, template in CATALOG.items():
            self.assertIsInstance(template, ArtifactTemplate)
            self.assertEqual(template_id, template.id)
            with self.assertRaises(AttributeError):
                template.name = "改名"
        with self.assertRaises(TypeError):
            CATALOG["new"] = CATALOG["qingfeng_jian"]


class CommandParserTests(unittest.TestCase):
    def test_all_supported_commands(self) -> None:
        cases = {
            "#修仙帮助": Command("help"),
            "#新手帮助": Command("beginner_help"),
            "#修炼帮助": Command("cultivation_help"),
            "#秘境帮助": Command("exploration_help"),
            "#道具帮助": Command("props_help"),
            "#斗法帮助": Command("lightning_help"),
            "#修仙": Command("profile"),
            "#修仙 青玄": Command("profile", "青玄"),
            "#修炼": Command("cultivate"),
            "#自主修炼": Command("self_cultivate"),
            "#突破": Command("breakthrough"),
            "#秘境": Command("explore"),
            "#法宝": Command("inventory"),
            "#法宝 f0a1b2c3": Command("inventory", "F0A1B2C3"),
            "#法宝帮助": Command("artifact_help"),
            "#法宝 帮助": Command("artifact_help"),
            "#法宝效果": Command("artifact_help"),
            "#法宝 效果": Command("artifact_help"),
            "#法宝图鉴": Command("artifact_help"),
            "#法宝神通": Command("artifact_help"),
            "#法宝 help": Command("artifact_help"),
            "#宝录": Command("book"),
            "#献宝 FABC": Command("offer", "FABC"),
            "#仙榜": Command("ranking"),
            "#接受斗法": Command("accept"),
            "#拒绝斗法": Command("reject"),
            "#取消斗法": Command("cancel"),
            "#引雷": Command("lightning"),
            "#斗法状态": Command("duel_status"),
            "#战绩": Command("history"),
            "#战绩 d0a1b2c3d 7": Command("history", "D0A1B2C3D", page=7),
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertEqual(expected, parse_command(message(f" \n{content}\t ")))

    def test_usage_and_item_identifier_boundaries(self) -> None:
        cases = {
            "#修仙帮助 更多": "#修仙帮助",
            "#新手帮助 更多": "#新手帮助",
            "#修炼帮助 更多": "#修炼帮助",
            "#秘境帮助 更多": "#秘境帮助",
            "#道具帮助 更多": "#道具帮助",
            "#斗法帮助 更多": "#斗法帮助",
            "#修炼 多一次": "#修炼",
            "#突破 x": "#突破",
            "#秘境 1": "#秘境",
            "#宝录 x": "#宝录",
            "#仙榜 x": "#仙榜",
            "#法宝帮助 更多": "#法宝帮助",
            "#法宝 BAD": "#法宝 [F编号]",
            "#法宝 F1": "#法宝 [F编号]",
            "#法宝 F" + "A" * 32: "#法宝 [F编号]",
            "#献宝": "#献宝 F编号",
            "#献宝 F-123": "#献宝 F编号",
            "#接受斗法 多余": "#接受斗法",
            "#斗法状态 x": "#斗法状态",
            "#战绩 2": "#战绩 [D编号 [页码]]",
            "#战绩 D0A1B2C3D 0": "#战绩 [D编号 [页码]]",
            "#战绩 D0A1B2C3D 100001": "#战绩 [D编号 [页码]]",
            "#战绩 D0A1B2C3D " + "9" * 5000: "#战绩 [D编号 [页码]]",
        }
        for content, usage in cases.items():
            with self.subTest(content=content):
                self.assertEqual(Command("usage", usage), parse_command(message(content)))
        for content in ("#修炼abc", "#法宝F123", "#修仙青玄", "聊天 #修炼", "#斗法abc",
                        "#新手帮助更多", "#修炼帮助更多", "#秘境帮助更多", "#道具帮助更多",
                        "#斗法帮助更多"):
            with self.subTest(content=content):
                self.assertIsNone(parse_command(message(content)))

    def test_only_group_text_and_verified_mentions_are_accepted(self) -> None:
        self.assertIsNone(parse_command(message("#修炼", group=False)))
        for content in ("#新手帮助", "#修炼帮助", "#秘境帮助", "#道具帮助", "#斗法帮助"):
            with self.subTest(content=content):
                self.assertIsNone(parse_command(message(content, group=False)))
        self.assertIsNone(parse_command(replace(message("#修炼"), message_kind="image")))
        self.assertIsNone(parse_command(message(None)))
        self.assertIsNone(parse_command(message(123)))
        self.assertEqual(Command("cultivate"), parse_command(message("@机器人\u2005#修炼", mention_state="explicit_self")))
        self.assertEqual(Command("cultivate"), parse_command(message("#修炼 @机器人\u200a", mention_state="explicit_self")))
        self.assertEqual(Command("cultivate"), parse_command(message("@甲\u2005#修炼 @机器人\u2005", mention_state="explicit_self")))
        self.assertIsNone(parse_command(message("@机器人\u2005#修炼", mention_state="explicit_other")))
        self.assertIsNone(parse_command(message("@机器人\u2005#修炼", mention_state="unknown")))

    def test_challenge_requires_exactly_one_verified_member_mention(self) -> None:
        target = ("wxid_target",)
        self.assertEqual(Command("challenge", target_id="wxid_target"), parse_command(
            message("@名字 有 空格\u2005#斗法", mention_state="explicit_other", mentioned_ids=target)))
        self.assertEqual(Command("challenge", target_id="wxid_target"), parse_command(
            message("#斗法 @名字 有 空格\u200a", mention_state="explicit_other", mentioned_ids=target)))
        for content, state, ids in (
            ("#斗法", "none", ()),
            ("@名字\u2005#斗法", "unknown", target),
            ("@机器人\u2005#斗法", "explicit_self", ("wxid_bot",)),
            ("@机器人\u2005#斗法 @群友\u2005", "explicit_self", ("wxid_bot", "wxid_target")),
            ("@甲\u2005#斗法 @乙\u2005", "explicit_other", ("wxid_one", "wxid_two")),
            ("@群友\u2005#斗法 多余", "explicit_other", target),
        ):
            with self.subTest(content=content, state=state):
                self.assertEqual(Command("usage", "#斗法 @群友"), parse_command(
                    message(content, mention_state=state, mentioned_ids=ids)))

    def test_support_requires_one_verified_mention_and_bounded_integer_amount(self) -> None:
        for content in ('#支持 @群友\u2005 50', '@群友\u2005#支持 50', '#支持 50 @群友\u2005'):
            self.assertEqual(Command('support', target_id='wxid_target', amount=50), parse_command(
                message(content, mention_state='explicit_other', mentioned_ids=('wxid_target',))))
        for content, state, ids in (
            ('#支持 @群友 50', 'none', ()),
            ('#支持 @群友\u2005 50', 'explicit_self', ('wxid_bot',)),
            ('#支持 @群友\u2005 50', 'explicit_other', ('wxid_one', 'wxid_two')),
            ('#支持 @群友\u2005 -5', 'explicit_other', ('wxid_target',)),
            ('#支持 @群友\u2005 1.5', 'explicit_other', ('wxid_target',)),
            ('#支持 @群友\u2005 0', 'explicit_other', ('wxid_target',)),
            ('#支持 @群友\u2005 ' + '9' * 5000, 'explicit_other', ('wxid_target',)),
        ):
            with self.subTest(content=content[:80]):
                self.assertEqual('usage', parse_command(message(content, mention_state=state, mentioned_ids=ids)).kind)


if __name__ == "__main__":
    unittest.main()
