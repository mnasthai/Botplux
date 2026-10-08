"""Structural and transcription checks for the fixed enemy pattern data."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


_path = Path(__file__).parents[1] / "src/wechat_receiver/games/dungeon/patterns.py"
_spec = importlib.util.spec_from_file_location("dungeon_patterns_under_test", _path)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
PATTERNS = _module.PATTERNS
PART_RULES = _module.PART_RULES


def _rounds():
    for enemy_id, entry in PATTERNS.items():
        for phase in ("phase1", "phase2"):
            variants = entry.get(phase)
            if variants is None:
                continue
            if isinstance(variants, dict):
                for variant, rows in variants.items():
                    for row in rows:
                        yield enemy_id, phase, variant, row
            else:
                for row in variants:
                    yield enemy_id, phase, None, row
        if "summon_round" in entry:
            yield enemy_id, "summon_round", None, entry["summon_round"]


class DungeonPatternTests(unittest.TestCase):
    def test_exact_roster_and_cycle_shape(self) -> None:
        self.assertEqual(set(PATTERNS),
                         {f"M{i:02d}" for i in range(1, 25)} |
                         {f"B{i:02d}" for i in range(1, 7)})
        self.assertNotIn("B07", PATTERNS)
        for i in range(1, 25):
            enemy_id = f"M{i:02d}"
            rows = PATTERNS[enemy_id]["phase1"]
            self.assertEqual(len(rows), 4, enemy_id)
            self.assertNotIn("phase2", PATTERNS[enemy_id])
            self.assertFalse(rows[0]["strong"])
            self.assertTrue(rows[1]["charge"])
            self.assertIsNone(rows[1]["front"])
            self.assertIsNone(rows[1]["back"])
            self.assertTrue(rows[2]["strong"])
            self.assertTrue(rows[3]["recovery"])
        for enemy_id in ("B01", "B02", "B03", "B04", "B06"):
            for phase in ("phase1", "phase2"):
                self.assertIsInstance(PATTERNS[enemy_id][phase], list)
                self.assertTrue(PATTERNS[enemy_id][phase][-1]["recovery"])
        for phase in ("phase1", "phase2"):
            self.assertEqual(set(PATTERNS["B05"][phase]), {"quake", "stone"})
            for rows in PATTERNS["B05"][phase].values():
                self.assertTrue(rows[-1]["recovery"])
        self.assertTrue(PATTERNS["B04"]["summon_round"]["summon"])
        self.assertFalse(PATTERNS["B04"]["summon_round"]["recovery"])

    def test_rows_have_readable_cues_and_one_hit_per_segment(self) -> None:
        targets = {"main", "two", "all", "low_two"}
        effects = {"burn", "poison", "bleed", "attack_down", "vulnerable", "thunder"}
        for enemy_id, phase, variant, row in _rounds():
            location = (enemy_id, phase, variant, row["name"])
            self.assertTrue(isinstance(row["name"], str) and row["name"].strip(), location)
            self.assertTrue(isinstance(row["cue"], str) and row["cue"].strip(), location)
            for segment in ("front", "back"):
                hit = row[segment]
                if hit is None:
                    continue
                self.assertIsInstance(hit, dict, location)
                self.assertIn(hit["target"], targets, location)
                self.assertIn(hit["weight"], {"light", "heavy"}, location)
                self.assertGreater(hit["multiplier"], 0, location)
                self.assertTrue(set(hit["effects"]) <= effects, location)
            if row["recovery"] or row["summon"] or row["lock_thunder"]:
                self.assertIsNone(row["front"], location)
                self.assertIsNone(row["back"], location)
            if row["strong"]:
                self.assertTrue(row["front"] or row["back"], location)

    def test_mob_light_and_strong_coefficients_match_design(self) -> None:
        # L and S are already final coefficients; no range/status discount follows.
        expected = {
            "M01": (.75, 6.30), "M02": (.80, 7.20), "M03": (.75, 4.536),
            "M04": (.80, 7.20), "M05": (1.05, 2.00), "M06": (1.05, 1.44),
            "M07": (1.05, 1.60), "M08": (1.05, 2.00), "M09": (.75, 6.30),
            "M10": (.75, 5.04), "M11": (.80, 6.48), "M12": (.80, 7.20),
            "M13": (.80, 1.20), "M14": (.80, 1.20), "M15": (.80, .96),
            "M16": (.80, 1.08), "M17": (.80, 1.20), "M18": (.80, 1.08),
            "M19": (.80, 1.20), "M20": (.80, .864), "M21": (.80, 8.10),
            "M22": (.80, 7.20), "M23": (.80, 7.29), "M24": (.80, 7.20),
        }
        for enemy_id, (light, strong) in expected.items():
            l, _, s, _ = PATTERNS[enemy_id]["phase1"]
            self.assertEqual((l["front"] or l["back"])["multiplier"], light, enemy_id)
            self.assertEqual((s["front"] or s["back"])["multiplier"], strong, enemy_id)
        self.assertEqual(PATTERNS["M01"]["phase1"][0]["front"]["target"], "low_two")
        self.assertEqual(PATTERNS["M01"]["phase1"][2]["back"]["target"], "main")
        self.assertEqual(PATTERNS["M01"]["phase1"][2]["back"]["offensive_multiplier"], 7.20)
        self.assertEqual(PATTERNS["M09"]["phase1"][3]["expose"], 0)
        self.assertEqual(PATTERNS["M22"]["phase1"][3]["expose"], .25)

    def test_boss_distinctions_and_part_rules(self) -> None:
        self.assertEqual(PATTERNS["B02"]["phase2"][2]["front"]["weight"], "heavy")
        self.assertEqual(PATTERNS["B02"]["phase2"][2]["back"]["weight"], "light")
        self.assertEqual(PATTERNS["B02"]["phase1"][-1]["expose"], .25)
        self.assertEqual(PATTERNS["B02"]["phase2"][-1]["expose"], .35)
        self.assertEqual(PATTERNS["B03"]["phase2"][1]["front"]["effects"], ["bleed"])
        self.assertEqual(PATTERNS["B05"]["phase1"]["quake"][0]["front"]["target"], "two")
        stone = PATTERNS["B05"]["phase2"]["stone"][1]
        self.assertEqual((stone["front"]["weight"], stone["back"]["weight"]),
                         ("heavy", "light"))
        thunder = PATTERNS["B06"]["phase1"]
        self.assertTrue(thunder[0]["mark_setup"] and thunder[1]["mark_setup"])
        self.assertTrue(thunder[2]["lock_thunder"])
        self.assertEqual(thunder[3]["back"]["marked_multiplier"], 7.40)
        self.assertTrue(thunder[3]["back"]["clear_marks"])
        self.assertEqual(PATTERNS["B06"]["phase2"][3]["back"]["marked_multiplier"], 6.20)
        self.assertEqual(set(PART_RULES),
                         {"M02", "M04", "M07", "M12", "M17", "M24", "B03"})
        self.assertEqual(PART_RULES["M17"],
                         {"name": "蟹壳", "armor": .10, "open_rounds": 1,
                          "strong_multiplier": .84})
        self.assertEqual(PART_RULES["B03"]["open_rounds"], 2)
        self.assertEqual(PART_RULES["M24"]["strong_multiplier"], 5.35)


if __name__ == "__main__":
    unittest.main()
