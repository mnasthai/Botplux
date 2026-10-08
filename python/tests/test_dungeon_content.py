"""Catalog integrity and numeric precedence for the tower dungeon."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

# Load the catalog on its own so these tests can run while a sibling service
# module is under development; the package import is exercised by integration.
_path = Path(__file__).parents[1] / "src/wechat_receiver/games/dungeon/content.py"
_spec = importlib.util.spec_from_file_location("dungeon_content_under_test", _path)
assert _spec and _spec.loader
_content = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_content)
BOSSES = _content.BOSSES
CLASSES = _content.CLASSES
ENEMIES = _content.ENEMIES
EQUIPMENT = _content.EQUIPMENT
EXPLORATION_NODES = _content.EXPLORATION_NODES
OPEN_ROOTS = _content.OPEN_ROOTS
ROOTS = _content.ROOTS
RULES = _content.RULES
RULE_TEXT = _content.RULE_TEXT
STARTER_ROOTS = _content.STARTER_ROOTS
UPGRADES = _content.UPGRADES
validate_catalog = _content.validate_catalog
available_upgrades = _content.available_upgrades


class DungeonContentTests(unittest.TestCase):
    def test_catalog_is_complete_and_references_are_valid(self) -> None:
        validate_catalog()
        self.assertEqual(
            (len(CLASSES), len(ROOTS), len(ENEMIES), len(BOSSES),
             len(UPGRADES), len(EQUIPMENT), len(EXPLORATION_NODES)),
            (8, 30, 24, 6, 28, 6, 6),
        )
        self.assertEqual(set(OPEN_ROOTS), set(ROOTS))
        self.assertEqual(STARTER_ROOTS, ("R01", "R07", "R13", "R19", "R25"))
        self.assertEqual({entry["slot"] for entry in EQUIPMENT.values()}, {"weapon", "armor"})

    def test_current_balance_overrides_older_numeric_draft(self) -> None:
        self.assertEqual(RULES["guard_reduction"], 0.70)
        self.assertEqual(RULES["damage_reduction_cap"], 0.80)
        self.assertEqual(CLASSES["C01"]["base_hp"], 800)
        self.assertEqual(CLASSES["C01"]["base_atk"], 140)
        self.assertEqual(CLASSES["C01"]["growth_hp"], 0.14)
        self.assertEqual(CLASSES["C03"]["basic_multiplier"], 0.75)
        self.assertEqual(CLASSES["C04"]["basic_kind"], "heal")
        self.assertIsNone(CLASSES["C06"]["skill_cd"])
        self.assertEqual((ENEMIES["M13"]["hp"], ENEMIES["M13"]["attack"]), (250, 110))
        self.assertEqual((ENEMIES["M21"]["hp"], ENEMIES["M21"]["attack"]), (2800, 150))
        for boss_id, boss in BOSSES.items():
            self.assertEqual((boss["hp"], boss["attack"], boss["defense"]),
                             (3000 if boss_id == "B03" else 3360, 180, 90))
        self.assertEqual((ENEMIES["M05"]["hp"], ENEMIES["M05"]["atk"],
                          ENEMIES["M05"]["defense"]), (800, 260, 65))
        self.assertEqual(RULES["day_enemy_scaling"][2],
                         {"hp": 1.75, "atk": 1.45, "defense": 15})
        self.assertEqual(set(RULES["day_enemy_scaling"]), {1, 2})
        self.assertEqual(RULES["enemy_hp_party_multipliers"], (1.0, 2.35, 3.80))
        self.assertEqual(RULES["enemy_atk_party_multipliers"], (1.0, 1.30, 1.60))

    def test_class_nerfs_apply_to_base_and_growth_without_flattening_roles(self) -> None:
        old_stats = ((1000, 200, 100), (1250, 170, 110), (900, 225, 90),
                     (1050, 225, 90), (950, 160, 95), (900, 230, 85),
                     (1100, 180, 105), (1050, 220, 85))
        for entry, (hp, atk, defense) in zip(CLASSES.values(), old_stats):
            self.assertLess(entry['base_hp'], hp)
            self.assertLess(entry['base_atk'], atk)
            for level in (1, 5, 9):
                actual = entry['base_defense'] + entry['growth_defense'] * (level - 1)
                self.assertAlmostEqual(actual, (defense + 5 * (level - 1)) * .85)
        for class_id in ('C03', 'C06'):
            self.assertLess(CLASSES[class_id]['growth_hp'], .10)
        for class_id in ('C01', 'C02'):
            self.assertLess(CLASSES[class_id]['growth_atk'], .08)
        self.assertLessEqual(CLASSES['C04']['base_atk'], 225 * .4)
        self.assertEqual(CLASSES['C04']['low_hp_bonus_atk_cap'], .25)

    def test_two_days_reward_combat_experience_more_than_supply(self) -> None:
        self.assertEqual((RULES['days'], RULES['nodes_per_day'], RULES['nights']), (2, 6, 3))
        thresholds = RULES['experience_thresholds']
        xp = RULES['node_experience']
        level = lambda total: sum(total >= threshold for threshold in thresholds)
        self.assertGreater(level(xp['camp'] * 6), level(xp['spring'] * 6))
        self.assertGreater(level(xp['elite'] * 6), level(xp['camp'] * 6))
        self.assertEqual(level(xp['camp'] * 8 + xp['spring'] * 4), RULES['max_level'])

    def test_strong_moves_preview_and_boss_mechanics_remain_distinct(self) -> None:
        for enemy in (*ENEMIES.values(), *BOSSES.values()):
            self.assertEqual(enemy["moves"]["strong"]["telegraph_rounds"], 1)
            self.assertGreater(enemy["moves"]["strong"]["multiplier"], enemy["normal_multiplier"])
        self.assertEqual(ENEMIES["M04"]["part"]["name"], "雷角")
        self.assertEqual(BOSSES["B04"]["special"]["heal_total_cap"], 0.15)
        self.assertEqual(BOSSES["B05"]["moves"]["strong_alt"]["name"], "沉石")
        self.assertEqual(BOSSES["B06"]["moves"]["strong"]["requires_thunder_marks"], 2)

    def test_broken_reference_is_rejected(self) -> None:
        old_roots = UPGRADES["F01"]["roots"]
        try:
            UPGRADES["F01"]["roots"] = "R99"
            with self.assertRaisesRegex(ValueError, "unknown root reference"):
                validate_catalog()
        finally:
            UPGRADES["F01"]["roots"] = old_roots

    def test_current_departure_and_reward_rules_override_design_sheet(self) -> None:
        self.assertEqual(RULES["rewarded_runs_per_day"], 5)
        self.assertFalse(RULES["practice_mode"])
        self.assertEqual(RULES["failure_rewards"], {
            0: (0, 0, 0), 1: (10, 50, 0), 2: (20, 150, 1),
        })
        self.assertEqual(RULES["clear_rewards"], {
            "cultivation": 30, "stones": 250, "dust": 1, "root_chance": 0.5,
        })
        self.assertEqual(RULES["root_duplicate_dust"], 2)
        self.assertEqual(RULES["root_exchange_dust"], 6)
        self.assertIn("第6次不能出发", RULE_TEXT["G18"]["text"])
        self.assertIn("50%概率", RULE_TEXT["G20"]["text"])

    def test_upgrade_candidates_require_a_real_trigger_source(self) -> None:
        healer = {"class_id": "C04", "roots": [], "fortunes": []}
        shadow = {"class_id": "C06", "roots": [], "fortunes": []}
        sword = {"class_id": "C01", "roots": [], "fortunes": []}
        beast = {"class_id": "C05", "roots": [], "fortunes": []}
        for class_id in CLASSES:
            self.assertGreaterEqual(len(available_upgrades({"class_id": class_id,
                                                            "roots": [], "fortunes": []})), 3)
        self.assertTrue({"F08", "F25", "F26"} <= set(available_upgrades(healer)))
        self.assertNotIn("F20", available_upgrades(healer))
        self.assertTrue({"F27", "F28"} <= set(available_upgrades(shadow)))
        self.assertNotIn("F09", available_upgrades(shadow))
        self.assertNotIn("F13", available_upgrades(sword))
        self.assertNotIn("F18", available_upgrades(sword))
        self.assertNotIn("F22", available_upgrades(sword))
        self.assertIn("F22", available_upgrades(beast))
        sword["roots"] = ["R19", "R26"]
        self.assertTrue({"F13", "F18"} <= set(available_upgrades(sword)))
        sword["fortunes"] = ["F13"]
        self.assertNotIn("F13", available_upgrades(sword))
        self.assertIn("F15", available_upgrades({"class_id": "C01", "roots": [],
                                                  "fortunes": []}, [sword, healer]))


if __name__ == "__main__":
    unittest.main()
