"""Stage-two behavioural tests for the 宿舍仙途 stateful plugin.

These tests deliberately use the real transactional reply router and a temporary
SQLite store.  They assert durable assets and action markers instead of game
copy, so the game may improve its wording without weakening its guarantees.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import re
import tempfile
import unittest
from unittest.mock import patch

from wechat_receiver.games import service
from wechat_receiver.games.catalog import CATALOG
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.plugins import LoadedPlugin, load_plugins
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class FixedRng:
    """A deterministic game RNG; values select rarity and first available item."""

    def __init__(self, *rolls: int) -> None:
        self.rolls = list(rolls or (0,))

    def randrange(self, stop: int) -> int:
        self.assert_stop(stop)
        return self.rolls.pop(0) if self.rolls else 0

    @staticmethod
    def assert_stop(stop: int) -> None:
        if stop != 100:
            raise AssertionError(f"unexpected random range: {stop}")

    @staticmethod
    def choice(sequence):
        return sequence[0]


class XiuxianGameTests(unittest.TestCase):
    account = "wxid_bot"
    group = "22913213991@chatroom"
    other_group = "another@chatroom"
    base = datetime(2026, 9, 18, 15, 59, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        self.now = self.base.timestamp()
        self.config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group, self.other_group})),
            enabled_plugins=("xiuxian",), reply_ttl_seconds=60, max_message_age_seconds=120,
        )
        self.rng = FixedRng()
        plugin = LoadedPlugin(
            "xiuxian", None, parse_command=parse_command,
            handle_command=lambda command, context: service.handle_command(command, context, rng=self.rng),
        )
        self.router = ReplyRouter(self.store, self.config, [plugin], started_at=self.now - 1)
        self.sequence = 0

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def message(self, text: str, player: str = "alice", *, group: str | None = None,
                event_key: str | None = None, message_id: str | None = None) -> Message:
        self.sequence += 1
        number = self.sequence
        return Message(
            session_id="test", event_key=event_key or f"test:{number}", seq=number, call_id=number,
            source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None, content=text, raw_content=text,
            conversation_id=group or self.group, sender_id=player, direction="incoming",
            message_time_candidate=int(self.now), message_id_candidate=message_id or str(10_000 + number),
            mentioned_ids=(), mention_state="none", history_state="live_candidate",
        )

    def send(self, text: str, player: str = "alice", **kwargs) -> int:
        return self.router.handle(self.message(text, player, **kwargs), "test", now=self.now)

    def player(self, player: str = "alice", group: str | None = None):
        return self.store.db.execute(
            "SELECT * FROM game_players WHERE account_id=? AND group_id=? AND player_id=?",
            (self.account, group or self.group, player),
        ).fetchone()

    def items(self, player: str = "alice", group: str | None = None):
        return self.store.db.execute(
            "SELECT * FROM game_items WHERE account_id=? AND group_id=? AND owner_player_id=? ORDER BY item_id",
            (self.account, group or self.group, player),
        ).fetchall()

    def register(self, player: str = "alice", name: str = "青玄", **kwargs) -> None:
        self.send(f"#修仙 {name}", player, **kwargs)
        self.assertIsNotNone(self.player(player, kwargs.get("group")))

    def latest_reply(self) -> str:
        return self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def test_help_directory_routes_to_read_only_module_guides(self) -> None:
        entries = ('#新手帮助', '#修炼帮助', '#秘境帮助', '#法宝帮助', '#副本帮助',
                   '#商店帮助', '#道具帮助', '#魔鬼交易帮助', '#决斗帮助', '#斗法帮助')
        self.assertEqual(1, self.send('#修仙帮助'))
        directory = self.latest_reply()
        self.assertEqual(set(entries), set(re.findall(r'#[^\s·]+', directory)))
        self.assertNotIn('#购买', directory)
        self.assertNotIn('#副本行动', directory)
        for registered in (False, True):
            if registered:
                self.register()
            before = dict(self.player()) if registered else None
            items = [dict(item) for item in self.items()]
            actions = self.store.db.execute('SELECT count(*) FROM game_actions').fetchone()[0]
            for entry in entries:
                with self.subTest(entry=entry, registered=registered):
                    self.assertEqual(1, self.send(entry))
                    text = self.latest_reply()
                    self.assertIn('↩️ 玩法总览：#修仙帮助', text)
                    self.assertNotIn('你还没有创建修士', text)
                    if not registered:
                        self.assertIsNone(self.player())
            self.assertEqual(before, dict(self.player()) if registered else None)
            self.assertEqual(items, [dict(item) for item in self.items()])
            self.assertEqual(actions, self.store.db.execute('SELECT count(*) FROM game_actions').fetchone()[0])

    def test_module_help_uses_current_rules_and_disabled_duel_status(self) -> None:
        rules = replace(DEFAULT_GAME_CONFIG, self_cultivation_interval_seconds=90,
                        exploration_cost=77, support_timeout_seconds=45,
                        support_minimum=11, support_maximum=99)
        self.assertIn('基础间隔 90 秒', service.format_module_help('cultivation_help', rules))
        self.assertIn('每次消耗 77 灵石', service.format_module_help('exploration_help', rules))
        self.assertIn('当前未开启', service._help_text(rules))
        self.assertIn('当前未开启', service.format_module_help('lightning_help', rules))
        enabled = replace(rules, duel_enabled=True)
        self.assertNotIn('当前未开启', service._help_text(enabled))
        detail = service.format_module_help('lightning_help', enabled)
        self.assertNotIn('当前未开启', detail)
        self.assertIn('45 秒支持窗口', detail)
        self.assertIn('11～99 灵石', detail)

    def test_registration_is_once_only_and_name_keys_are_normalized(self) -> None:
        self.register("alice", "Ａlice", message_id="100")
        first = self.player("alice")
        first_item = self.items("alice")[0]
        self.assertEqual((0, 100, "qi"), (first["cultivation"], first["spirit_stones"], first["realm"]))
        self.assertEqual(("qingfeng_jian", "artifact", "held"),
                         (first_item["template_id"], first_item["rarity"], first_item["state"]))
        self.assertRegex(first_item["item_id"], r"^F[0-9A-Fa-f]{8}$")

        # A second capture of the successful creation cannot mint another sword.
        self.send("#修仙 Ａlice", "alice", event_key="test:1", message_id="100")
        self.assertEqual(1, len(self.items("alice")))
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0])

        self.send("#修仙 alice", "bob", message_id="101")
        self.assertIsNone(self.player("bob"))
        self.send("#修仙 新道号", "alice", message_id="102")
        self.assertEqual("新道号", self.player("alice")["dao_name"])
        self.assertEqual(1, len(self.items("alice")))

    def test_cultivation_uses_beijing_days_and_breakthrough_failure_does_not_charge(self) -> None:
        self.register()
        self.send("#修炼", message_id="200")
        after_first = self.player()
        self.assertEqual((50, 200), (after_first["cultivation"], after_first["spirit_stones"]))
        self.send("#修炼", message_id="201")
        self.assertEqual((50, 200), (self.player()["cultivation"], self.player()["spirit_stones"]))

        self.now += timedelta(minutes=2).total_seconds()  # 23:59 -> 00:01 in Beijing
        self.send("#修炼", message_id="202")
        self.assertEqual((100, 300), (self.player()["cultivation"], self.player()["spirit_stones"]))
        self.send("#突破", message_id="203")
        self.assertEqual(("foundation", 0, 200),
                         (self.player()["realm"], self.player()["cultivation"], self.player()["spirit_stones"]))
        before = tuple(self.player()[key] for key in ("realm", "cultivation", "spirit_stones"))
        self.send("#突破", message_id="204")
        self.assertEqual(before, tuple(self.player()[key] for key in ("realm", "cultivation", "spirit_stones")))
        self.assertEqual(4, self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0])

    def test_self_cultivation_uses_an_attempt_cooldown_and_is_independent_of_daily_practice(self) -> None:
        # Fix this scenario's rewards so normal operator tuning does not
        # change its expected accounting. The production values stay configurable.
        self.router.game_config = replace(self.router.game_config,
            self_cultivation_success_percent=10, self_cultivation_reward=5, self_cultivation_loss=2)
        self.register(message_id="250")

        # A failure at zero does not go negative, but it is still an attempt
        # and therefore persists an action for the one-hour cooldown.
        self.rng = FixedRng(99)
        self.send("#自主修炼", message_id="251")
        self.assertEqual((0, 100), (self.player()["cultivation"], self.player()["spirit_stones"]))
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_actions WHERE action_kind='self_cultivate'").fetchone()[0])

        # Daily practice is separate: it remains available and does not reset
        # or bypass the self-cultivation attempt cooldown.
        self.send("#修炼", message_id="252")
        self.assertEqual((50, 200), (self.player()["cultivation"], self.player()["spirit_stones"]))
        self.now += 3_599
        self.rng = FixedRng(0)
        self.send("#自主修炼", message_id="253")
        self.assertEqual(50, self.player()["cultivation"])
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_actions WHERE action_kind='self_cultivate'").fetchone()[0])

        # Exactly one hour after the failed attempt is eligible.  The success
        # has no stone reward, and a same-message replay cannot add it twice.
        self.now += 1
        self.send("#自主修炼", event_key="self:success", message_id="254")
        self.assertEqual((55, 200), (self.player()["cultivation"], self.player()["spirit_stones"]))
        self.send("#自主修炼", event_key="self:replay", message_id="254")
        self.assertEqual(55, self.player()["cultivation"])
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM game_actions WHERE action_kind='self_cultivate'").fetchone()[0])

        self.now += 3_600
        self.rng = FixedRng(99)
        self.send("#自主修炼", message_id="255")
        self.assertEqual((53, 200), (self.player()["cultivation"], self.player()["spirit_stones"]))

    def test_exploration_rejects_insufficient_balance_or_full_bag_without_spending_daily_use(self) -> None:
        self.register()
        self.store.db.execute("UPDATE game_players SET spirit_stones=59 WHERE account_id=? AND group_id=? AND player_id=?",
                              (self.account, self.group, "alice"))
        self.store.db.commit()
        self.send("#秘境", message_id="300")
        self.assertIsNone(self.player()["last_explored_on"])
        self.assertEqual(59, self.player()["spirit_stones"])

        self.store.db.execute("UPDATE game_players SET spirit_stones=100 WHERE account_id=? AND group_id=? AND player_id=?",
                              (self.account, self.group, "alice"))
        for number in range(5):
            self.store.db.execute("""INSERT INTO game_items(item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
                VALUES(?,?,?,?,?,'alice','held')""", (f"F00000{number:02d}", self.account, self.group, f"fill{number}", "artifact"))
        self.store.db.commit()
        self.send("#秘境", message_id="301")
        self.assertIsNone(self.player()["last_explored_on"])
        self.assertEqual(100, self.player()["spirit_stones"])

    def test_rare_item_returns_to_pool_with_same_id_and_empty_pool_has_the_documented_fallback(self) -> None:
        self.register()
        self.rng = FixedRng(80)  # qi's ancient range is 80..94
        self.send("#秘境", message_id="400")
        rare = self.store.db.execute("SELECT * FROM game_items WHERE rarity='ancient' AND owner_player_id='alice'").fetchone()
        self.assertIsNotNone(rare)
        self.send(f"#献宝 {rare['item_id']}", message_id="401")
        pooled = self.store.db.execute("SELECT * FROM game_items WHERE item_id=?", (rare["item_id"],)).fetchone()
        self.assertEqual(("pool", None), (pooled["state"], pooled["owner_player_id"]))

        self.now += 24 * 60 * 60
        self.rng = FixedRng(80)
        self.send("#秘境", message_id="402")
        recovered = self.store.db.execute("SELECT * FROM game_items WHERE item_id=?", (rare["item_id"],)).fetchone()
        self.assertEqual(("held", "alice"), (recovered["state"], recovered["owner_player_id"]))

        # When every ancient template is held elsewhere, a player with no item receives a normal item, not stones.
        self.register("bob", "白墨", message_id="403")
        self.store.db.execute("DELETE FROM game_items WHERE account_id=? AND group_id=? AND owner_player_id='bob'",
                              (self.account, self.group))
        for number, template in enumerate(template for template in CATALOG.values() if template.rarity == "ancient"):
            existing = self.store.db.execute(
                "SELECT item_id FROM game_items WHERE account_id=? AND group_id=? AND template_id=?",
                (self.account, self.group, template.id),
            ).fetchone()
            if existing:
                self.store.db.execute("UPDATE game_items SET owner_player_id='alice',state='held' WHERE item_id=?", (existing["item_id"],))
            else:
                self.store.db.execute("""INSERT INTO game_items(item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
                    VALUES(?,?,?,?,?,'alice','held')""",
                    (f"F900000{number:02d}", self.account, self.group, template.id, "ancient"))
        self.store.db.execute("UPDATE game_players SET last_explored_on=NULL,spirit_stones=100 WHERE account_id=? AND group_id=? AND player_id='bob'",
                              (self.account, self.group))
        self.store.db.commit()
        self.rng = FixedRng(80)
        self.send("#秘境", "bob", message_id="404")
        bob_item = self.items("bob")[0]
        self.assertEqual("artifact", bob_item["rarity"])
        self.assertEqual(40, self.player("bob")["spirit_stones"])

    def test_offering_requires_the_owner_and_stays_in_its_group(self) -> None:
        self.register("alice", "青玄", message_id="500")
        item_id = self.items("alice")[0]["item_id"]
        self.register("bob", "白墨", message_id="501")
        self.send(f"#献宝 {item_id}", "bob", message_id="502")
        self.assertEqual("alice", self.store.db.execute("SELECT owner_player_id FROM game_items WHERE item_id=?", (item_id,)).fetchone()[0])
        self.register("alice", "异群", group=self.other_group, message_id="503")
        self.send(f"#献宝 {item_id}", "alice", group=self.other_group, message_id="504")
        self.assertEqual("alice", self.store.db.execute("SELECT owner_player_id FROM game_items WHERE item_id=?", (item_id,)).fetchone()[0])

    def test_message_id_deduplication_rejects_cross_capture_replays_and_body_conflicts(self) -> None:
        self.register(message_id="700")
        item_id = self.items()[0]["item_id"]
        self.send(f"#献宝 {item_id}", event_key="test:offer-a", message_id="701")
        self.assertEqual("retired", self.store.db.execute("SELECT state FROM game_items WHERE item_id=?", (item_id,)).fetchone()[0])
        stones = self.player()["spirit_stones"]
        self.send(f"#献宝 {item_id}", event_key="test:offer-b", message_id="701")
        self.assertEqual(stones, self.player()["spirit_stones"])
        self.send("#修仙 冲突", event_key="test:offer-c", message_id="701")
        self.assertNotEqual("冲突", self.player()["dao_name"])
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0])

    def test_outbox_failure_rolls_back_game_action_and_can_retry_once(self) -> None:
        command = self.message("#修仙 青玄", message_id="800")
        enqueue = self.router.outbox.enqueue_in_transaction
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.router.handle(command, "test", now=self.now)
        self.assertIsNone(self.player())
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0])
        self.router.outbox.enqueue_in_transaction = enqueue
        self.assertEqual(1, self.router.handle(command, "test", now=self.now))
        self.assertIsNotNone(self.player())
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0])

    def test_real_xiuxian_plugin_loads_and_dispatches_through_reply_router(self) -> None:
        plugins = load_plugins(Path(__file__).resolve().parents[2] / "plugins", ("xiuxian",))
        self.assertEqual("xiuxian", plugins[0].name)
        self.assertTrue(plugins[0].stateful)
        router = ReplyRouter(self.store, self.config, plugins, started_at=self.now - 1)
        with patch.object(service, "_RANDOM", FixedRng()):
            self.assertEqual(1, router.handle(self.message("#修仙 青玄", message_id="900"), "test", now=self.now))
        self.assertIsNotNone(self.player())
        self.assertIn("踏入仙途", self.latest_reply())

    def test_read_commands_do_not_mutate_assets_and_ranking_is_group_scoped(self) -> None:
        # Book and ranking intentionally remain useful before the reader creates a role.
        self.send("#宝录", "visitor", message_id="910")
        self.assertIn("本群稀有宝录", self.latest_reply())
        self.send("#仙榜", "visitor", message_id="911")
        self.assertIn("尚无人上榜", self.latest_reply())

        self.register("alice", "青玄", message_id="912")
        self.register("bob", "白墨", message_id="913")
        self.register("carol", "赤霄", message_id="914")
        self.register("outsider", "外群", group=self.other_group, message_id="915")
        self.store.db.execute("UPDATE game_players SET realm='foundation',cultivation=20 WHERE account_id=? AND group_id=? AND player_id='bob'",
                              (self.account, self.group))
        self.store.db.execute("UPDATE game_players SET cultivation=99 WHERE account_id=? AND group_id=? AND player_id='carol'",
                              (self.account, self.group))
        self.store.db.execute("UPDATE game_players SET realm='nascent',cultivation=999 WHERE account_id=? AND group_id=? AND player_id='outsider'",
                              (self.account, self.other_group))
        self.store.db.commit()
        before = (self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0],
                  tuple((row["item_id"], row["state"], row["owner_player_id"])
                        for row in self.store.db.execute("SELECT item_id,state,owner_player_id FROM game_items ORDER BY item_id")))
        item_id = self.items("alice")[0]["item_id"]
        for text in ("#修仙", "#法宝", f"#法宝 {item_id}", "#宝录", "#仙榜"):
            self.send(text, "alice")
        ranking = self.latest_reply()
        self.assertLess(ranking.index("白墨"), ranking.index("赤霄"))
        self.assertNotIn("外群", ranking)
        after = (self.store.db.execute("SELECT count(*) FROM game_actions").fetchone()[0],
                 tuple((row["item_id"], row["state"], row["owner_player_id"])
                       for row in self.store.db.execute("SELECT item_id,state,owner_player_id FROM game_items ORDER BY item_id")))
        self.assertEqual(before, after)

    def test_nonempty_inventory_gets_rare_pool_refunds_when_unique_items_are_held(self) -> None:
        self.register("alice", "青玄", message_id="920")
        self.register("bob", "白墨", message_id="921")
        for number, template in enumerate(template for template in CATALOG.values() if template.rarity in ("ancient", "treasure")):
            self.store.db.execute("""INSERT INTO game_items(item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
                VALUES(?,?,?,?,?,'bob','held')""",
                (f"F800000{number:02d}", self.account, self.group, template.id, template.rarity))
        self.store.db.commit()
        held_before = len(self.items("alice"))
        self.rng = FixedRng(80)  # ancient
        self.send("#秘境", "alice", message_id="922")
        self.assertEqual((held_before, 80), (len(self.items("alice")), self.player("alice")["spirit_stones"]))
        self.now += 24 * 60 * 60
        self.store.db.execute("UPDATE game_players SET spirit_stones=100,last_explored_on=NULL WHERE account_id=? AND group_id=? AND player_id='alice'",
                              (self.account, self.group))
        self.store.db.commit()
        self.rng = FixedRng(99)  # treasure
        self.send("#秘境", "alice", message_id="923")
        self.assertEqual((held_before, 90), (len(self.items("alice")), self.player("alice")["spirit_stones"]))

    def test_inviting_duel_does_not_lock_but_supporting_and_playing_lock_asset_commands(self) -> None:
        self.register("alice", "青玄", message_id="930")
        self.register("bob", "白墨", message_id="931")
        self.store.db.execute("""INSERT INTO game_duels(duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json)
            VALUES('D-stage2',?,?,?,?,?,?,?)""",
            (self.account, self.group, "alice", "bob", "inviting", 1, "{}"))
        self.store.db.commit()
        self.send("#修仙 待战可改", "alice", message_id="932")
        self.assertEqual("待战可改", self.player("alice")["dao_name"])
        self.store.db.execute("UPDATE game_players SET cultivation=100,spirit_stones=100,last_explored_on=NULL WHERE account_id=? AND group_id=? AND player_id='alice'",
                              (self.account, self.group))
        self.store.db.execute("UPDATE game_duels SET state='supporting' WHERE duel_id='D-stage2'")
        self.store.db.commit()
        item_id = self.items("alice")[0]["item_id"]
        before = (self.player("alice")["dao_name"], self.player("alice")["realm"],
                  self.player("alice")["cultivation"], self.player("alice")["spirit_stones"],
                  self.player("alice")["last_explored_on"], self.store.db.execute(
                      "SELECT state FROM game_items WHERE item_id=?", (item_id,)).fetchone()[0])
        for state in ("supporting", "playing"):
            self.store.db.execute("UPDATE game_duels SET state=? WHERE duel_id='D-stage2'", (state,))
            self.store.db.commit()
            for text in ("#修仙 不应改", "#突破", "#秘境", f"#献宝 {item_id}"):
                self.send(text, "alice")
            self.assertEqual(before, (self.player("alice")["dao_name"], self.player("alice")["realm"],
                                      self.player("alice")["cultivation"], self.player("alice")["spirit_stones"],
                                      self.player("alice")["last_explored_on"], self.store.db.execute(
                                          "SELECT state FROM game_items WHERE item_id=?", (item_id,)).fetchone()[0]))


if __name__ == "__main__":
    unittest.main()
