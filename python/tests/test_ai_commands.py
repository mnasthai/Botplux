"""Transactional command tests for the local AI reply plugin.

These tests never run an AI provider: they exercise the real plugin loader and
reply router up to the durable ``ai_jobs`` and outbox writes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_receiver.ai.config import AIConfig
from wechat_receiver.games.schema import initialize_game_schema
from wechat_receiver.models import Message
from wechat_receiver.plugins import load_plugins
from wechat_receiver.reply_router import ReplyRouter
from wechat_receiver.store import Store


class AICommandTests(unittest.TestCase):
    account = "wxid_bot"
    group = "ai-test@chatroom"
    admin = "wxid_admin"
    member = "wxid_member"
    base = datetime(2026, 9, 18, 16, 0, tzinfo=timezone.utc).timestamp()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "messages.sqlite3")
        with self.store.db:
            initialize_game_schema(self.store.db)
        self.now = self.base
        self.sequence = 0
        self.ai_config = AIConfig(
            key_file=Path(self.temporary.name) / "not-read.key",
            cooldown_seconds=60,
            user_daily_limit=2,
            group_daily_limit=10,
            deepseek_daily_limit=10,
            codex_daily_limit=10,
        )
        self.config = SimpleNamespace(
            sender=SimpleNamespace(
                account_id=self.account,
                allowed_targets=frozenset({self.group, self.admin}),
            ),
            enabled_plugins=("ai",),
            reply_ttl_seconds=60,
            max_message_age_seconds=120,
            admin_ids=(self.admin,),
            ai_config=self.ai_config,
        )
        self.plugin = load_plugins(Path(__file__).resolve().parents[2] / "plugins", ("ai",))[0]
        self.router = ReplyRouter(self.store, self.config, [self.plugin], started_at=self.now - 1)
        with self.store.db:
            self.seed_player(self.member, stones=200)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def seed_player(self, player: str, *, stones: int) -> None:
        self.store.db.execute(
            """INSERT INTO game_players(account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones)
               VALUES(?,?,?,?,?,?)""",
            (self.account, self.group, player, "青玄", player, stones),
        )

    def message(self, text: str, *, sender: str | None = None, group: str | None = None,
                event_key: str | None = None, message_id: str | None = None) -> Message:
        self.sequence += 1
        number = self.sequence
        sender = self.member if sender is None else sender
        group = self.group if group is None else group
        return Message(
            session_id="test", event_key=event_key or f"test:{number}", seq=number, call_id=number,
            source="receive_batch", event_kind="item", observed_at_ms=int(self.now * 1000),
            message_type=1, message_kind="text", app_message_type=None, content=text, raw_content=text,
            conversation_id=group, sender_id=sender, direction="incoming",
            message_time_candidate=int(self.now), message_id_candidate=message_id or str(10_000 + number),
            mentioned_ids=(), mention_state="none", history_state="live_candidate",
        )

    def send(self, text: str, **kwargs) -> int:
        return self.router.handle(self.message(text, **kwargs), "test", now=self.now)

    def jobs(self):
        return self.store.db.execute("SELECT * FROM ai_jobs ORDER BY created_at,job_id").fetchall()

    def stones(self, player: str = member) -> int:
        return self.store.db.execute(
            "SELECT spirit_stones FROM game_players WHERE account_id=? AND group_id=? AND player_id=?",
            (self.account, self.group, player),
        ).fetchone()[0]

    def finish_all_jobs(self) -> None:
        with self.store.db:
            self.store.db.execute("UPDATE ai_jobs SET state='completed'")

    def test_wendao_charges_deepseek_and_tianji_at_their_fixed_rates(self) -> None:
        self.assertEqual(1, self.send("#问道 今日运势？"))
        first = self.jobs()[0]
        self.assertEqual(("deepseek", 20, "今日运势？"),
                         (first["provider"], first["cost"], first["question"]))
        self.assertEqual(180, self.stones())

        self.finish_all_jobs()
        self.now += self.ai_config.cooldown_seconds
        self.assertEqual(1, self.send("#问道 天机 今日群聊摘要"))
        second = self.jobs()[1]
        self.assertEqual(("codex", 40, "今日群聊摘要"),
                         (second["provider"], second["cost"], second["question"]))
        self.assertEqual(140, self.stones())
        self.assertEqual(2, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_admin_ai_is_free_for_private_or_group_but_denies_non_admin(self) -> None:
        self.assertEqual(1, self.send("#AI 私聊问题", sender=self.admin, group=self.admin))
        first = self.jobs()[0]
        self.assertEqual((1, 0, "deepseek"), (first["is_admin"], first["cost"], first["provider"]))

        self.finish_all_jobs()
        self.now += self.ai_config.cooldown_seconds
        self.assertEqual(1, self.send("#AI 天机 群聊问题", sender=self.admin, group=self.group))
        second = self.jobs()[1]
        self.assertEqual((1, 0, "codex"), (second["is_admin"], second["cost"], second["provider"]))

        self.now += self.ai_config.cooldown_seconds
        self.assertEqual(1, self.send("#AI 不应受理", sender=self.member, group=self.group))
        self.assertIn("仅限管理员", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])
        self.assertEqual(2, len(self.jobs()))

    def test_non_command_creates_no_job_or_outbox_reply(self) -> None:
        self.assertEqual(0, self.send("今天天气不错"))
        self.assertEqual(0, len(self.jobs()))
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])

    def test_status_and_acceptance_expose_only_public_summary(self) -> None:
        self.assertEqual(1, self.send("#问道 今天怎么样"))
        accepted = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertEqual("已收到，已扣除 20 灵石；若本次未完成会自动退还灵石。", accepted)

        self.assertEqual(1, self.send("#AI状态", sender=self.admin, group=self.admin))
        status = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("状态：", status)
        self.assertIn("待回答：1 项", status)
        self.assertIn("问道额度：", status)
        self.assertIn("天机额度：", status)
        self.assertIn("记忆：0 条", status)
        self.assertIn("当前天机档位：luna", status)
        self.assertIn("中思考深度", status)
        for private in ("DeepSeek", "Codex", "gpt-", "activity", "wxid", "@chatroom", "脚本", "登录"):
            self.assertNotIn(private, status)

    def test_tianji_model_administrator_commands_are_durable_and_free(self) -> None:
        self.assertEqual(1, self.send("#天机模型", sender=self.admin, group=self.group))
        self.assertIn("当前天机档位：luna", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_model_settings").fetchone()[0])

        self.assertEqual(1, self.send("#天机模型 sol", sender=self.admin, group=self.admin))
        setting = self.store.db.execute(
            "SELECT profile,updated_by FROM ai_model_settings WHERE account_id=?", (self.account,)
        ).fetchone()
        self.assertEqual(("sol", self.admin), tuple(setting))
        reply = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("sol（高思考深度）", reply)
        self.assertIn("后续开始的天机问答和群记忆统一使用新档位", reply)
        self.assertIn("当前任务继续完成", reply)
        self.assertEqual(0, len(self.jobs()))
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_calls").fetchone()[0])
        self.assertEqual(200, self.stones())

    def test_tianji_model_rejects_nonadmins_and_invalid_parameters(self) -> None:
        self.assertEqual(1, self.send("#天机模型 luna", sender=self.member, group=self.group))
        self.assertIn("仅限管理员", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_model_settings").fetchone()[0])

        self.assertEqual(1, self.send("#天机模型 gpt-unknown", sender=self.admin, group=self.admin))
        self.assertIn("只支持 sol 或 luna", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_model_settings").fetchone()[0])

    def test_tianji_model_replay_cannot_restore_an_older_profile_or_cross_accounts(self) -> None:
        self.assertEqual(1, self.send("#天机模型 luna", sender=self.admin, group=self.group,
                                      event_key="model:first", message_id="model-first"))
        self.assertEqual(1, self.send("#天机模型 sol", sender=self.admin, group=self.group,
                                      event_key="model:second", message_id="model-second"))
        self.assertEqual(0, self.send("#天机模型 luna", sender=self.admin, group=self.group,
                                      event_key="model:replay", message_id="model-first"))
        self.assertEqual("sol", self.store.db.execute(
            "SELECT profile FROM ai_model_settings WHERE account_id=?", (self.account,)
        ).fetchone()[0])

        other_account = "wxid_other_bot"
        self.config.sender.account_id = other_account
        self.assertEqual(1, self.send("#天机模型 luna", sender=self.admin, group=self.admin,
                                      event_key="other:model", message_id="other-model"))
        self.assertEqual("luna", self.store.db.execute(
            "SELECT profile FROM ai_model_settings WHERE account_id=?", (other_account,)
        ).fetchone()[0])
        self.assertEqual("sol", self.store.db.execute(
            "SELECT profile FROM ai_model_settings WHERE account_id=?", (self.account,)
        ).fetchone()[0])

    def test_tianji_model_switch_rolls_back_with_its_reply(self) -> None:
        message = self.message("#天机模型 luna", sender=self.admin, group=self.admin,
                               event_key="model:rollback", message_id="model-rollback")
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.router.handle(message, "test", now=self.now)
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_model_settings").fetchone()[0])
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM ai_model_switches").fetchone()[0])

    def test_real_administrator_bypasses_daily_limits_and_cooldown_without_opening_tools(self) -> None:
        self.ai_config = replace(self.ai_config, cooldown_seconds=60, user_daily_limit=1,
                                 group_daily_limit=1, deepseek_daily_limit=1, codex_daily_limit=1)
        self.config.ai_config = self.ai_config
        with self.store.db:
            self.store.db.execute(
                """INSERT INTO game_players(account_id,group_id,player_id,dao_name,dao_name_key,spirit_stones)
                   VALUES(?,?,?,?,?,?)""",
                (self.account, self.group, self.admin, "玄月", "admin-dao", 100),
        )

        self.assertEqual(1, self.send("#问道 管理员第一问", sender=self.admin))
        first = self.store.db.execute("SELECT * FROM ai_jobs WHERE question='管理员第一问'").fetchone()
        self.assertEqual((0, 1, 20), (first["is_admin"], first["daily_limit_exempt"], first["cost"]))
        self.finish_all_jobs()
        self.assertEqual(1, self.send("#问道 管理员第二问", sender=self.admin))
        self.finish_all_jobs()
        self.assertEqual(1, self.send("#AI 管理员第三问", sender=self.admin, group=self.admin))
        third = self.store.db.execute("SELECT * FROM ai_jobs WHERE question='管理员第三问'").fetchone()
        self.assertEqual((1, 1, 0), (third["is_admin"], third["daily_limit_exempt"], third["cost"]))
        self.assertEqual(60, self.stones(self.admin))

        self.finish_all_jobs()
        self.assertEqual(1, self.send("#问道 普通第一问"))
        self.finish_all_jobs()
        self.assertEqual(1, self.send("#问道 普通太快"))
        self.assertIn("频繁", self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        ordinary = self.store.db.execute(
            "SELECT count(*) FROM ai_jobs WHERE daily_limit_exempt=0 AND provider='deepseek'"
        ).fetchone()[0]
        self.assertEqual(1, ordinary)

    def test_pending_cooldown_and_daily_limit_do_not_charge_extra(self) -> None:
        self.assertEqual(1, self.send("#问道 第一问"))
        self.assertEqual(180, self.stones())
        self.assertEqual(1, self.send("#问道 第二问"))
        self.assertIn("正在处理", self.store.db.execute(
            "SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])
        self.assertEqual(1, len(self.jobs()))
        self.assertEqual(180, self.stones())

        self.finish_all_jobs()
        self.now += 30
        self.assertEqual(1, self.send("#问道 太快了"))
        self.assertIn("频繁", self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(1, len(self.jobs()))
        self.assertEqual(180, self.stones())

        self.now += 30
        self.assertEqual(1, self.send("#问道 第二次有效"))
        self.finish_all_jobs()
        self.now += self.ai_config.cooldown_seconds
        self.assertEqual(1, self.send("#问道 第三次超限"))
        self.assertIn("次数已用完", self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(2, len(self.jobs()))
        self.assertEqual(160, self.stones())

    def test_replayed_message_does_not_charge_or_create_a_second_job(self) -> None:
        original = self.message("#问道 重复消息", event_key="test:dedupe", message_id="dedupe-id")
        self.assertEqual(1, self.router.handle(original, "test", now=self.now))
        self.assertEqual(0, self.router.handle(original, "test", now=self.now))
        replay = self.message("#问道 重复消息", event_key="test:replay", message_id="dedupe-id")
        self.assertEqual(0, self.router.handle(replay, "test", now=self.now))
        self.assertEqual(1, len(self.jobs()))
        self.assertEqual(180, self.stones())

    def test_outbox_failure_rolls_back_charge_and_job(self) -> None:
        message = self.message("#问道 事务回滚", event_key="test:rollback", message_id="rollback-id")
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue down")):
            with self.assertRaisesRegex(RuntimeError, "queue down"):
                self.router.handle(message, "test", now=self.now)
        self.assertEqual(200, self.stones())
        self.assertEqual(0, len(self.jobs()))
        self.assertEqual(0, self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0])
        row = self.store.db.execute(
            "SELECT status FROM plugin_runs WHERE account_id=? AND event_key=? AND plugin='ai'",
            (self.account, message.event_key),
        ).fetchone()
        self.assertIsNone(row)

    def test_feature_switch_controls_wendao_tianji_and_global_ai(self) -> None:
        self.config.ai_config = replace(self.ai_config, user_daily_limit=10)

        # 1. 默认状态：#AI开关 展示均为开启
        self.assertEqual(1, self.send("#AI开关", sender=self.admin, group=self.admin))
        status = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("AI总状态：全部开启", status)
        self.assertIn("问道（DeepSeek）：开启", status)
        self.assertIn("天机（Codex & 记忆总结）：开启", status)

        # 2. 管理员关闭问道
        self.assertEqual(1, self.send("#关闭问道", sender=self.admin, group=self.admin))
        reply = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("问道功能已关闭", reply)
        self.assertIn("问道(关)", reply)
        self.assertIn("天机(开)", reply)

        # 验证玩家问道被拦截且不扣灵石、不入队
        self.assertEqual(1, self.send("#问道 测问道拦截"))
        blocked_wendao = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("问道功能当前已关闭", blocked_wendao)
        self.assertIn("道友可使用【#问道 天机 问题】", blocked_wendao)
        self.assertEqual(200, self.stones())
        self.assertEqual(0, len(self.jobs()))

        # 验证天机此时仍然可用（扣 40 灵石）
        self.assertEqual(1, self.send("#问道 天机 测天机仍可用"))
        self.finish_all_jobs()
        self.assertEqual(160, self.stones())
        self.assertEqual(1, len(self.jobs()))

        # 3. 管理员关闭天机
        self.now += self.ai_config.cooldown_seconds
        self.assertEqual(1, self.send("#关闭天机", sender=self.admin, group=self.admin))
        self.assertEqual(1, self.send("#问道 天机 测天机拦截"))
        blocked_tianji = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("天机功能当前已关闭", blocked_tianji)
        self.assertEqual(160, self.stones())
        self.assertEqual(1, len(self.jobs()))

        # 验证此时 #整理记忆 也会被联动拦截并提示
        self.assertEqual(1, self.send("#整理记忆", sender=self.admin, group=self.admin))
        blocked_mem = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("天机功能当前已关闭，群记忆整理已暂停", blocked_mem)

        # 4. 管理员开启问道（天机保持关闭，问道立即恢复可用）
        self.assertEqual(1, self.send("#开启问道", sender=self.admin, group=self.admin))
        reply_open = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("问道功能已开启", reply_open)
        self.assertIn("问道(开)", reply_open)
        self.assertIn("天机(关)", reply_open)

        # 关键验证：即使天机仍关闭，问道完全不受影响，正常受理并扣 20 灵石
        self.assertEqual(1, self.send("#问道 测问道恢复"))
        self.finish_all_jobs()
        self.assertEqual(140, self.stones())
        self.assertEqual(2, len(self.jobs()))

        # 5. 管理员关闭 AI 总开关（一键关闭所有）
        self.now += self.ai_config.cooldown_seconds
        self.assertEqual(1, self.send("#关闭AI", sender=self.admin, group=self.admin))
        reply_close_all = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("AI功能已关闭", reply_close_all)
        self.assertIn("问道(关)", reply_close_all)
        self.assertIn("天机(关)", reply_close_all)

        self.assertEqual(1, self.send("#问道 测总开关拦截"))
        blocked_all = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("问道功能当前已关闭", blocked_all)
        self.assertEqual(140, self.stones())
        self.assertEqual(2, len(self.jobs()))

        # 管理员自己提问也受限制
        self.assertEqual(1, self.send("#AI 管理员提问", sender=self.admin, group=self.admin))
        blocked_admin = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("问道功能当前已关闭", blocked_admin)
        self.assertEqual(2, len(self.jobs()))

        # 6. 管理员重新开启 AI 总开关（一键开启所有）
        self.assertEqual(1, self.send("#开启AI", sender=self.admin, group=self.admin))
        reply_open_all = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("AI功能已开启", reply_open_all)
        self.assertIn("问道(开)", reply_open_all)
        self.assertIn("天机(开)", reply_open_all)

        self.assertEqual(1, self.send("#问道 测总开关恢复"))
        self.assertEqual(120, self.stones())
        self.assertEqual(3, len(self.jobs()))

    def test_feature_switch_denies_non_admin_and_handles_replay_and_rollback(self) -> None:
        # 非管理员尝试使用开关指令
        self.assertEqual(1, self.send("#关闭AI", sender=self.member, group=self.group))
        denied = self.store.db.execute("SELECT text FROM outbox ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("仅限管理员", denied)

        # 参数化指令
        self.assertEqual(1, self.send("#AI开关 问道 关闭", sender=self.admin, group=self.admin))
        row = self.store.db.execute("SELECT wendao_enabled FROM ai_feature_settings WHERE account_id=?", (self.account,)).fetchone()
        self.assertEqual(0, row[0])

        self.assertEqual(1, self.send("#AI开关 问道 开启", sender=self.admin, group=self.admin))
        row = self.store.db.execute("SELECT wendao_enabled FROM ai_feature_settings WHERE account_id=?", (self.account,)).fetchone()
        self.assertEqual(1, row[0])

        # 重放保护：相同 message_id 不会重复执行
        msg = self.message("#关闭问道", sender=self.admin, group=self.admin, event_key="sw:1", message_id="sw-msg-1")
        self.assertEqual(1, self.router.handle(msg, "test", now=self.now))
        # 重放
        msg_replay = self.message("#关闭问道", sender=self.admin, group=self.admin, event_key="sw:2", message_id="sw-msg-1")
        self.assertEqual(0, self.router.handle(msg_replay, "test", now=self.now))

        # 事务回滚
        rollback_msg = self.message("#关闭AI", sender=self.admin, group=self.admin, event_key="sw:rb", message_id="sw-msg-rb")
        with patch.object(self.router.outbox, "enqueue_in_transaction", side_effect=RuntimeError("queue error")):
            with self.assertRaisesRegex(RuntimeError, "queue error"):
                self.router.handle(rollback_msg, "test", now=self.now)
        # 确认未写入 settings 表中已关闭的状态（保持原有的 ai_enabled=1）
        feat = self.store.db.execute("SELECT ai_enabled FROM ai_feature_settings WHERE account_id=?", (self.account,)).fetchone()
        self.assertEqual(1, feat[0])

