"""Durable stage-three duel behaviour through the real reply router.

The assertions deliberately inspect game state and assets instead of reply
copy.  Prompt delivery is advanced through the local outbox; no sender is
started and no wall-clock waits are used.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from wechat_receiver.games import duels
from wechat_receiver.games.commands import parse_command
from wechat_receiver.games.config import DEFAULT_GAME_CONFIG
from wechat_receiver.models import Message
from wechat_receiver.outbox import Outbox
from wechat_receiver.plugins import LoadedPlugin
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class FixedRng:
    def __init__(self, lightning: int = 6, loot_index: int = 0) -> None:
        self.lightning = lightning
        self.loot_index = loot_index
        self.choice_inputs: list[tuple[str, ...]] = []

    def randint(self, start: int, end: int) -> int:
        if (start, end) != (1, 6):
            raise AssertionError((start, end))
        return self.lightning

    def choice(self, items):
        values = tuple(items)
        self.choice_inputs.append(tuple(item["item_id"] for item in values))
        return values[self.loot_index]


class XiuxianDuelTests(unittest.TestCase):
    account = "wxid_bot"
    group = "22913213991@chatroom"
    base = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        self.now = self.base.timestamp()
        self.session = "duel-test"
        self.sequence = 0
        self.rng = FixedRng()
        self.config = SimpleNamespace(
            sender=SimpleNamespace(account_id=self.account, allowed_targets=frozenset({self.group})),
            enabled_plugins=("xiuxian",), reply_ttl_seconds=60, max_message_age_seconds=120,
            game_config=replace(DEFAULT_GAME_CONFIG, duel_enabled=True, daily_duel_limit=3,
                                pair_daily_duel_limit=2),
        )
        self.plugin = LoadedPlugin(
            "xiuxian", None, parse_command=parse_command,
            handle_command=lambda command, context: duels.handle_command(command, context, rng=self.rng),
            on_start=duels.on_start, on_before_messages=duels.on_before_messages, on_poll=duels.on_poll,
        )
        self.router = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now - 1)
        # Recovery is deliberately before seeding: startup must cancel only
        # already persisted work from an earlier process.
        self.router.start(self.session, now=self.now)
        self.seed_player("alice", "青玄")
        self.seed_player("bob", "白墨")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def seed_player(self, player_id: str, name: str) -> None:
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones,joined_at)
            VALUES(?,?,?,?,?,?,?)""", (self.account, self.group, player_id, name, name.casefold(), 100,
                                         datetime.fromtimestamp(self.now, timezone.utc).isoformat()))
        self.seed_item(player_id, {"alice": "F00000001", "bob": "F00000002"}.get(player_id, "F00000003"))
        self.store.db.commit()

    def seed_observer(self, player_id: str, name: str) -> None:
        """Register a supporter without giving them a duel-eligible item."""
        self.store.db.execute("""INSERT INTO game_players
            (account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones,joined_at)
            VALUES(?,?,?,?,?,?,?)""", (self.account, self.group, player_id, name if len(name) >= 2 else name + "修", name.casefold(), 100,
                                         datetime.fromtimestamp(self.now, timezone.utc).isoformat()))
        self.store.db.commit()

    def seed_item(self, player_id: str, item_id: str) -> None:
        self.store.db.execute("""INSERT INTO game_items
            (item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
            VALUES(?,?,?,?,? ,?,'held')""",
            (item_id, self.account, self.group, "qingfeng_jian", "artifact", player_id))

    def seed_finished_duel(self, duel_id: str, challenger: str, challenged: str, *, counted_on: str,
                           created_at: str | None = None) -> None:
        self.store.db.execute("""INSERT INTO game_duels
            (duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json,
             counted_on,created_at)
            VALUES(?,?,?,?,?,'settled',1,'{}',?,?)""",
            (duel_id, self.account, self.group, challenger, challenged, counted_on,
             created_at or datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace("+00:00", "Z")))
        self.store.db.commit()

    def message(self, text: str, player: str, *, target: str | None = None,
                event_key: str | None = None, message_id: str | None = None,
                observed_at: float | None = None) -> Message:
        self.sequence += 1
        number = self.sequence
        challenge = target is not None
        return Message(
            session_id=self.session, event_key=event_key or f"{self.session}:{number}", seq=number, call_id=number,
            source="receive_batch", event_kind="item", observed_at_ms=int((self.now if observed_at is None else observed_at) * 1000),
            message_type=1, message_kind="text", app_message_type=None, content=text, raw_content=text,
            conversation_id=self.group, sender_id=player, direction="incoming",
            message_time_candidate=int(self.now), message_id_candidate=message_id or str(90_000 + number),
            mentioned_ids=(target,) if challenge else (),
            mention_state="explicit_other" if challenge else "none", history_state="live_candidate",
        )

    def send(self, text: str, player: str, **kwargs) -> int:
        return self.router.handle(self.message(text, player, **kwargs), self.session, now=self.now)

    def duel(self):
        return self.store.db.execute("SELECT * FROM game_duels WHERE account_id=? AND group_id=? ORDER BY created_at DESC,duel_id DESC LIMIT 1",
                                     (self.account, self.group)).fetchone()

    def held(self, player: str) -> list[str]:
        return [row[0] for row in self.store.db.execute("""SELECT item_id FROM game_items
            WHERE account_id=? AND group_id=? AND owner_player_id=? AND state='held' ORDER BY item_id""",
            (self.account, self.group, player)).fetchall()]

    def accept_latest_prompt(self, *, completed_at: float | None = None) -> None:
        request_id = self.duel()["prompt_request_id"]
        self.assertIsNotNone(request_id)
        outbox = Outbox(self.store.db)
        at = self.now if completed_at is None else completed_at
        claim = outbox.claim_next(self.account, self.session, now=datetime.fromtimestamp(self.now, timezone.utc),
                                  request_id=request_id)
        self.assertIsNotNone(claim)
        outbox.record_result(claim["request_id"], claim["attempt_id"], "accepted",
                             now=datetime.fromtimestamp(at, timezone.utc))

    def create_invitation(self) -> None:
        self.assertEqual(1, self.send("#斗法 @白墨\u2005", "alice", target="bob"))
        self.assertEqual("inviting", self.duel()["state"])
        self.accept_latest_prompt()

    def open_playing(self) -> None:
        """Deliver invitation/support/turn prompts without a real sender."""
        self.create_invitation()
        self.assertEqual(1, self.send("#接受斗法", "bob"))
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.assertEqual("playing", self.duel()["state"])
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.assertIsNotNone(self.duel()["phase_opened_at"])

    def open_supporting(self) -> None:
        self.create_invitation()
        self.assertEqual(1, self.send("#接受斗法", "bob"))
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual("supporting", self.duel()["state"])

    def stones(self, player: str) -> int:
        return self.store.db.execute("SELECT spirit_stones FROM game_players WHERE player_id=?", (player,)).fetchone()[0]

    def latest_reply(self) -> str:
        return self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def support(self, player: str, target: str, amount: int, **kwargs) -> int:
        return self.send(f"#支持 @{target}\u2005 {amount}", player, target=target, **kwargs)

    def test_challenge_is_group_scoped_and_acceptance_rechecks_assets(self) -> None:
        self.create_invitation()
        # The invitation does not lock assets.  Acceptance must re-check both
        # players, so a now-empty target cannot enter an accepted duel.
        self.store.db.execute("UPDATE game_items SET state='retired',owner_player_id=NULL WHERE owner_player_id='bob'")
        self.store.db.commit()
        self.assertEqual(1, self.send("#接受斗法", "bob"))
        self.assertIn(self.duel()["state"], {"cancelled", "settled"})
        self.assertEqual([], self.held("bob"))
        self.assertEqual(["F00000001"], self.held("alice"))

    def test_only_participants_can_accept_reject_cancel_or_lightning(self) -> None:
        self.seed_player("carol", "赤霄")
        self.create_invitation()
        self.assertEqual(1, self.send("#接受斗法", "carol"))
        self.assertEqual("inviting", self.duel()["state"])
        self.assertEqual(1, self.send("#取消斗法", "bob"))
        self.assertEqual("inviting", self.duel()["state"])
        self.assertEqual(1, self.send("#引雷", "bob"))
        self.assertEqual("inviting", self.duel()["state"])

    def test_cross_event_replay_cannot_advance_a_turn(self) -> None:
        self.open_playing()
        before = self.duel()["next_turn"]
        self.send("#引雷", "alice", event_key="replay:a", message_id="777")
        after_first = self.duel()["next_turn"]
        self.send("#引雷", "alice", event_key="replay:b", message_id="777")
        self.assertNotEqual(before, after_first)
        self.assertEqual(after_first, self.duel()["next_turn"])

    def test_six_turns_end_once_and_transfer_one_item_to_a_seven_item_winner(self) -> None:
        # Bob can legally begin at the six-item cap and may hold seven after
        # receiving the one random loss item; no bonus resources are awarded.
        for index in range(3, 8):
            self.seed_item("alice", f"F0000000{index}")
        self.store.db.commit()
        self.open_playing()
        self.assertEqual({("F00000001", "F00000003", "F00000004", "F00000005", "F00000006", "F00000007"),
                          ("F00000002",)}, set(self.rng.choice_inputs))
        stones = {player: self.store.db.execute("SELECT spirit_stones FROM game_players WHERE player_id=?", (player,)).fetchone()[0]
                  for player in ("alice", "bob")}
        for turn, player in enumerate(("alice", "bob", "alice", "bob", "alice", "bob"), 1):
            self.assertEqual(1, self.send("#引雷", player, message_id=str(80_000 + turn)))
            if turn < 6:
                self.accept_latest_prompt()
                self.router.before_messages(self.session, now=self.now)
        duel = self.duel()
        self.assertEqual(("settled", 6, "alice", "bob"),
                         (duel["state"], duel["lightning_position"], duel["winner_player_id"], duel["loser_player_id"]))
        self.assertEqual(7, len(self.held("alice")))
        self.assertEqual(0, len(self.held("bob")))
        self.assertEqual(stones, {player: self.store.db.execute("SELECT spirit_stones FROM game_players WHERE player_id=?", (player,)).fetchone()[0]
                                  for player in ("alice", "bob")})

    def _assert_lightning_position(self, position: int) -> None:
        self.rng.lightning = position
        self.open_playing()
        for turn in range(1, position + 1):
            player = "alice" if turn % 2 else "bob"
            self.assertEqual(1, self.send("#引雷", player, message_id=str(70_000 + turn)))
            if turn < position:
                self.accept_latest_prompt()
                self.router.before_messages(self.session, now=self.now)
        duel = self.duel()
        loser = "alice" if position % 2 else "bob"
        winner = "bob" if loser == "alice" else "alice"
        self.assertEqual(("settled", position, winner, loser),
                         (duel["state"], duel["lightning_position"], duel["winner_player_id"], duel["loser_player_id"]))
        self.assertEqual((0, 2) if loser == "alice" else (2, 0),
                         (len(self.held("alice")), len(self.held("bob"))))

    def test_lightning_position_1(self) -> None:
        self._assert_lightning_position(1)

    def test_lightning_position_2(self) -> None:
        self._assert_lightning_position(2)

    def test_lightning_position_3(self) -> None:
        self._assert_lightning_position(3)

    def test_lightning_position_4(self) -> None:
        self._assert_lightning_position(4)

    def test_lightning_position_5(self) -> None:
        self._assert_lightning_position(5)

    def test_lightning_position_6(self) -> None:
        self._assert_lightning_position(6)

    def test_turn_timeout_transfers_one_item(self) -> None:
        self.store.db.execute("UPDATE game_players SET cultivation=12 WHERE player_id='alice'")
        self.store.db.execute("UPDATE game_players SET cultivation=37 WHERE player_id='bob'")
        self.store.db.commit()
        self.open_playing()
        deadline = datetime.fromisoformat(self.duel()["phase_deadline_at"].replace("Z", "+00:00")).timestamp()
        while self.now < deadline:
            self.now = min(deadline, self.now + 4)
            self.router.tick(self.session, now=self.now)
        duel = self.duel()
        self.assertEqual(("settled", "bob", "alice", "timeout"),
                         (duel["state"], duel["winner_player_id"], duel["loser_player_id"], duel["final_reason"]))
        self.assertEqual((0, 2), (len(self.held("alice")), len(self.held("bob"))))
        self.assertEqual([0, 37], [r[0] for r in self.store.db.execute(
            'SELECT cultivation FROM game_players ORDER BY player_id')])

    def test_lightning_loss_deducts_twenty_cultivation_once_without_winner_reward(self) -> None:
        self.store.db.execute("UPDATE game_players SET cultivation=25 WHERE player_id='alice'")
        self.store.db.execute("UPDATE game_players SET cultivation=37 WHERE player_id='bob'")
        self.store.db.commit()
        self.rng.lightning = 1
        self.open_playing()
        self.send('#引雷', 'alice', message_id='20100')
        self.send('#引雷', 'alice', message_id='20100', event_key='replayed-loss')
        self.assertEqual([5, 37], [r[0] for r in self.store.db.execute(
            'SELECT cultivation FROM game_players ORDER BY player_id')])

    def test_prompt_is_not_open_until_completed_and_failure_cancels_without_loot(self) -> None:
        self.assertEqual(1, self.send("#斗法 @白墨\u2005", "alice", target="bob"))
        self.assertIsNone(self.duel()["phase_opened_at"])
        self.assertEqual(1, self.send("#接受斗法", "bob"))
        self.assertEqual("inviting", self.duel()["state"])
        request_id = self.duel()["prompt_request_id"]
        outbox = Outbox(self.store.db)
        claim = outbox.claim_next(self.account, self.session, now=datetime.fromtimestamp(self.now, timezone.utc), request_id=request_id)
        outbox.record_result(claim["request_id"], claim["attempt_id"], "failed", now=datetime.fromtimestamp(self.now, timezone.utc))
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual("cancelled", self.duel()["state"])
        self.assertEqual((1, 1), (len(self.held("alice")), len(self.held("bob"))))

    def test_unknown_prompt_result_and_changed_session_cancel_without_loot(self) -> None:
        self.assertEqual(1, self.send("#斗法 @白墨\u2005", "alice", target="bob"))
        request_id = self.duel()["prompt_request_id"]
        outbox = Outbox(self.store.db)
        claim = outbox.claim_next(self.account, self.session, now=datetime.fromtimestamp(self.now, timezone.utc), request_id=request_id)
        outbox.record_result(claim["request_id"], claim["attempt_id"], "unknown", now=datetime.fromtimestamp(self.now, timezone.utc))
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual("cancelled", self.duel()["state"])
        self.assertEqual((1, 1), (len(self.held("alice")), len(self.held("bob"))))

        # A live duel is also tied to the receiver session that created it.
        self.seed_player("carol", "赤霄")
        self.now += 61
        self.assertEqual(1, self.send("#斗法 @赤霄\u2005", "alice", target="carol"))
        self.router.before_messages("other-session", now=self.now)
        self.assertEqual("cancelled", self.duel()["state"])

    def test_completed_at_controls_opening_and_stalled_poll_cancels(self) -> None:
        self.assertEqual(1, self.send("#斗法 @白墨\u2005", "alice", target="bob"))
        submitted_at = self.now + 2
        self.accept_latest_prompt(completed_at=submitted_at)
        self.now += 3
        self.router.before_messages(self.session, now=self.now)
        opened = datetime.fromisoformat(self.duel()["phase_opened_at"].replace("Z", "+00:00")).timestamp()
        self.assertEqual(submitted_at, opened)
        self.router.tick(self.session, now=self.now)
        self.now += 6
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual("cancelled", self.duel()["state"])

    def test_log_catchup_does_not_cancel_other_members_invitation_or_acceptance(self) -> None:
        self.config.admin_ids = frozenset({'alice'})
        self.send('#斗法 @青玄\u2005', 'bob', target='alice')
        self.accept_latest_prompt()
        for issue in ('receiver_log_unread:12_bytes', 'receiver_log_incomplete_line'):
            self.router.before_messages(self.session, now=self.now, runtime_issue=issue)
            self.router.tick(self.session, now=self.now, runtime_issue=issue)
            self.assertEqual('inviting', self.duel()['state'])
        self.send('#接受斗法', 'alice')
        self.assertEqual('supporting', self.duel()['state'])
        self.accept_latest_prompt()
        self.now += .5
        self.router.before_messages(self.session, now=self.now,
                                   runtime_issue='receiver_log_incomplete_line')
        self.assertEqual('supporting', self.duel()['state'])
        self.router.before_messages(self.session, now=self.now + .5, runtime_issue=None)
        self.assertEqual('supporting', self.duel()['state'])
        self.assertIsNotNone(self.duel()['phase_opened_at'])

    def test_pending_tail_defers_timeout_until_on_time_lightning_is_dispatched(self) -> None:
        self.open_playing()
        deadline = datetime.fromisoformat(self.duel()['phase_deadline_at'].replace('Z', '+00:00')).timestamp()
        while self.now + 4 < deadline:
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.now = deadline - .1
        self.router.before_messages(self.session, now=self.now,
                                   runtime_issue='receiver_log_incomplete_line')
        self.now = deadline + .5
        self.router.tick(self.session, now=self.now, runtime_issue='receiver_log_unread:20_bytes')
        self.assertEqual('playing', self.duel()['state'])
        self.assertEqual((1, 1), (len(self.held('alice')), len(self.held('bob'))))
        self.router.before_messages(self.session, now=self.now, runtime_issue=None)
        self.send('#引雷', 'alice', observed_at=deadline - .001)
        self.router.tick(self.session, now=self.now)
        self.assertEqual(('playing', 2, 'bob'),
                         (self.duel()['state'], self.duel()['next_turn'], self.duel()['current_player_id']))

    def test_persistent_tail_stall_cancels_and_refunds_support_once(self) -> None:
        self.seed_observer('carol', '旁观者')
        self.open_supporting()
        self.support('carol', 'alice', 50)
        for offset in range(7):
            # A changing byte count or half-line must not reset the grace period.
            issue = f'receiver_log_unread:{20 + offset}_bytes' if offset % 2 else 'receiver_log_incomplete_line'
            self.router.before_messages(self.session, now=self.now + offset, runtime_issue=issue)
            self.router.tick(self.session, now=self.now + offset, runtime_issue=issue)
        self.assertEqual(('cancelled', 'technical:receiver_stalled'),
                         (self.duel()['state'], self.duel()['final_reason']))
        self.assertEqual(100, self.stones('carol'))
        self.assertEqual(('refunded', 50), tuple(self.store.db.execute(
            'SELECT settlement_state,payout FROM game_supports').fetchone()))
        self.assertEqual((1, 1), (len(self.held('alice')), len(self.held('bob'))))

    def test_real_receiver_error_still_cancels_immediately(self) -> None:
        self.create_invitation()
        with self.assertLogs(level='WARNING') as logs:
            self.router.before_messages(self.session, now=self.now,
                                       runtime_issue='receiver_read_error:OSError: private path')
        self.assertEqual(('cancelled', 'technical:runtime'),
                         (self.duel()['state'], self.duel()['final_reason']))
        self.assertIn('runtime=receiver_read_error', logs.output[0])
        self.assertNotIn('private path', logs.output[0])
        self.assertEqual((1, 1), (len(self.held('alice')), len(self.held('bob'))))

    def test_prompt_receipt_committed_during_lock_wait_uses_fresh_time(self) -> None:
        self.send('#斗法 @白墨\u2005', 'alice', target='bob')
        self.accept_latest_prompt(completed_at=self.now + .1)
        locked = False

        def trace(sql):
            nonlocal locked
            if sql == 'BEGIN IMMEDIATE':
                locked = True

        def clock():
            return self.now + (.2 if locked else 0)

        self.store.db.set_trace_callback(trace)
        try:
            with patch('wechat_receiver.reply_router.time.time', side_effect=clock):
                self.router.before_messages(self.session)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual('inviting', self.duel()['state'])
        opened = datetime.fromisoformat(self.duel()['phase_opened_at'].replace('Z', '+00:00')).timestamp()
        self.assertEqual(self.now + .1, opened)

    def test_invalid_future_prompt_receipt_remains_unsafe_with_explicit_time(self) -> None:
        self.send('#斗法 @白墨\u2005', 'alice', target='bob')
        self.accept_latest_prompt(completed_at=self.now + 2)
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual('technical:invalid_submission_time', self.duel()['final_reason'])

    def test_accept_command_refreshes_clock_after_receipt_lock_race(self) -> None:
        self.send('#斗法 @白墨\u2005', 'alice', target='bob')
        self.accept_latest_prompt(completed_at=self.now + .1)
        message = self.message('#接受斗法', 'bob', observed_at=self.now + .15)
        locked = False

        def trace(sql):
            nonlocal locked
            if sql == 'BEGIN IMMEDIATE':
                locked = True

        self.store.db.set_trace_callback(trace)
        try:
            with patch('wechat_receiver.reply_router.time.time',
                       side_effect=lambda: self.now + (.2 if locked else 0)):
                self.router.handle(message, self.session)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual('supporting', self.duel()['state'])

    def test_outbox_enqueue_failure_rolls_back_new_invitation(self) -> None:
        command = self.message("#斗法 @白墨\u2005", "alice", target="bob")
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.router.handle(command, self.session, now=self.now)
        self.assertIsNone(self.duel())

    def test_invitation_cooldown_and_daily_limit_are_enforced(self) -> None:
        today = datetime.fromtimestamp(self.now, timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat()
        recent = datetime.fromtimestamp(self.now - 30, timezone.utc).isoformat().replace("+00:00", "Z")
        self.seed_finished_duel("D00000001", "alice", "bob", counted_on=today, created_at=recent)
        self.assertEqual(1, self.send("#斗法 @白墨\u2005", "alice", target="bob"))
        self.assertEqual(1, self.store.db.execute("SELECT count(*) FROM game_duels").fetchone()[0])

        self.store.db.execute("DELETE FROM game_duels")
        for index in range(5):
            self.seed_finished_duel(f"D0000001{index}", "alice", "bob", counted_on=today,
                                    created_at=datetime.fromtimestamp(self.now - 120 - index, timezone.utc).isoformat().replace("+00:00", "Z"))
        self.send("#斗法 @白墨\u2005", "alice", target="bob", message_id="606")
        self.assertEqual(5, self.store.db.execute("SELECT count(*) FROM game_duels").fetchone()[0])

    def test_player_pair_limits_and_beijing_acceptance_date(self) -> None:
        today = datetime.fromtimestamp(self.now, timezone.utc).astimezone(timezone(timedelta(hours=8))).date().isoformat()
        self.seed_player("carol", "赤霄")
        for index in range(3):
            self.seed_finished_duel(f"D0000002{index}", "alice", "carol", counted_on=today)
        self.send("#斗法 @白墨\u2005", "alice", target="bob")
        self.assertEqual(3, self.store.db.execute("SELECT count(*) FROM game_duels").fetchone()[0])

        self.store.db.execute("DELETE FROM game_duels")
        for index in range(2):
            self.seed_finished_duel(f"D0000003{index}", "alice", "bob", counted_on=today)
        self.send("#斗法 @白墨\u2005", "alice", target="bob", message_id="607")
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM game_duels").fetchone()[0])

        self.store.db.execute("DELETE FROM game_duels")
        self.store.db.commit()
        self.now = datetime(2026, 9, 18, 15, 59, 30, tzinfo=timezone.utc).timestamp()
        self.create_invitation()
        # Cross midnight while the 60-second invitation is still valid.
        self.now += 31
        self.assertEqual(1, self.send("#接受斗法", "bob"))
        self.assertEqual("2026-09-19", self.duel()["counted_on"])

    def test_reject_cancel_and_technical_cancel_release_assets_and_count_reservations(self) -> None:
        self.create_invitation()
        self.assertEqual(1, self.send("#拒绝斗法", "bob"))
        self.assertEqual(("cancelled", ["F00000001"], ["F00000002"]),
                         (self.duel()["state"], self.held("alice"), self.held("bob")))

        self.now += 61
        self.create_invitation()
        self.assertEqual(1, self.send("#取消斗法", "alice"))
        self.assertEqual("cancelled", self.duel()["state"])

        self.now += 61
        self.create_invitation()
        self.send("#接受斗法", "bob")
        self.router.before_messages("changed-session", now=self.now)
        self.assertEqual("cancelled", self.duel()["state"])
        self.assertEqual(0, self.store.db.execute("""SELECT count(*) FROM game_duels
            WHERE state IN ('supporting','playing','settled') AND counted_on IS NOT NULL""").fetchone()[0])

    def test_final_result_outbox_failure_rolls_back_loot_and_reuses_persisted_choice(self) -> None:
        self.rng.lightning = 1
        self.open_playing()
        selected = tuple(self.rng.choice_inputs)
        command = self.message("#引雷", "alice", event_key="final:1", message_id="888")
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.router.handle(command, self.session, now=self.now)
        self.assertEqual(("playing", ["F00000001"], ["F00000002"]),
                         (self.duel()["state"], self.held("alice"), self.held("bob")))
        self.assertEqual(selected, tuple(self.rng.choice_inputs))

    def test_supports_pay_winner_principal_and_loser_pool_by_share(self) -> None:
        for player, name in (("carol", "甲"), ("dave", "乙"), ("eve", "丙")):
            self.seed_observer(player, name)
        self.rng.lightning = 2  # Alice survives, Bob loses, so Alice's pool wins.
        self.open_supporting()
        self.assertEqual(1, self.support("carol", "alice", 30))
        self.assertEqual(1, self.support("dave", "alice", 70))
        self.assertEqual(1, self.support("eve", "bob", 100))
        self.assertEqual((70, 30, 0), (self.stones("carol"), self.stones("dave"), self.stones("eve")))
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.send("#引雷", "alice")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.send("#引雷", "bob")
        self.assertEqual((130, 170, 0), (self.stones("carol"), self.stones("dave"), self.stones("eve")))
        rows = self.store.db.execute("SELECT settlement_state,payout FROM game_supports ORDER BY supporter_id").fetchall()
        self.assertEqual([( "paid", 60), ("paid", 140), ("paid", 0)], [tuple(row) for row in rows])

    def test_support_gate_rejects_participants_duplicates_and_closed_windows_without_charging(self) -> None:
        self.seed_observer("carol", "甲")
        self.create_invitation()
        self.support("carol", "alice", 50)
        self.assertEqual(100, self.stones("carol"))
        self.send("#接受斗法", "bob")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.support("alice", "bob", 50)
        self.assertEqual(100, self.stones("alice"))
        self.assertEqual(1, self.support("carol", "alice", 50))
        self.assertEqual(50, self.stones("carol"))
        self.support("carol", "bob", 50)
        self.assertEqual(50, self.stones("carol"))
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.support("carol", "alice", 10)
        self.assertEqual(50, self.stones("carol"))

    def test_single_side_and_timeout_refund_supports_once(self) -> None:
        self.seed_observer("carol", "甲")
        self.rng.lightning = 2
        self.open_supporting()
        self.support("carol", "alice", 50)
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        # One-sided support is always principal-only, even on a normal win.
        self.send("#引雷", "alice")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.send("#引雷", "bob")
        self.assertEqual(100, self.stones("carol"))
        self.assertEqual(("refunded", 50), tuple(self.store.db.execute(
            "SELECT settlement_state,payout FROM game_supports").fetchone()))

    def test_timeout_and_restart_refund_supports_idempotently(self) -> None:
        self.seed_observer("carol", "甲")
        self.open_supporting()
        self.support("carol", "alice", 50)
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        deadline = datetime.fromisoformat(self.duel()["phase_deadline_at"].replace("Z", "+00:00")).timestamp()
        while self.now < deadline:
            self.now = min(deadline, self.now + 4)
            self.router.tick(self.session, now=self.now)
        self.assertEqual(100, self.stones("carol"))
        self.router.tick(self.session, now=self.now + 1)
        self.assertEqual(100, self.stones("carol"))
        self.assertEqual(("refunded", 50), tuple(self.store.db.execute(
            "SELECT settlement_state,payout FROM game_supports").fetchone()))

    def test_restart_refunds_pending_support_once(self) -> None:
        self.seed_observer("carol", "甲")
        self.open_supporting()
        self.support("carol", "alice", 50)
        recovered = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now)
        recovered.start("restarted", now=self.now + 1)
        self.assertEqual(("cancelled", 100, "refunded", 50),
                         (self.duel()["state"], self.stones("carol"), *tuple(self.store.db.execute(
                             "SELECT settlement_state,payout FROM game_supports").fetchone())))
        recovered.tick("restarted", now=self.now + 2)
        self.assertEqual(100, self.stones("carol"))

    def test_final_support_result_queue_failure_rolls_back_all_assets(self) -> None:
        self.seed_observer("carol", "甲")
        self.rng.lightning = 1
        self.open_supporting()
        self.support("carol", "bob", 50)
        for _ in range(8):
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.store.db.execute("UPDATE game_players SET cultivation=40 WHERE player_id='alice'")
        self.store.db.commit()
        command = self.message("#引雷", "alice", event_key="support-final:1", message_id="889")
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.router.handle(command, self.session, now=self.now)
        self.assertEqual(("playing", 40, ["F00000001"], ["F00000002"], 50),
                         (self.duel()["state"], self.store.db.execute("SELECT cultivation FROM game_players WHERE player_id='alice'").fetchone()[0],
                          self.held("alice"), self.held("bob"), self.stones("carol")))
        self.assertEqual("pending", self.store.db.execute("SELECT settlement_state FROM game_supports").fetchone()[0])
        self.assertEqual(1, self.router.handle(command, self.session, now=self.now))
        self.assertEqual(("settled", 20, [], ["F00000001", "F00000002"], 100),
                         (self.duel()["state"], self.store.db.execute("SELECT cultivation FROM game_players WHERE player_id='alice'").fetchone()[0],
                          self.held("alice"), self.held("bob"), self.stones("carol")))

    def test_history_second_page_is_group_scoped_and_limits_supporter_details(self) -> None:
        # A public completed duel may have many supports, but a page exposes no
        # more than ten records and cannot look up the same ID in another group.
        self.seed_finished_duel("DABCDEF01", "alice", "bob", counted_on="2026-09-18")
        for index in range(12):
            player = f"viewer{index}"
            self.seed_observer(player, f"观{index}")
            self.store.db.execute("""INSERT INTO game_supports
                (duel_id,account_id,group_id,supporter_id,supported_player_id,amount,payout,settlement_state)
                VALUES('DABCDEF01',?,?,?,?,?,?,'paid')""", (self.account, self.group, player, "alice", 10, 10))
        self.store.db.commit()
        self.assertEqual(1, self.send("#战绩 DABCDEF01 2", "alice"))
        self.assertLessEqual(self.latest_reply().count("到账"), 10)

    def test_deadline_uses_capture_time_not_later_router_processing_time(self) -> None:
        self.open_playing()
        deadline = datetime.fromisoformat(self.duel()["phase_deadline_at"].replace("Z", "+00:00")).timestamp()
        while self.now + 4 < deadline - 1:
            self.now += 4
            self.router.tick(self.session, now=self.now)
        self.now = deadline - 1
        self.router.tick(self.session, now=self.now)
        self.now = deadline + 1
        self.assertEqual(1, self.send("#引雷", "alice", observed_at=deadline - 0.001))
        self.assertEqual(2, self.duel()["next_turn"])

        # A message captured exactly at the half-open deadline is rejected.
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        deadline = datetime.fromisoformat(self.duel()["phase_deadline_at"].replace("Z", "+00:00")).timestamp()
        self.now = deadline
        self.assertEqual(1, self.send("#引雷", "bob", observed_at=deadline))
        self.assertEqual(2, self.duel()["next_turn"])

    def test_wrong_player_message_id_cannot_be_replayed_on_their_later_turn(self) -> None:
        self.open_playing()
        self.assertEqual(1, self.send("#引雷", "bob", event_key="wrong:bob", message_id="555"))
        self.assertEqual(1, self.duel()["next_turn"])
        self.send("#引雷", "alice", message_id="556")
        self.accept_latest_prompt()
        self.router.before_messages(self.session, now=self.now)
        self.assertEqual(2, self.duel()["next_turn"])
        self.assertEqual(0, self.send("#引雷", "bob", event_key="replay:bob", message_id="555"))
        self.assertEqual(2, self.duel()["next_turn"])

    def test_startup_recovery_cancels_unfinished_duels_without_transferring_assets(self) -> None:
        self.create_invitation()
        before = (self.held("alice"), self.held("bob"))
        recovered = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now)
        recovered.start("new-session", now=self.now + 1)
        self.assertEqual("cancelled", self.duel()["state"])
        self.assertEqual(before, (self.held("alice"), self.held("bob")))


if __name__ == "__main__":
    unittest.main()
