"""Regressions for committed routines and asynchronous, segment-based defense."""

from copy import deepcopy
import json
import unittest

from wechat_receiver.games.dungeon import combat as c
from wechat_receiver.games.dungeon.content import CLASSES, RULES
from wechat_receiver.games.dungeon.patterns import PART_RULES, PATTERNS
from test_dungeon_combat import FixedRandom, battle, intent, member, step


def hit(targets=("a",), multiplier=1, weight="light", effects=()):
    return {"target": "main", "targets": list(targets), "multiplier": multiplier,
            "weight": weight, "effects": list(effects)}


def attack_round(state, front=None, back=None):
    state["intent"] = {"name": "测试连招", "cue": "它压低重心。", "kind": "strong",
                       "strong": True, "front": front, "back": back,
                       "targets": list(dict.fromkeys(p for row in (front, back) if row for p in row["targets"])),
                       "round": state["round"], "phase": state["enemy"]["phase"]}


def ready(enemy, fighters=None, phase=1, index=0):
    state = battle(*(fighters or [member()]), enemy=enemy, night=3 if enemy.startswith("B") else 1)
    state["enemy"]["phase"] = phase
    state.pop("pattern", None)
    state["intent"] = c._choose_intent(state, FixedRandom())
    for _ in range(index):
        state = step(state, {c._pid(m): "recover" for m in state["members"] if c._alive(m)})
    return state


