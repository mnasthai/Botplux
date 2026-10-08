from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from wechat_receiver.games import renderer


class GameRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.templates = self.root / "templates"
        self.components = self.root / "components"
        self.output = self.root / "output"
        self.templates.mkdir()
        self.components.mkdir()
        for name, color in (
            ("bg_profile.png", (12, 42, 40, 255)),
            ("bg_ranking.png", (15, 38, 37, 255)),
            ("bg_duel.png", (22, 40, 37, 255)),
        ):
            Image.new("RGBA", (1200, 1600), color).save(self.templates / name)
        self.patches = (
            patch.object(renderer, "TEMPLATES_DIR", self.templates),
            patch.object(renderer, "LEGACY_TEMPLATES_DIR", self.templates),
            patch.object(renderer, "COMP_DIR", self.components),
            patch.object(renderer, "PROFILE_TEMPLATE", self.templates / "bg_profile.png"),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_fit_and_wrap_use_pixel_width_without_losing_unlimited_text(self) -> None:
        fitted, font, size = renderer._fit_text(
            "太上玄妙无穷无尽非常非常长的修士道号◇终",
            180,
            preferred_size=38,
            minimum_size=25,
        )
        self.assertLessEqual(renderer._measure(fitted, font), 180)
        self.assertGreaterEqual(size, 25)
        self.assertTrue(fitted.endswith("…"))

        original = "青山之外有青山，云海尽头仍是云海。"
        lines = renderer._wrap_text(original, renderer._font(renderer.BODY_FONTS, 28), 120)
        self.assertGreater(len(lines), 1)
        self.assertEqual(original, "".join(lines))
        self.assertTrue(all(renderer._measure(line, renderer._font(renderer.BODY_FONTS, 28)) <= 120 for line in lines))

    def test_rule_mapping_controls_breakthrough_threshold(self) -> None:
        rules = {
            "breakthrough_cultivation": (321, 654, 987),
            "breakthrough_stones": (123, 456, 789),
        }
        self.assertEqual((321, 123), renderer._cultivation_target("qi", rules))
        self.assertEqual((654, 456), renderer._cultivation_target("foundation", rules))
        self.assertEqual((None, None), renderer._cultivation_target("nascent", rules))

    def test_profile_shows_discounted_costs_and_all_eight_artifacts(self) -> None:
        ids = ("qingfeng_jian", "qiankun_ding", "fuhai_yin", "xuanyuan_jing", "xunling_pan",
               "qinglian_deng", "shanhe_tu", "tianlei_gu")
        inventory = [{"template_id": key} for key in ids]
        with patch.object(ImageDraw.ImageDraw, "text", autospec=True,
                          side_effect=ImageDraw.ImageDraw.text) as text:
            renderer.render_profile_card(
                {"dao_name": "折扣修士", "realm": "qi", "cultivation": 90, "spirit_stones": 180},
                inventory, {"mine_remaining": 61},
                rules={"breakthrough_cultivation": (100, 250, 500),
                       "breakthrough_stones": (200, 300, 600)}, output_dir=self.output)
        labels = [str(call.args[2]) for call in text.call_args_list]
        self.assertIn("8 / 8 件", labels)
        for key in ids:
            self.assertIn(renderer.CATALOG[key].name, labels)
        self.assertIn("扩容 · 不加攻击", labels)
        self.assertTrue(any("可突破至筑基" in value and "修为 90 · 灵石 180" in value for value in labels))
        self.assertIn("50 灵石 · #秘境", labels)
        self.assertIn("2 分钟后", labels)

    def test_profile_preserves_warnings_and_does_not_offer_unaffordable_exploration(self) -> None:
        inventory = [{"template_id": "qingfeng_jian"}] * 9
        with patch.object(ImageDraw.ImageDraw, "text", autospec=True,
                          side_effect=ImageDraw.ImageDraw.text) as text:
            renderer.render_profile_card(
                {"dao_name": "待整理修士", "realm": "qi", "cultivation": 0, "spirit_stones": 0,
                 "devil_contract_tier": 2}, inventory,
                {"debuffs": [{"debuff_kind": "duanmai_san"}], "duel_status": "D123 · 等待应战"},
                output_dir=self.output)
        labels = [str(call.args[2]) for call in text.call_args_list]
        self.assertIn("灵石不足", labels)
        for phrase in ("超限 3", "另 1 件", "断脉散", "D123", "魔契 2 层"):
            self.assertTrue(any(phrase in value for value in labels), phrase)

    def test_output_names_are_safe_unique_and_canvas_is_standard_size(self) -> None:
        player = {
            "player_id": "../../绝不能进入文件名",
            "dao_name": "月下客",
            "realm": "qi",
            "cultivation": 42,
            "spirit_stones": 88,
        }
        daily = {"cultivated": False, "explored": True, "self_cultivate_remaining": 61}
        first = renderer.render_profile_card(player, [], daily, output_dir=self.output)
        second = renderer.render_profile_card(player, [], daily, output_dir=self.output)
        self.assertNotEqual(first.name, second.name)
        self.assertRegex(first.name, r"^profile_[0-9a-f]{32}\.png$")
        self.assertNotIn("player", first.name)
        self.assertNotIn("绝不能", first.name)
        with Image.open(first) as image:
            self.assertEqual((1080, 1440), image.size)

    def test_ranking_renders_ten_rows_and_empty_state(self) -> None:
        rows = [
            {"dao_name": f"修士{index}", "realm": "qi", "cultivation": 1000 - index}
            for index in range(12)
        ]
        with patch.object(renderer, "_component", wraps=renderer._component) as component:
            full = renderer.render_ranking_card(rows, output_dir=self.output)
        rank_calls = [call.args[0] for call in component.call_args_list if call.args[0].startswith("rank_")]
        self.assertEqual(10, len(rank_calls))
        self.assertTrue(full.is_file())
        self.assertIn("前 10 名", renderer._ranking_note(12, None))
        self.assertIn("前 10 名", renderer._ranking_note(10, 51))
        self.assertNotIn("共", renderer._ranking_note(8, 0))

        empty = renderer.render_ranking_card([], output_dir=self.output)
        self.assertTrue(empty.is_file())
        self.assertIn("榜单暂空", renderer._ranking_note(0, None))

    def test_duel_is_byte_stable_and_timeout_copy_does_not_claim_lightning_defeat(self) -> None:
        duel = {"duel_id": "../../D-危险", "total_rounds": 7}
        kwargs = {
            "cultivation_loss": 13,
            "support_text": "围观支持已原额退还。\n青竹客：支持 20｜到账 20（收回本金）",
            "reason": "timeout",
            "output_dir": self.output,
        }
        first = renderer.render_duel_card(duel, "胜者", "败者", None, **kwargs)
        second = renderer.render_duel_card(duel, "胜者", "败者", None, **kwargs)
        self.assertRegex(first.name, r"^duel_[0-9a-f]{32}\.png$")
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        timeout_tag = renderer._duel_outcome_tag("timeout")
        self.assertIn("超时", timeout_tag)
        self.assertNotIn("天雷", timeout_tag)
        self.assertNotIn("轰", timeout_tag)

    def test_long_support_settlement_is_preserved_across_columns(self) -> None:
        text = "\n".join(f"修士{index}：支持 {index * 10}｜到账 {index * 12}" for index in range(1, 13))
        size, columns = renderer._support_layout(text)
        self.assertGreaterEqual(size, 25)
        self.assertEqual(
            text.replace("\n", ""),
            "".join(line for column in columns for line in column),
        )
        self.assertTrue(all(len(column) <= 7 for column in columns))

        oversized = "\n".join(f"第{index}位修士的支持结算内容很长需要完整保留" for index in range(30))
        with self.assertRaisesRegex(ValueError, "too long"):
            renderer._support_layout(oversized)

    def test_duel_next_turn_is_labeled_as_current_round_not_total_rounds(self) -> None:
        self.assertIn("第 4 轮结算", renderer._duel_meta({"duel_id": "D1", "next_turn": 4}))
        self.assertNotIn("共 4 回合", renderer._duel_meta({"duel_id": "D1", "next_turn": 4}))


if __name__ == "__main__":
    unittest.main()
