from copy import deepcopy
import json
import random
import unittest
from unittest.mock import patch

from wechat_receiver.games.dungeon import combat as c
from wechat_receiver.games.dungeon.content import BOSSES, CLASSES, ENEMIES, RULES
from wechat_receiver.games.dungeon.patterns import PATTERNS


class FixedRandom(random.Random):
    """Select a move deterministically while respecting zero candidate weights."""

    def __init__(self, preferred="normal"):
        super().__init__(0)
        self.preferred = preferred
        self.last_weights = []

    def choices(self, population, weights=None, *, cum_weights=None, k=1):
        self.last_weights = list(weights or [])
        if self.preferred in population and (not weights or weights[population.index(self.preferred)]):
            return [self.preferred]
        return [next(value for index, value in enumerate(population) if not weights or weights[index])]

    def choice(self, sequence):
        return sequence[0]

    def sample(self, population, k):
        return list(population)[:k]


def member(player="a", class_id="C01", roots=(), fortunes=()):
    result = c.create_member(player, player, class_id, list(roots))
    # Stable unit-test combatants isolate proc/ordering tests from catalog tuning.
    hp, attack, defense = {
        "C01": (1000, 200, 100), "C02": (1250, 170, 110),
        "C03": (900, 225, 90), "C04": (1050, 225, 90),
        "C05": (950, 160, 95), "C06": (900, 230, 85),
        "C07": (1100, 180, 105), "C08": (1050, 220, 85),
    }[class_id]
    result.update(max_hp=float(hp), hp=float(hp), atk=float(attack), defense=float(defense))
    result["fortunes"] = list(fortunes)
    return result


def battle(*members, enemy="M01", night=1):
    result = c.start_battle(list(members) or [member()], enemy, night, FixedRandom())
    result["enemy"]["hp"] = result["enemy"]["max_hp"] = 100000.0
    result["enemy"]["defense"] = 0.0
    result["enemy"]["atk"] = 0.0
    return result


def step(state, actions, rng=None):
    return c.resolve_round(state, actions, rng or FixedRandom())[0]


def intent(state, kind="normal", multiplier=1, targets=None, effects=None):
    victims = targets if targets is not None else [state["members"][0]["player_id"]]
    hit = {"target": "main", "targets": victims, "multiplier": multiplier,
           "weight": "heavy" if kind == "strong" else "light",
           "effects": [effect.get("dot", effect["kind"]) if isinstance(effect, dict) else effect for effect in effects or []],
           "clear_marks": kind == "strong" and state["enemy"]["id"] == "B06"}
    state["intent"] = {"kind": kind, "name": "测试招式", "targets": victims,
                       "front": hit if kind not in {"charge", "recovery"} else None, "back": None,
                       "strong": kind == "strong", "phase": state["enemy"]["phase"],
                       "round": state["round"]}