class DungeonTimingTests(unittest.TestCase):
    def test_dodge_commit_is_paid_once_and_bound_to_round_and_segment(self):
        state = battle(member())
        state["members"][0]["stance"] = 1
        action = {"type": "dodge", "segment": "front"}
        c.commit_action(state, "a", action)
        c.commit_action(state, "a", action)
        self.assertEqual(state["members"][0]["stance"], 0)
        self.assertIsNone(c.validate_action(state, "a", action))
        self.assertIsNotNone(c.validate_action(state, "a", {"type": "dodge", "segment": "back"}))
        before = deepcopy(state)
        result, logs = c.resolve_round(state, {"a": action}, FixedRandom())
        self.assertEqual(state, before)
        self.assertEqual(result["members"][0]["stance"], 0)
        self.assertTrue(any("避开" in line for line in logs))
        self.assertIsNotNone(c.validate_action(result, "a", action))
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_invalid_team_action_does_not_partially_pay_dodge(self):
        state = battle(member(), member("b", "C06"))
        before = deepcopy(state)
        with self.assertRaises(ValueError):
            step(state, {"a": {"type": "dodge", "segment": "front"}, "b": "skill"})
        self.assertEqual(state, before)
        for data in ({"type": "dodge"}, {"type": "dodge", "segment": "now"},
                     {"type": "recover", "segment": "front"}):
            self.assertIsNotNone(c.validate_action(state, "a", data))

    def test_dodging_empty_segment_still_pays_and_does_not_guard(self):
        state = battle(member(class_id="C02", roots=["R26", "R13", "R15"]))
        state["enemy"]["atk"] = 100
        state["members"][0]["defense"] = 0
        attack_round(state, front=hit())
        result = step(state, {"a": {"type": "dodge", "segment": "back"}})
        actor = result["members"][0]
        self.assertEqual(actor["stance"], 2)
        self.assertEqual(actor["hp"], 1150)
        self.assertEqual(actor["resource"], 0)
        self.assertEqual(result["enemy"]["hp"], 100000)
        self.assertNotIn("R13", actor["counters"])
        self.assertFalse(result["enemy"]["debuffs"])

    def test_each_segment_charges_guard_and_exact_zero_is_success(self):
        for stance, expected_loss in ((3, 90), (2, 230), (1, 230), (0, 300)):
            with self.subTest(stance=stance):
                state = battle(member())
                state["enemy"]["atk"] = 100
                state["members"][0].update(defense=0, stance=stance)
                attack_round(state, hit(), hit(multiplier=2, weight="heavy"))
                result = step(state, {"a": "defend"})
                self.assertEqual(result["members"][0]["hp"], 1000 - expected_loss)
                self.assertEqual(result["members"][0]["stance"], 0)

    def test_insufficient_guard_loses_additional_guard_reduction_but_keeps_shield(self):
        state = battle(member(roots=["R28"], fortunes=["F17"]))
        actor = state["members"][0]
        actor.update(hp=300, stance=1, defense=0)
        state["enemy"]["atk"] = 100
        c._shield(actor, 80, "a", "test", 1, [])
        attack_round(state, hit(), hit(multiplier=2, weight="heavy"))
        result = step(state, {"a": "defend"})
        self.assertEqual(result["members"][0]["hp"], 160)
        self.assertEqual(result["members"][0]["stance"], 0)
        self.assertFalse(result["members"][0]["shields"])

    def test_guard_choice_effects_do_not_require_hit_or_available_stance(self):
        state = ready("B06", [member(class_id="C06", roots=["R02"], fortunes=["F11"])], index=2)
        actor = state["members"][0]
        actor.update(stance=0, thunder_marks=2)
        c._add_dot(state, actor, state["enemy"], "burn", [], attack=100)
        result = step(state, {"a": "defend"})
        actor = result["members"][0]
        self.assertEqual(actor["stance"], 0)
        self.assertEqual(actor["resource"], 1)
        self.assertEqual(actor["thunder_marks"], 1)
        self.assertFalse(actor["dots"])
        self.assertGreater(c._buff(actor, "R02", result["round"]), 0)

    def test_guard_hit_effects_are_once_per_round_across_two_segments(self):
        state = battle(member(class_id="C02", roots=["R26", "R13"], fortunes=["F19"]))
        state["members"][0]["skill_ready_round"] = 8
        state["enemy"]["atk"] = 100
        attack_round(state, hit(), hit(weight="heavy"))
        result, logs = c.resolve_round(state, {"a": "defend"}, FixedRandom())
        actor = result["members"][0]
        self.assertEqual(actor["resource"], 1)
        self.assertEqual(actor["counters"]["R13"], 1)
        self.assertEqual(actor["skill_ready_round"], 7)
        self.assertEqual(sum("的不屈战意反击" in line for line in logs), 1)
        self.assertEqual(sum("的震岳反击" in line for line in logs), 1)
        self.assertEqual(sum("附加1层灼烧" in line for line in logs), 1)

    def test_enemy_attack_snapshot_is_shared_across_segments_and_party_order(self):
        for reverse in (False, True):
            fighters = [member(roots=["R15"]), member("b")]
            state = battle(*reversed(fighters) if reverse else fighters)
            for actor in state["members"]:
                actor["defense"] = 0
            state["enemy"]["atk"] = 100
            attack_round(state, hit(("a", "b")), hit(("a", "b")))
            result = step(state, {"a": "defend", "b": "recover"})
            self.assertEqual(c._member(result, "a")["hp"], 940)
            self.assertEqual(c._member(result, "b")["hp"], 800)
            self.assertEqual(c._reduction(result["enemy"], "attack_down", result["round"]), .1)

    def test_dodge_prevents_only_hit_status_and_keeps_existing_dot(self):
        for segment, expected_loss, new_poison in (("front", 80, True), ("back", 280, False)):
            state = ready("B04", index=1)
            state["enemy"]["atk"] = 100
            c._add_dot(state, state["members"][0], state["enemy"], "burn", [], attack=50)
            result = step(state, {"a": {"type": "dodge", "segment": segment}})
            self.assertEqual(result["members"][0]["hp"], 1000 - expected_loss - 5)
            dots = result["members"][0]["dots"]
            self.assertEqual(any(dot["kind"] == "poison" for dot in dots), new_poison)
            self.assertTrue(any(dot["kind"] == "burn" for dot in dots))

    def test_shield_absorbed_hits_still_charge_stance_and_apply_status(self):
        state = battle(member())
        state["enemy"]["atk"] = 100
        c._shield(state["members"][0], 300, "a", "test", 1, [])
        attack_round(state, hit(effects=("poison",)))
        result = step(state, {"a": "defend"})
        actor = result["members"][0]
        self.assertEqual(actor["hp"], 1000)
        self.assertEqual(actor["stance"], 2)
        self.assertEqual(actor["dots"][0]["kind"], "poison")
        self.assertEqual(actor["shields"][0]["amount"], 275)

    def test_rebirth_keeps_stance_and_remaining_segment_can_hit_or_be_dodged(self):
        for action, expected_outcome, expected_hp in (
                ("defend", "defeat", 0), ({"type": "dodge", "segment": "back"}, "ongoing", 300)):
            state = battle(member())
            state["members"][0].update(hp=1, defense=0, stance=1 if isinstance(action, dict) else 0)
            state["enemy"]["atk"] = 100
            attack_round(state, hit(), hit(multiplier=4, weight="heavy"))
            result = step(state, {"a": action})
            self.assertEqual(result["outcome"], expected_outcome)
            self.assertEqual(result["members"][0]["hp"], expected_hp)
            self.assertEqual(result["members"][0]["stance"], 0)
            self.assertTrue(result["members"][0]["solo_revive_used"])

    def test_front_counter_kill_cancels_remaining_targets_and_back_segment(self):
        state = battle(member(class_id="C02"), member("b"))
        state["enemy"].update(hp=50, atk=100)
        attack_round(state, hit(("a", "b")), hit(("a", "b"), multiplier=10, weight="heavy"))
        result, logs = c.resolve_round(state, {"a": "defend", "b": "recover"}, FixedRandom())
        self.assertEqual(result["outcome"], "victory")
        self.assertEqual(c._member(result, "b")["hp"], 1000)
        self.assertEqual(sum("的测试连招" in line for line in logs), 1)

    def test_recover_occurs_after_dot_and_only_for_survivors(self):
        for hp, expected_hp, expected_stance in ((15, 5, 2), (5, 0, 0)):
            state = battle(member(), member("b"))
            actor = state["members"][0]
            actor.update(hp=hp, stance=0)
            c._add_dot(state, actor, state["enemy"], "burn", [], attack=100)
            intent(state, "recovery")
            result = step(state, {"a": "recover", "b": "recover"})
            self.assertEqual(result["members"][0]["hp"], expected_hp)
            self.assertEqual(result["members"][0]["stance"], expected_stance)

    def test_rescue_and_ordinary_actions_do_not_refill_stance(self):
        state = battle(member(), member("b"))
        state["members"][0].update(stance=0, hp=500)
        state["members"][1].update(stance=0, hp=0, down_count=1)
        intent(state, "recovery")
        state = step(state, {"a": {"type": "rescue", "target": "b"}})
        self.assertEqual([m["stance"] for m in state["members"]], [0, 0])
        intent(state, "recovery")
        state = step(state, {"a": "potion", "b": "attack"})
        self.assertEqual([m["stance"] for m in state["members"]], [0, 0])
        restarted = c.start_battle(state["members"], "M01")
        self.assertEqual([m["stance"] for m in restarted["members"]], [3, 3])
        self.assertEqual([m["hp"] for m in restarted["members"]], [m["hp"] for m in state["members"]])

    def test_phase_pending_survives_healing_above_half_and_finishes_recovery(self):
        state = ready("B04")
        state["enemy"].update(max_hp=1000, hp=501)
        state["members"][0]["atk"] = 2
        c._regen(state["enemy"], "regen_test", state["enemy"], .10, 1, duration=3)
        state = step(state, {"a": "attack"})
        self.assertGreater(state["enemy"]["hp"], 500)
        self.assertTrue(state["enemy"]["phase_pending"])
        state = step(state, {"a": "recover"})
        self.assertEqual(state["enemy"]["phase"], 1)
        self.assertTrue(state["intent"]["recovery"])
        state = step(state, {"a": "recover"})
        self.assertEqual(state["enemy"]["phase"], 2)
        self.assertTrue(state["intent"]["summon"])

    def test_each_part_uses_exact_window_and_weakens_only_this_routine(self):
        for enemy_id, rule in PART_RULES.items():
            with self.subTest(enemy=enemy_id):
                state = ready(enemy_id, [member(), member("b")], index=0 if enemy_id == "B03" else 1)
                round_no = state["round"]
                self.assertIsNone(c.validate_action(state, "a", {"type": "part", "target": rule["name"]}))
                result = step(state, {"a": "part", "b": "part"})
                if rule.get("armor"):
                    self.assertEqual(result["enemy"]["armor_open_start"], round_no + 1)
                    self.assertEqual(result["enemy"]["armor_open_until"], round_no + rule["open_rounds"])
                if "strong_multiplier" in rule:
                    impacts = [result["intent"][s] for s in ("front", "back") if result["intent"].get(s)]
                    self.assertEqual(impacts[0]["multiplier"], rule["strong_multiplier"])
                self.assertIsNotNone(c.validate_action(result, "a", "part"))
                while result["pattern"]["index"]:
                    result = step(result, {"a": "recover", "b": "recover"})
                self.assertFalse(any(row.get("weakened") for row in result["pattern"]["rounds"]))

    def test_m09_exposure_requires_successful_heavy_block(self):
        for stance, action, exposed in ((2, "defend", .15), (1, "defend", 0),
                                       (1, {"type": "dodge", "segment": "back"}, 0)):
            state = ready("M09", index=2)
            state["members"][0]["stance"] = stance
            result = step(state, {"a": action})
            self.assertEqual(c._buff(result["enemy"], "exposed", result["round"]), exposed)
            result = step(result, {"a": "recover"})
            self.assertEqual(c._buff(result["enemy"], "exposed", result["round"]), 0)

    def test_lethal_part_hit_does_not_create_posthumous_armor_window(self):
        state = ready("B03")
        state["enemy"]["hp"] = 1
        result = step(state, {"a": "part"})
        self.assertEqual(result["outcome"], "victory")
        self.assertEqual(result["enemy"]["armor_open_start"], 0)
        self.assertEqual(result["enemy"]["armor_open_until"], 0)

    def test_tide_exposure_starts_after_heavy_and_ends_after_recovery(self):
        for enemy_id, strong_index, bonus in (("M22", 2, .25), ("B02", 2, .25)):
            state = ready(enemy_id, index=strong_index)
            before = state["enemy"]["hp"]
            state = step(state, {"a": "attack"})
            self.assertEqual(before - state["enemy"]["hp"], 200)
            self.assertEqual(c._buff(state["enemy"], "exposed", state["round"]), bonus)
            before = state["enemy"]["hp"]
            state = step(state, {"a": "attack"})
            self.assertEqual(before - state["enemy"]["hp"], 250)
            self.assertEqual(c._buff(state["enemy"], "exposed", state["round"]), 0)

    def test_b06_snapshots_only_after_two_mark_rounds_and_does_not_reprice(self):
        state = ready("B06")
        self.assertNotIn("marked_snapshot", state["pattern"])
        state = step(state, {"a": "attack"})
        self.assertNotIn("marked_snapshot", state["pattern"])
        state = step(state, {"a": "attack"})
        self.assertTrue(state["intent"]["lock_thunder"])
        self.assertTrue(state["pattern"]["marked_snapshot"])
        self.assertEqual(state["members"][0]["thunder_marks"], 2)
        state = step(state, {"a": "defend"})
        self.assertEqual(state["intent"]["back"]["multiplier"], 7.4)
        self.assertEqual(state["members"][0]["thunder_marks"], 1)
        state = step(state, {"a": "defend"})
        self.assertEqual(state["members"][0]["thunder_marks"], 0)

    def test_b06_phase2_front_new_mark_does_not_change_back_snapshot_or_clear_on_dodge(self):
        state = ready("B06", phase=2)
        state = step(state, {"a": "defend"})
        state = step(state, {"a": "defend"})
        self.assertFalse(state["pattern"]["marked_snapshot"])
        state = step(state, {"a": "recover"})
        self.assertEqual(state["intent"]["back"]["multiplier"], 5)
        state = step(state, {"a": {"type": "dodge", "segment": "back"}})
        self.assertEqual(state["members"][0]["thunder_marks"], 2)

    def test_b06_condense_lock_uses_survivors_after_second_round_dot(self):
        state = ready("B06", [member(), member("b")])
        state = step(state, {"a": "attack", "b": "recover"})
        actor = state["members"][0]
        actor["hp"] = 1
        c._add_dot(state, actor, state["enemy"], "burn", [], attack=100)
        state = step(state, {"a": "attack", "b": "recover"})
        self.assertEqual(state["members"][0]["hp"], 0)
        self.assertEqual(state["intent"]["targets"], ["b"])
        self.assertFalse(state["pattern"]["marked_snapshot"])
        state = step(state, {"b": {"type": "rescue", "target": "a"}})
        self.assertEqual(state["intent"]["back"]["targets"], ["b"])
        self.assertEqual(state["intent"]["back"]["multiplier"], 6.4)

    def test_b05_consults_only_last_completed_round_when_choosing_next_routine(self):
        rng = FixedRandom("stone")
        state = c.start_battle([member()], "B05", 3, rng)
        state["enemy"].update(hp=100000, max_hp=100000, atk=0)
        self.assertEqual(rng.last_weights, [1, 2])
        locked = deepcopy(state["pattern"]["rounds"])
        state = step(state, {"a": "attack"}, rng)
        self.assertEqual(rng.last_weights, [1, 2])
        self.assertEqual(state["pattern"]["rounds"], locked)
        state = step(state, {"a": "recover"}, rng)
        state = step(state, {"a": "attack"}, rng)
        self.assertEqual(rng.last_weights, [2, 1])

    def test_monster_light_and_heavy_targets_lock_independently_at_cycle_start(self):
        class Alternating(FixedRandom):
            def __init__(self):
                super().__init__()
                self.cursor = 0

            def choice(self, sequence):
                result = sequence[self.cursor % len(sequence)]
                self.cursor += 1
                return result

        state = c.start_battle([member(), member("b")], "M13", 0, Alternating())
        state["enemy"].update(hp=100000, max_hp=100000, atk=0)
        self.assertEqual(state["intent"]["targets"], ["a"])
        self.assertEqual(state["pattern"]["rounds"][2]["back"]["targets"], ["b"])
        state = step(state, {"a": "recover", "b": "recover"})
        self.assertEqual(state["intent"]["targets"], ["b"])
        state["members"][1].update(hp=0, down_count=1)
        state = step(state, {"a": "recover"})
        self.assertEqual(state["intent"]["targets"], ["b"])
        state["enemy"]["atk"] = 10000
        state = step(state, {"a": "recover"})
        self.assertEqual(state["members"][0]["hp"], 1000)

    def test_all_patterns_complete_without_mutating_static_data(self):
        before = deepcopy(PATTERNS)
        for enemy_id, data in PATTERNS.items():
            for phase in (1, 2) if "phase2" in data else (1,):
                state = ready(enemy_id, phase=phase)
                if state["intent"].get("summon"):
                    state = step(state, {"a": "recover"})
                rounds = len(state["pattern"]["rounds"])
                for index in range(rounds):
                    self.assertEqual(state["pattern"]["index"], index)
                    state = step(state, {"a": "recover"})
                self.assertEqual(state["pattern"]["index"], 0)
                self.assertEqual(json.loads(json.dumps(state)), state)
        self.assertEqual(PATTERNS, before)

    def test_auto_policy_recovers_or_spends_remaining_stance_without_hidden_refill(self):
        for stance, expected in ((3, {"type": "defend"}), (1, {"type": "dodge", "segment": "back"}),
                                 (0, {"type": "recover"})):
            state = ready("M13", index=2)
            state["members"][0]["stance"] = stance
            self.assertEqual(c._auto_actions(state)["a"], expected)
        state = ready("M13", index=1)
        state["members"][0]["stance"] = 1
        self.assertEqual(c._auto_actions(state)["a"], {"type": "recover"})

    def test_auto_part_selects_lowest_damage_eligible_fighter_and_skips_lethal(self):
        state = ready("M07", [member(), member("b", "C03"), member("c", "C04")], index=1)
        state["members"][0]["hp"] = 400
        state["members"][1]["atk"] = 10
        actions = c._auto_actions(state)
        self.assertEqual(actions["b"], {"type": "part", "target": "鱼鳍"})
        self.assertEqual(sum(action["type"] == "part" for action in actions.values()), 1)
        state["enemy"]["hp"] = 100
        state["members"][0]["stance"] = 0
        actions = c._auto_actions(state)
        self.assertEqual(actions["a"]["type"], "attack")
        self.assertNotIn("part", [action["type"] for action in actions.values()])
        state = ready("M07", [member(class_id="C04")], index=1)
        state["members"][0]["hp"] = 1
        self.assertEqual(c._auto_actions(state)["a"]["type"], "attack")

    def test_auto_short_battle_finishes_without_forcing_a_full_cycle(self):
        fighter = member()
        fighter["atk"] = 10000
        state, _ = c.auto_battle([fighter], "M13", rng=FixedRandom())
        self.assertEqual(state["round"], 1)
        self.assertEqual(state["outcome"], "victory")
        self.assertEqual(state["members"][0]["hp"], fighter["hp"])

    def test_low_hp_dan_bonus_is_capped_by_attack_not_large_target_hp(self):
        state = battle(member(class_id="C04"), member("b"))
        state["members"][0]["atk"] = 10
        state["members"][1].update(max_hp=100000, hp=100)
        result = step(state, {"a": {"type": "attack", "target": "b"}, "b": "recover"})
        expected = 10 * (CLASSES["C04"]["basic_multiplier"] + CLASSES["C04"]["low_hp_bonus_atk_cap"])
        self.assertEqual(result["members"][1]["hp"], 100 + expected)
        self.assertEqual(result["enemy"]["hp"], state["enemy"]["hp"])

    def test_attack_down_covers_two_future_actions_without_retroactive_damage(self):
        state = ready("M05")
        initial = state["enemy"]["hp"]
        state = step(state, {"a": "attack"})
        self.assertEqual(initial - state["enemy"]["hp"], 200)
        for expected in (160, 160, 200):
            intent(state, "recovery")
            before = state["enemy"]["hp"]
            state = step(state, {"a": "attack"})
            self.assertEqual(before - state["enemy"]["hp"], expected)

    def test_once_vulnerability_is_not_consumed_by_dodge_or_dot(self):
        state = battle(member())
        state["enemy"]["atk"] = 100
        actor = state["members"][0]
        c._debuff(actor, "enemy", "test", "vulnerable", .15, 1, once=True)
        c._add_dot(state, actor, state["enemy"], "burn", [], attack=100)
        attack_round(state, hit())
        state = step(state, {"a": {"type": "dodge", "segment": "front"}})
        self.assertEqual(state["members"][0]["hp"], 990)
        self.assertEqual(c._reduction(state["members"][0], "vulnerable", state["round"]), .15)
        attack_round(state, hit())
        state = step(state, {"a": "recover"})
        self.assertEqual(state["members"][0]["hp"], 922.5)
        self.assertFalse(state["members"][0]["debuffs"])

    def test_summary_announces_upcoming_targets_without_damage_or_timing_answers(self):
        state = ready("B01", [member(), member("b")])
        before = deepcopy(state)
        text = c.battle_summary(state)
        self.assertEqual(state, before)
        self.assertIn("架势3/3", text)
        self.assertIn("蓄势目标：a、b", text)
        self.assertIn(state["intent"]["cue"], text)
        for forbidden in ("倍率", "预计直伤", "威力", "前段", "后段", "应该", "闪早", "闪晚"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
