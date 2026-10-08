"""Visual-card integration at the transactional game reply boundary."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from wechat_receiver.games import duels, service
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.plugins import LoadedPlugin, reply_request_id
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 13
        + b"\x00" * 4 + b"\x00\x00\x00\x00IEND\xaeB`\x82")


class XiuxianVisualCardTests(unittest.TestCase):
    account = "wxid_bot"
    group = "visual@chatroom"
    other_group = "other@chatroom"
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc).timestamp()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "messages.sqlite3")
        self.config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group, self.other_group})),
            enabled_plugins=("xiuxian",), reply_ttl_seconds=60, max_message_age_seconds=120,
            game_config=replace(DEFAULT_GAME_CONFIG, visual_cards_enabled=True, duel_enabled=True),
        )
        self.plugin = LoadedPlugin("xiuxian", None, parse_command=parse_command,
            handle_command=service.handle_command, on_start=duels.on_start,
            on_before_messages=duels.on_before_messages, on_poll=duels.on_poll)
        self.router = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now - 1)
        self.router.start("visual-session", now=self.now)
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def message(self, text, player="alice", *, group=None):
        self.sequence += 1
        return Message(session_id="visual-session", event_key=f"visual:{self.sequence}", seq=self.sequence,
            call_id=self.sequence, source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None, content=text, raw_content=text,
            conversation_id=group or self.group, sender_id=player, direction="incoming",
            message_time_candidate=int(self.now), message_id_candidate=str(1000 + self.sequence),
            mentioned_ids=(), mention_state="none", history_state="live_candidate")

    def send(self, text, player="alice", *, group=None):
        return self.router.handle(self.message(text, player, group=group), "visual-session", now=self.now)

    def command(self):
        return self.store.db.execute("SELECT * FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()

    def seed_player(self, player, name, *, group=None, realm="qi", cultivation=0):
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,realm,cultivation,spirit_stones,joined_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (self.account, group or self.group, player, name, name.casefold(), realm,
                                               cultivation, 100, datetime.fromtimestamp(self.now, timezone.utc).isoformat()))

    def test_profile_uses_real_renderer_then_stages_an_image(self):
        self.send("#修仙 青玄")
        self.assertEqual(1, self.send("#修仙"))
        command = self.command()
        self.assertEqual("image", command["command_kind"])
        payload = json.loads(command["payload_json"])
        self.assertTrue(Path(payload["media_path"]).is_file())
        self.assertGreater(payload["media_bytes"], 0)

    def test_ranking_uses_ten_rows_but_passes_group_scoped_total(self):
        for number in range(10):
            self.seed_player(f"p{number}", f"修士{number}", cultivation=number)
        self.seed_player("outsider", "异群", group=self.other_group, cultivation=999)
        self.store.db.commit()
        fixture = self.root / "ranking.png"
        fixture.write_bytes(_PNG)
        with patch("wechat_receiver.games.renderer.render_ranking_card", return_value=fixture) as render:
            self.assertEqual(1, self.send("#仙榜", "visitor"))
        rows, = render.call_args.args
        self.assertEqual(10, len(rows))
        self.assertEqual(10, render.call_args.kwargs["total_count"])
        self.assertEqual(9, rows[0]["cultivation"])
        self.assertEqual("image", self.command()["command_kind"])

    def test_empty_ranking_still_uses_a_card(self):
        fixture = self.root / "empty.png"
        fixture.write_bytes(_PNG)
        with patch("wechat_receiver.games.renderer.render_ranking_card", return_value=fixture) as render:
            self.assertEqual(1, self.send("#仙榜", "visitor"))
        self.assertEqual([], render.call_args.args[0])
        self.assertEqual(0, render.call_args.kwargs["total_count"])
        self.assertEqual("image", self.command()["command_kind"])

    def test_visual_off_and_profile_renderer_failure_keep_text_replies(self):
        self.send("#修仙 青玄")
        self.router.game_config = replace(self.router.game_config, visual_cards_enabled=False)
        self.assertEqual(1, self.send("#修仙"))
        self.assertEqual("text", self.command()["command_kind"])
        self.router.game_config = replace(self.router.game_config, visual_cards_enabled=True)
        with patch("wechat_receiver.games.renderer.render_profile_card", side_effect=RuntimeError("bad template")):
            self.assertEqual(1, self.send("#修仙"))
        self.assertEqual("text", self.command()["command_kind"])

    def seed_settled_duel(self, duel_id, *, reason, loss, state="settled"):
        self.seed_player("alice", "青玄", cultivation=50)
        self.seed_player("bob", "白墨", cultivation=50)
        item_id = "F" + duel_id[1:]
        self.store.db.execute("""INSERT INTO game_items
            (item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
            VALUES(?,?,?,?,?,?,?)""", (item_id, self.account, self.group, "qingfeng_jian", "artifact", "alice", "held"))
        rules = json.dumps({"settlement": {"cultivation_loss": loss}})
        self.store.db.execute("""INSERT INTO game_duels
            (duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json,
             winner_player_id,loser_player_id,loot_item_id,final_reason,prompt_request_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (duel_id, self.account, self.group, "alice", "bob", state,
                2, rules, "alice" if state == "settled" else None, "bob" if state == "settled" else None,
                item_id if state == "settled" else None, reason,
                reply_request_id(self.account, "xiuxian", f"duel:{duel_id}:result"),
                datetime.fromtimestamp(self.now, timezone.utc).isoformat()))
        self.store.db.commit()

    def test_settled_duels_use_text_without_rendering_even_when_visual_cards_are_enabled(self):
        for duel_id, reason, loss in (("D11111111", "lightning", 17), ("D22222222", "timeout", 3)):
            with self.subTest(reason=reason):
                self.seed_settled_duel(duel_id, reason=reason, loss=loss)
                with patch("wechat_receiver.games.renderer.render_duel_card") as render:
                    self.assertEqual(1, self.router.tick("visual-session", now=self.now))
                render.assert_not_called()
                command = self.command()
                self.assertEqual("text", command["command_kind"])
                self.assertIn(f"白墨损失 {loss} 修为", command["text"])
                self.assertIn("青玄获胜", command["text"])
                self.assertIn("青锋剑", command["text"])
                if reason == "timeout":
                    self.assertIn("引雷超时", command["text"])
                self.assertEqual(reply_request_id(self.account, "xiuxian", f"duel:{duel_id}:result"),
                                 command["request_id"])
                self.assertEqual(0, self.router.tick("visual-session", now=self.now))
                self.store.db.execute("DELETE FROM game_duels WHERE account_id=? AND group_id=?", (self.account, self.group))
                self.store.db.execute("DELETE FROM game_items WHERE account_id=? AND group_id=?", (self.account, self.group))
                self.store.db.execute("DELETE FROM game_players WHERE account_id=? AND group_id=?", (self.account, self.group))
                self.store.db.commit()

    def test_cancelled_duel_keeps_text_result(self):
        self.seed_settled_duel("D33333333", reason="declined", loss=0, state="cancelled")
        self.assertEqual(1, self.router.tick("visual-session", now=self.now))
        self.assertEqual("text", self.command()["command_kind"])