class DungeonCombatTests(unittest.TestCase):
    def test_state_is_json_and_inputs_are_not_mutated(self):
        original = member(roots=["R25"])
        original["hp"] = 600
        party = [original]
        state = c.start_battle(party, "M01", 1, FixedRandom())
        before = deepcopy(state)
        result, _ = c.resolve_round(state, {"a": "attack"}, FixedRandom())
        self.assertEqual(state, before)
        self.assertEqual(original["hp"], 600)
        self.assertNotIn("shields", original)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_scale_preserves_injury_and_reads_catalog(self):
        actor = member()
        actor["hp"] = 400
        actor["equipment_stats"] = {"hp": 100, "atk": 20, "defense": 10}
        result = c.scale_member(actor, 3)
        stats = CLASSES["C01"]
        hp = c._r(stats["base_hp"] * (1 + 2 * stats["growth_hp"] + 4 * stats["growth_hp_quadratic"]) + 100)
        self.assertEqual(result["max_hp"], hp)
        self.assertEqual(result["hp"], c._r(hp * .4))
        self.assertEqual(result["atk"], c._r(stats["base_atk"] * (1 + 2 * stats["growth_atk"] + 4 * stats["growth_atk_quadratic"]) + 20))
        self.assertEqual(result["defense"], stats["base_defense"] + 2 * stats["growth_defense"] + 10)
        with patch.dict(CLASSES["C01"], {"base_atk": 250}):
            self.assertEqual(c.create_member("x", "x", "C01")["atk"], 250)

    def test_guard_reduces_direct_damage_only_and_caps_at_eighty_percent(self):
        state = battle(member(roots=["R28"], fortunes=["F17"]))
        actor = state["members"][0]
        actor["hp"] = 300
        state["enemy"]["atk"] = 200
        intent(state)
        c._add_dot(state, actor, state["enemy"], "poison", [], attack=100)
        result = step(state, {"a": "defend"})
        # 200 raw / (1 + DEF100/100) * .2 + poison10.
        self.assertAlmostEqual(result["members"][0]["hp"], 270)
        state = battle(member())
        state["enemy"]["atk"] = 200
        intent(state)
        self.assertAlmostEqual(step(state, {"a": "defend"})["members"][0]["hp"], 970)

    def test_shield_absorbs_dot_and_natural_expiry_heals_but_breaking_does_not(self):
        state = battle(member(roots=["R27"]))
        actor = state["members"][0]
        actor["hp"] = 500
        c._shield(actor, 100, "a", "test", 1, [])
        c._add_dot(state, actor, state["enemy"], "burn", [], attack=100)
        result = step(state, {"a": "defend"})
        self.assertEqual(result["members"][0]["hp"], 500)
        self.assertEqual(sum(s["amount"] for s in result["members"][0]["shields"]), 90)
        result = step(result, {"a": "defend"})
        self.assertEqual(result["members"][0]["hp"], 520)
        self.assertEqual(result["members"][0]["shields"], [])

    def test_skill_requires_two_complete_rounds_and_ultimate_does_not_reset_cd(self):
        state = step(battle(member()), {"a": "skill"})
        self.assertEqual(state["round"], 2)
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "skill"}))
        state = step(state, {"a": "ultimate"})
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "skill"}))
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "ultimate"}))
        state = step(state, {"a": "attack"})
        self.assertEqual(state["round"], 4)
        self.assertIsNone(c.validate_action(state, "a", {"type": "skill"}))

    def test_cooldown_reduction_has_floor_and_requires_actual_enemy_attack(self):
        state = battle(member(roots=["R13"], fortunes=["F09"]))
        state = step(state, {"a": "skill"})
        self.assertEqual(state["members"][0]["skill_ready_round"], 3)
        intent(state, "charge")
        state = step(state, {"a": "defend"})
        self.assertNotIn("R13", state["members"][0]["counters"])
        self.assertIsNone(c.validate_action(state, "a", "skill"))

    def test_shadow_has_no_skill_and_shadows_do_not_create_recursive_attacks(self):
        state = battle(member(class_id="C06", fortunes=["F27", "F28"]))
        self.assertIn("没有主动技能", c.validate_action(state, "a", "skill"))
        state = step(state, {"a": "defend"})
        self.assertEqual(state["members"][0]["resource"], 2)
        before = state["enemy"]["hp"]
        state = step(state, {"a": "attack"})
        self.assertAlmostEqual(before - state["enemy"]["hp"], 230 * 1.25)
        before = state["enemy"]["hp"]
        state = step(state, {"a": "attack"})
        self.assertAlmostEqual(before - state["enemy"]["hp"], 230 * 1.40)
        self.assertEqual(state["members"][0]["resource"], 0)

    def test_dan_ultimate_aggregates_overflow_and_caps_at_two_attack(self):
        state = battle(member(class_id="C04", roots=["R09", "R10"], fortunes=["F08"]), member("b"), member("c"))
        before = state["enemy"]["hp"]
        result, logs = c.resolve_round(state, {"a": "ultimate", "b": "defend", "c": "defend"}, FixedRandom())
        self.assertAlmostEqual(before - result["enemy"]["hp"], 450)
        self.assertEqual(sum("原始转伤" in line for line in logs), 1)
        self.assertTrue(any(s["id"] == "regen_R09" for s in result["members"][0]["buffs"]))
        self.assertFalse(any(s["id"] == "regen_R09" for s in result["members"][1]["buffs"]))

    def test_dan_partial_heal_only_converts_overflow_and_does_not_proc_attack_roots(self):
        state = battle(member(class_id="C04", roots=["R19", "R04"], fortunes=["F04", "F10"]))
        actor = state["members"][0]
        actor["hp"] -= 100
        before = state["enemy"]["hp"]
        result = step(state, {"a": "attack"})
        self.assertEqual(result["members"][0]["hp"], actor["max_hp"])
        self.assertAlmostEqual(before - result["enemy"]["hp"], 80)
        self.assertFalse(result["enemy"]["dots"])

    def test_main_healing_occurs_before_allied_attacks(self):
        fighter = member(roots=["R14"], fortunes=["F06"])
        fighter["hp"] -= 100
        state = battle(fighter, member("b", "C04"))
        before = state["enemy"]["hp"]
        result = step(state, {"a": "attack", "b": {"type": "attack", "target": "a"}})
        # Main heal produces 80 overflow; the earlier entrant's attack sees
        # the effective-healing buffs because healing is a separate phase.
        self.assertAlmostEqual(before - result["enemy"]["hp"], 200 * 1.15 + 80)

    def test_low_hp_main_heal_bonus_is_once_and_copy_does_not_reoverflow(self):
        state = battle(member(class_id="C04", roots=["R10"], fortunes=["F05"]), member("b"))
        state["members"][0]["hp"] = 300
        state["members"][1]["hp"] = 300
        before = state["enemy"]["hp"]
        result = step(state, {"a": "attack", "b": "defend"})
        # (180 + 52.5) * 1.1; copied effective healing is exactly 20%,
        # without a second amplification or another copy.
        amount = 232.5 * 1.1
        self.assertAlmostEqual(result["members"][0]["hp"], 300 + amount)
        self.assertAlmostEqual(result["members"][1]["hp"], 300 + amount * .2)
        self.assertEqual(before, result["enemy"]["hp"])

    def test_main_heal_buffs_do_not_trigger_from_potions_or_regeneration(self):
        state = battle(member(class_id="C04", roots=["R07", "R09"], fortunes=["F25", "F26"]))
        state["members"][0]["hp"] = 400
        before = state["enemy"]["hp"]
        result = step(state, {"a": "potion"})
        self.assertEqual(before, result["enemy"]["hp"])
        ids = [s["id"] for s in result["members"][0]["buffs"]]
        self.assertNotIn("F25", ids)
        self.assertNotIn("F26", ids)
        self.assertNotIn("regen_R09", ids)

    def test_f26_can_be_used_in_second_following_round(self):
        state = battle(member(class_id="C04", fortunes=["F26"]))
        state = step(state, {"a": "attack"})
        state = step(state, {"a": "defend"})
        state["members"][0]["hp"] = 100
        result = step(state, {"a": "attack"})
        self.assertAlmostEqual(result["members"][0]["hp"], 100 + (180 + 52.5) * 1.1)

    def test_beast_assist_once_per_team_rotates_and_accepts_full_heal_basic(self):
        state = battle(member("a", "C04"), member("b", "C05", ["R12"], ["F21"]), member("c", "C05", [], ["F24"]))
        state["members"][1]["hp"] -= 200
        state["members"][2]["hp"] -= 200
        result, logs = c.resolve_round(state, {"a": "attack", "b": "defend", "c": "attack"}, FixedRandom())
        self.assertEqual(sum("的灵兽协击" in line for line in logs), 1)
        self.assertTrue(any(line.startswith("b的灵兽协击") for line in logs))
        self.assertAlmostEqual(result["members"][1]["hp"], 750 + 9.5)
        result, logs = c.resolve_round(result, {"a": "attack", "b": "defend", "c": "defend"}, FixedRandom())
        self.assertTrue(any(line.startswith("c的灵兽协击") for line in logs))
        self.assertAlmostEqual(result["members"][2]["hp"], 750 + 19)

    def test_beast_owner_rotates_even_without_a_basic_attack(self):
        state = battle(member("a", "C05"), member("b", "C05"))
        state = step(state, {"a": "defend", "b": "defend"})
        _, logs = c.resolve_round(state, {"a": "attack", "b": "attack"}, FixedRandom())
        self.assertTrue(any(line.startswith("b的灵兽协击") for line in logs))

    def test_rescue_is_bound_to_one_rescuer_pause_preserves_switch_resets(self):
        state = battle(member(), member("b"), member("c"))
        target = state["members"][2]
        target["hp"], target["down_count"] = 0, 3
        state = step(state, {"a": {"type": "rescue", "target": "c"}, "b": "defend"})
        state = step(state, {"a": "defend", "b": "defend"})
        self.assertEqual(state["members"][2]["rescue_progress"], 1)
        state = step(state, {"a": "defend", "b": {"type": "rescue", "target": "c"}})
        self.assertEqual(state["members"][2]["rescue_progress"], 1)
        self.assertEqual(state["members"][2]["rescue_by"], "b")
        for _ in range(2):
            state = step(state, {"a": "defend", "b": {"type": "rescue", "target": "c"}})
        self.assertEqual(state["members"][2]["hp"], 300)
        self.assertEqual(state["members"][2]["down_count"], 3)
        self.assertIsNone(state["members"][2]["rescue_by"])

    def test_two_rescuers_rejected_atomically_and_rescuer_fall_clears_binding(self):
        state = battle(member(), member("b"), member("c"))
        state["members"][2].update(hp=0, down_count=2)
        before = deepcopy(state)
        with self.assertRaisesRegex(ValueError, "一名队友"):
            step(state, {"a": {"type": "rescue", "target": "c"}, "b": {"type": "rescue", "target": "c"}})
        self.assertEqual(state, before)
        state = step(state, {"a": {"type": "rescue", "target": "c"}, "b": "defend"})
        state["members"][0]["hp"] = 1
        state["enemy"]["atk"] = 100
        intent(state, targets=["a"])
        state = step(state, {"a": "attack", "b": "defend"})
        self.assertIsNone(state["members"][2]["rescue_by"])
        self.assertEqual(state["members"][2]["rescue_progress"], 0)

    def test_victory_revives_down_only_and_never_triggers_rescue_roots(self):
        state = battle(member(roots=["R30"]), member("b", roots=["R30"]))
        state["members"][0].update(hp=450, potions=3)
        state["members"][1].update(hp=0, down_count=7, potions=1)
        state["enemy"]["hp"] = 1
        result = step(state, {"a": "attack"})
        self.assertEqual(result["outcome"], "victory")
        self.assertEqual(result["members"][0]["hp"], 450)
        self.assertEqual(result["members"][0]["potions"], 3)
        self.assertEqual(result["members"][1]["hp"], 300)
        self.assertEqual(result["members"][1]["potions"], 2)
        self.assertEqual(result["members"][1]["down_count"], 7)
        self.assertFalse(result["members"][0]["shields"])

    def test_solo_rebirth_once_for_run_and_not_reset_by_new_battle(self):
        state = battle(member(roots=["R30"]))
        state["members"][0]["hp"] = 1
        state["enemy"]["atk"] = 200
        intent(state)
        result = step(state, {"a": "attack"})
        self.assertEqual(result["members"][0]["hp"], 300)
        self.assertTrue(result["members"][0]["solo_revive_used"])
        self.assertTrue(result["members"][0]["shields"])
        second = c.start_battle(result["members"], "M01", 1, FixedRandom())
        second["members"][0]["hp"] = 1
        second["enemy"]["atk"] = 200
        intent(second)
        self.assertEqual(step(second, {"a": "attack"})["outcome"], "defeat")

    def test_potion_is_self_only_and_uses_action_with_25_percent_healing(self):
        state = battle(member(), member("b"))
        state["members"][0]["hp"] = 300
        self.assertIn("只能自己", c.validate_action(state, "a", {"type": "potion", "target": "b"}))
        before = state["enemy"]["hp"]
        result = step(state, {"a": "potion", "b": "defend"})
        self.assertEqual(result["members"][0]["hp"], 550)
        self.assertEqual(result["members"][0]["potions"], 0)
        self.assertEqual(result["enemy"]["hp"], before)

    def test_debuffs_sum_per_source_then_take_highest_not_team_sum(self):
        state = battle(member())
        enemy = state["enemy"]
        enemy["defense"] = 100
        c._debuff(enemy, "a", "skill", "defense_down", .10, 1)
        c._debuff(enemy, "a", "ultimate", "defense_down", .15, 1)
        c._debuff(enemy, "b", "other", "defense_down", .20, 1)
        c._debuff(enemy, "a", "skill", "defense_down", .10, 1)
        self.assertAlmostEqual(c._damage_amount(state, enemy, 175), 100)
        self.assertEqual(len(enemy["debuffs"]), 3)
        self.assertAlmostEqual(c._damage_amount(state, enemy, 137.5, penetration=.8), 100)

    def test_dots_cap_teamwide_snapshot_attack_and_replacement_does_not_expire_proc(self):
        state = battle(member(roots=["R21"], fortunes=["F13"]), member("b"))
        actor, other = state["members"]
        for source in (actor, other, actor, other):
            c._add_dot(state, state["enemy"], source, "burn", [])
        self.assertEqual(len(state["enemy"]["dots"]), 3)
        self.assertEqual(state["enemy"]["hp"], 100000)
        actor["atk"] = 999
        before = state["enemy"]["hp"]
        result = step(state, {"a": "defend", "b": "defend"})
        self.assertAlmostEqual(before - result["enemy"]["hp"], 20 + 24 + 20)
        result, logs = c.resolve_round(result, {"a": "defend", "b": "defend"}, FixedRandom())
        self.assertEqual(sum("的余烬" in line for line in logs), 1)

    def test_poison_restores_its_owner_once_per_round(self):
        state = battle(member(class_id="C05", roots=["R08"], fortunes=["F22", "F23"]), member("b"))
        source = state["members"][0]
        source["hp"] = 400
        for _ in range(3):
            c._add_dot(state, state["enemy"], source, "poison", [])
        self.assertTrue(all(s["remaining"] == 3 for s in state["enemy"]["dots"]))
        self.assertAlmostEqual(c._reduction(state["enemy"], "defense_down", 1), .05)
        result = step(state, {"a": "defend", "b": "defend"})
        self.assertEqual(result["members"][0]["hp"], 419)
        self.assertEqual(result["members"][1]["hp"], 1000)

    def test_counter_exceptions_do_not_loop_and_r26_is_fifteen_percent(self):
        state = battle(member(class_id="C02", roots=["R26"], fortunes=["F18", "F19"]))
        state["enemy"]["atk"] = 100
        intent(state)
        before = state["enemy"]["hp"]
        result, logs = c.resolve_round(state, {"a": "defend"}, FixedRandom())
        self.assertAlmostEqual(before - result["enemy"]["hp"], 170 * (.5 + .15) * 1.4 + 17)
        self.assertEqual(sum("附加1层灼烧" in line for line in logs), 1)
        self.assertEqual(result["members"][0]["resource"], 1)

    def test_one_shot_attack_debuff_survives_a_charge_then_consumes(self):
        state = battle(member(roots=["R15"]))
        state["enemy"]["atk"] = 100
        intent(state)
        state = step(state, {"a": "defend"})
        intent(state, "charge")
        state = step(state, {"a": "defend"})
        before = state["members"][0]["hp"]
        intent(state)
        state = step(state, {"a": "attack"})
        self.assertAlmostEqual(before - state["members"][0]["hp"], 45)
        self.assertEqual(c._reduction(state["enemy"], "attack_down", state["round"]), 0)

    def test_blood_cost_occurs_after_main_healing_bypasses_shields_and_r20_snapshots(self):
        blood = member("a", "C08", ["R20", "R23"])
        blood["hp"] = blood["max_hp"] * .52
        state = battle(blood, member("b", "C04"))
        actor = state["members"][0]
        c._shield(actor, 200, "a", "test", 1, [])
        # The healer heals self at full HP: cost must not alter their target.
        before = state["enemy"]["hp"]
        result = step(state, {"a": "skill", "b": "attack"})
        # R23 applies to the skill after paying; R20 sees pre-payment 52%.
        self.assertAlmostEqual(before - result["enemy"]["hp"], 220 * 1.8 * 1.12 + 180 + 220 * .08)
        self.assertEqual(sum(s["amount"] for s in result["members"][0]["shields"]), 200)

    def test_boss_phase_waits_for_complete_locked_pattern_and_recovery(self):
        state = c.start_battle([member()], "B01", 3, FixedRandom())
        state["enemy"]["atk"] = 0
        state["enemy"]["hp"] = state["enemy"]["max_hp"] / 2 + 1
        snapshot = deepcopy(state["pattern"]["rounds"][1])
        result = step(state, {"a": "attack"})
        self.assertEqual(result["enemy"]["phase"], 1)
        self.assertTrue(result["enemy"]["phase_pending"])
        self.assertEqual(result["intent"]["kind"], "strong")
        self.assertEqual(result["intent"]["back"], snapshot["back"])
        self.assertEqual(result["intent"]["phase"], 1)
        result = step(result, {"a": "defend"})
        self.assertTrue(result["intent"]["recovery"])
        self.assertEqual(result["enemy"]["phase"], 1)
        result = step(result, {"a": "recover"})
        self.assertEqual(result["enemy"]["phase"], 2)
        self.assertEqual(result["pattern"]["index"], 0)

    def test_locked_target_falls_and_strong_does_not_retarget(self):
        state = battle(member(), member("b"))
        intent(state, "strong", 10, ["a"])
        state["enemy"]["atk"] = 200
        state["members"][0].update(hp=0, down_count=1)
        result, logs = c.resolve_round(state, {"b": "attack"}, FixedRandom())
        self.assertEqual(result["members"][1]["hp"], 1000)
        self.assertTrue(any("落空" in line for line in logs))

    def test_fixed_monster_cycle_has_charge_and_recovery_between_heavy_hits(self):
        state = battle(member())
        rng = FixedRandom("charge")
        for expected_round, kind in ((2, "charge"), (3, "strong"), (4, "recovery"), (5, "light")):
            state = step(state, {"a": "attack"}, rng)
            self.assertEqual(state["round"], expected_round)
            self.assertEqual(state["intent"]["kind"], kind)

    def test_rng_move_preference_cannot_insert_extra_actions_into_routine(self):
        state = battle(member())
        locked = deepcopy(state["pattern"])
        state = step(state, {"a": "attack"}, FixedRandom("light"))
        self.assertEqual(state["pattern"]["rounds"], locked["rounds"])
        self.assertEqual(state["intent"]["kind"], "charge")
        state = step(state, {"a": "attack"}, FixedRandom("light"))
        self.assertEqual(state["intent"]["kind"], "strong")

    def test_armor_part_damage_uses_old_armor_window_starts_next_round(self):
        state = battle(member(), member("b"), enemy="M02", night=2)
        state = step(state, {"a": "recover", "b": "recover"})
        before = state["enemy"]["hp"]
        result = step(state, {"a": "part", "b": "attack"})
        self.assertAlmostEqual(before - result["enemy"]["hp"], 200 * .75 * .6 + 200 * .75)
        before = result["enemy"]["hp"]
        result = step(result, {"a": "attack", "b": "attack"})
        self.assertAlmostEqual(before - result["enemy"]["hp"], 400)

    def test_part_weakens_next_locked_strong_once_before_enemy_action(self):
        state = battle(member(), member("b"), enemy="M04", night=2)
        state = step(state, {"a": "recover", "b": "recover"})
        state["enemy"]["atk"] = 100
        result = step(state, {"a": "part", "b": "part"})
        self.assertEqual(result["intent"]["front"]["multiplier"], 5.04)
        result = step(result, {"a": "attack", "b": "attack"})
        self.assertEqual(result["members"][0]["hp"], 748)
        self.assertEqual(result["members"][1]["hp"], 748)

    def test_b04_waits_for_locked_strong_then_summons_unique_sac(self):
        state = c.start_battle([member()], "B04", 3, FixedRandom("charge"))
        state["enemy"]["hp"] = state["enemy"]["max_hp"] / 2 + 1
        state["enemy"]["atk"] = 0
        state = step(state, {"a": "attack"})
        self.assertEqual(state["intent"]["kind"], "strong")
        state = step(state, {"a": "defend"})
        self.assertTrue(state["intent"]["recovery"])
        state = step(state, {"a": "recover"})
        self.assertEqual(state["intent"]["kind"], "summon")
        state = step(state, {"a": "defend"})
        self.assertTrue(state["enemy"]["summon_used"])
        self.assertEqual(state["enemy"]["sac"]["max_hp"], state["enemy"]["max_hp"] * .1)
        for _ in range(8):
            state = step(state, {"a": "defend"})
        self.assertAlmostEqual(state["enemy"]["healing_spent"], state["enemy"]["max_hp"] * .15)
        state["enemy"]["sac"]["hp"] = 1
        before = state["enemy"]["hp"]
        state = step(state, {"a": {"type": "attack", "target": "sac"}})
        self.assertEqual(state["enemy"]["hp"], before)
        self.assertEqual(state["enemy"]["sac"]["hp"], 0)
        self.assertNotEqual(state["intent"]["kind"], "summon")

    def test_thunder_defend_clears_mark_and_dead_target_clears_after_punishment(self):
        state = battle(member(), member("b"), enemy="B06", night=3)
        state["members"][0]["thunder_marks"] = 2
        intent(state, "strong", 2, ["a"])
        state["enemy"]["atk"] = 100
        state = step(state, {"a": "defend", "b": "attack"})
        self.assertEqual(state["members"][0]["thunder_marks"], 0)
        state["members"][0].update(hp=1, thunder_marks=2)
        intent(state, "strong", 2, ["a"])
        state = step(state, {"a": "attack", "b": "attack"})
        self.assertEqual(state["members"][0]["hp"], 0)
        self.assertEqual(state["members"][0]["thunder_marks"], 0)

    def test_all_enemy_variants_use_fixed_patterns_and_serializable_intents(self):
        for enemy_id, entry in {**ENEMIES, **BOSSES}.items():
            with self.subTest(enemy=enemy_id):
                state = c.start_battle([member()], enemy_id, entry.get("night") or 0, FixedRandom("charge"))
                self.assertEqual(state["enemy"]["max_hp"], entry["hp"])
                self.assertEqual(len(state["intent"]["targets"]), 1)
                self.assertEqual(json.loads(json.dumps(state)), state)
                state["enemy"]["atk"] = 0
                state = step(state, {"a": "defend"})
                self.assertEqual(state["pattern"]["index"], 1)
                state = step(state, {"a": "defend"})
                self.assertEqual(state["outcome"], "ongoing")

    def test_reading_summary_does_not_consume_once_vulnerability(self):
        state = battle(member())
        intent(state, "strong", 2)
        c._debuff(state["members"][0], "enemy", "test", "vulnerable", .15, 1, once=True)
        before = deepcopy(state)
        c.battle_summary(state)
        self.assertEqual(state, before)

    def test_rescue_always_follows_main_healing_regardless_of_entry_order(self):
        for reverse in (False, True):
            fighters = [member("a"), member("b", "C04"), member("c")]
            if reverse:
                fighters[0], fighters[1] = fighters[1], fighters[0]
            state = battle(*fighters)
            c._member(state, "c").update(hp=0, down_count=1)
            result = step(state, {"a": {"type": "rescue", "target": "c"}, "b": "ultimate"})
            self.assertEqual(c._member(result, "c")["hp"], 300)

    def test_enemy_target_is_validated_and_limited_to_dan_main_heals(self):
        state = battle(member(class_id="C04"))
        for kind in ("defend", "potion", "rescue"):
            self.assertIsNotNone(c.validate_action(state, "a", {"type": kind, "enemy_target": "enemy"}))
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "attack", "enemy_target": "bogus"}))
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "attack", "enemy_target": "sac"}))
        self.assertIsNone(c.validate_action(state, "a", {"type": "attack", "target": "a", "enemy_target": "enemy"}))
        state = battle(member())
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "attack", "enemy_target": "enemy"}))

    def test_invalid_guard_and_part_targets_do_not_consume_rounds(self):
        state = battle(member(), enemy="M04", night=2)
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "defend", "target": "b"}))
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "part", "target": "不存在的部位"}))
        self.assertIsNone(c.validate_action(state, "a", {"type": "defend", "target": "自己"}))
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "part", "target": "雷角"}))
        state = step(state, {"a": "recover"})
        self.assertIsNone(c.validate_action(state, "a", {"type": "part", "target": "雷角"}))

    def test_day_scaling_is_composed_with_party_size_and_does_not_scale_nights(self):
        state = c.start_battle([member(), member("b")], "M13", 0, FixedRandom(), day=3)
        day = RULES["day_enemy_scaling"][RULES["days"]]
        self.assertEqual(state["enemy"]["max_hp"], c._r(250 * RULES["enemy_hp_party_multipliers"][1] * day["hp"]))
        self.assertEqual(state["enemy"]["atk"], c._r(110 * RULES["enemy_atk_party_multipliers"][1] * day["atk"]))
        self.assertEqual(state["enemy"]["defense"], 40 + day["defense"])
        for day in (1, 3):
            state = c.start_battle([member()], "M01", 1, FixedRandom(), day=day)
            self.assertEqual(state["enemy"]["max_hp"], 1800)
            self.assertEqual(state["enemy"]["atk"], 120)

    def test_party_enemy_scaling_covers_every_enemy_tier_without_defense_bonus(self):
        entries = {**ENEMIES, **BOSSES}
        for tier in ("mob", "elite", "night1", "night2", "boss"):
            enemy_id, entry = next((key, value) for key, value in entries.items()
                                   if value["tier"] == tier)
            for size, hp_scale, atk_scale in zip((1, 2, 3), RULES["enemy_hp_party_multipliers"], RULES["enemy_atk_party_multipliers"]):
                with self.subTest(tier=tier, party_size=size):
                    fighters = [member(str(index)) for index in range(size)]
                    state = c.start_battle(fighters, enemy_id, 0, FixedRandom(), day=3)
                    day_scale = RULES["day_enemy_scaling"][RULES["days"]] if tier in {"mob", "elite"} else {"hp": 1, "atk": 1, "defense": 0}
                    self.assertEqual(state["enemy"]["max_hp"],
                                     c._r(entry["hp"] * hp_scale * day_scale["hp"]))
                    self.assertEqual(state["enemy"]["atk"],
                                     c._r(entry["atk"] * atk_scale * day_scale["atk"]))
                    self.assertEqual(state["enemy"]["defense"],
                                     entry["defense"] + day_scale["defense"])

    def test_party_scaled_attack_feeds_actual_enemy_hit_without_public_damage_prediction(self):
        state = c.start_battle([member(), member("b")], "M13", 0,
                               FixedRandom("charge"))
        attack = 110 * RULES["enemy_atk_party_multipliers"][1]
        self.assertEqual(state["enemy"]["atk"], attack)
        self.assertNotIn("预计直伤", c.battle_summary(state))
        state = step(state, {"a": "defend", "b": "defend"})
        state = step(state, {"a": "recover", "b": "recover"})
        before = state["members"][0]["hp"]
        state = step(state, {"a": "defend", "b": "defend"})
        self.assertAlmostEqual(before - state["members"][0]["hp"], attack * 1.2 / 2 * .3)

    def test_refreshing_open_armor_does_not_restore_armor_mid_round(self):
        state = battle(member(), member("b"), enemy="M02", night=2)
        state = step(state, {"a": "recover", "b": "recover"})
        state["enemy"].update(armor_open_start=1, armor_open_until=state["round"])
        before = state["enemy"]["hp"]
        result = step(state, {"a": "part", "b": "part"})
        self.assertAlmostEqual(before - result["enemy"]["hp"], 200 * .6 * 2)
        self.assertEqual(result["enemy"]["armor_open_until"], 4)

    def test_cinder_shield_cumulative_cap_counts_actual_amount_added(self):
        state = battle(member(fortunes=["F16"]))
        actor = state["members"][0]
        c._shield(actor, 290, "a", "existing", 1, [], duration=1)
        c._add_dot(state, state["enemy"], actor, "burn", [], attack=100)
        state = step(state, {"a": "defend"})
        self.assertEqual(state["members"][0]["counters"]["F16_shield"], 10)
        for _ in range(6):
            c._add_dot(state, state["enemy"], state["members"][0], "burn", [], attack=100)
            state = step(state, {"a": "defend"})
        self.assertEqual(state["members"][0]["counters"]["F16_shield"], 80)

    def test_r01_r02_r03_r04_r22_buffs_apply_once_without_chain(self):
        state = battle(member(roots=["R01", "R02", "R04"]))
        state["enemy"]["defense"] = 100
        state = step(state, {"a": "skill"})
        state = step(state, {"a": "defend"})
        before = state["enemy"]["hp"]
        # R01 expires at end of the round after casting; guarding consumes
        # that opportunity, so only R02 and first-basic R04 apply now.
        state = step(state, {"a": "attack"})
        self.assertAlmostEqual(before - state["enemy"]["hp"], 120 + 30)
        before = state["enemy"]["hp"]
        state = step(state, {"a": "attack"})
        self.assertAlmostEqual(before - state["enemy"]["hp"], 100)
        state = battle(member(roots=["R03", "R22"]))
        state["enemy"]["defense"] = 100
        state["enemy"]["hp"] = 20000
        c._add_dot(state, state["enemy"], state["members"][0], "burn", [])
        before = state["enemy"]["hp"]
        state = step(state, {"a": "attack"})
        self.assertAlmostEqual(before - state["enemy"]["hp"], 200 * 1.06 / 1.95 + 20, places=3)

    def test_cleanse_prioritizes_more_layers_and_burn_on_tie(self):
        state = battle(member(roots=["R17", "R18"], fortunes=["F11"]))
        actor = state["members"][0]
        for kind in ("burn", "poison", "poison"):
            c._add_dot(state, actor, state["enemy"], kind, [], attack=100)
        state = step(state, {"a": "potion"})
        self.assertEqual([s["kind"] for s in state["members"][0]["dots"]], ["burn", "poison"])
        # Both have one tick left; F11 removes burn on the tied layer count.
        state, logs = c.resolve_round(state, {"a": "defend"}, FixedRandom())
        self.assertFalse(any("的灼烧" in line for line in logs))
        self.assertTrue(any("的中毒" in line for line in logs))

    def test_regen_low_hp_and_burn_kill_heal_do_not_resurrect(self):
        state = battle(member(roots=["R11", "R24"]))
        actor = state["members"][0]
        actor["hp"] = 310
        state["enemy"]["atk"] = 100
        intent(state)
        state = step(state, {"a": "attack"})
        self.assertEqual(state["members"][0]["hp"], 310)
        self.assertEqual(state["members"][0]["counters"]["R11"], 1)
        state["enemy"]["hp"] = 1
        c._add_dot(state, state["enemy"], state["members"][0], "burn", [])
        result = step(state, {"a": "attack"})
        self.assertEqual(result["outcome"], "victory")
        self.assertEqual(result["members"][0]["hp"], 360)

    def test_followup_basic_skill_burn_and_shield_synergies_are_scoped(self):
        state = battle(member(class_id="C07", roots=["R05", "R16", "R19"], fortunes=["F01", "F02", "F10", "F14", "F15", "F20"]))
        state = step(state, {"a": "attack"})
        state = step(state, {"a": "attack"})
        c._add_dot(state, state["enemy"], state["members"][0], "burn", [])
        before = state["enemy"]["hp"]
        result = step(state, {"a": "skill"})
        # Primary skill gets F02+R16+F15; F10 extra is independent.
        self.assertAlmostEqual(before - result["enemy"]["hp"], 180 * .9 * 1.4 + 180 * .25 + 36)
        self.assertAlmostEqual(c._reduction(result["enemy"], "defense_down", result["round"]), .15)

    def test_positive_fractional_hp_and_damage_are_not_displayed_as_zero(self):
        state = battle(member())
        state["enemy"]["hp"] = .25
        self.assertIn("<1/", c.battle_summary(state))
        _, logs = c.resolve_round(state, {"a": "attack"}, FixedRandom())
        self.assertTrue(any("造成<1伤害" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
