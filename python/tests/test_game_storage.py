from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from wechat_receiver.games import DEFAULT_GAME_CONFIG, load_game_config
from wechat_receiver.store import Store


class GameStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.path = self.root / "messages.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def _player(self, player_id: str, name: str):
        self.store.db.execute("""INSERT INTO game_players(account_id,group_id,player_id,dao_name,dao_name_key)
            VALUES('account','group',?,?,?)""", (player_id, name, name.casefold()))

    def test_v2_upgrade_preserves_message_and_outbox_rows(self):
        db = self.store.db
        db.execute("INSERT INTO sources(path,identity,generation) VALUES('log','identity',1)")
        db.execute("""INSERT INTO raw_events(source_id,start_offset,end_offset,raw,parse_status,session_id)
            VALUES(1,0,1,X'00','ok','session')""")
        db.execute("""INSERT INTO messages(raw_event_id,session_id,direction,history_state,model_json)
            VALUES(1,'session','incoming','unknown','{}')""")
        db.execute("""INSERT INTO outbox(request_id,command_json,fingerprint,expected_account_id,observer_session_id,
            target_id,text,created_at,expires_at,origin,protocol_version,status,enqueued_at,updated_at)
            VALUES('request','{}','{}','account','session','group','hello','now','later','test',1,'queued','now','now')""")
        db.execute("DROP TABLE game_actions")
        db.execute("DROP TABLE game_supports")
        db.execute("DROP TABLE game_duels")
        db.execute("DROP TABLE game_items")
        db.execute("DROP TABLE game_players")
        db.execute("PRAGMA user_version=2")
        db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT text FROM outbox WHERE request_id='request'").fetchone()[0], "hello")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM game_players").fetchone()[0], 0)

    def test_game_constraints_enforce_group_scope_and_uniqueness(self):
        self._player("one", "One")
        self._player("two", "Two")
        db = self.store.db
        db.execute("""INSERT INTO game_items(item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
            VALUES('F001','account','group','shanhe_tu','treasure','one','held')""")
        db.execute("""INSERT INTO game_players(account_id,group_id,player_id,dao_name,dao_name_key)
            VALUES('account','other-group','other','Other','other')""")
        db.execute("""INSERT INTO game_items(item_id,account_id,group_id,template_id,rarity,owner_player_id,state)
            VALUES('F099','account','other-group','bichen_zhu','artifact','other','held')""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO game_items(item_id,account_id,group_id,template_id,rarity,state)
                VALUES('F002','account','group','shanhe_tu','treasure','pool')""")
        db.execute("""INSERT INTO game_duels(duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json)
            VALUES('D001','account','group','one','two','inviting',1,'{}')""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""UPDATE game_players SET cultivation=3.5
                WHERE account_id='account' AND group_id='group' AND player_id='one'""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""UPDATE game_duels SET phase_sequence='two' WHERE duel_id='D001'""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""UPDATE game_duels SET loot_item_id='F099' WHERE duel_id='D001'""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO game_duels(duel_id,account_id,group_id,challenger_id,challenged_id,state,rules_version,rules_json)
                VALUES('D002','account','group','two','one','supporting',1,'{}')""")
        db.execute("""INSERT INTO game_supports(duel_id,account_id,group_id,supporter_id,supported_player_id,amount)
            VALUES('D001','account','group','one','one',10)""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO game_supports(duel_id,account_id,group_id,supporter_id,supported_player_id,amount)
                VALUES('D001','account','group','one','two',10)""")

    def test_game_config_rejects_invalid_limits_and_drop_weights(self):
        config = self.root / "game.toml"
        self.assertFalse(DEFAULT_GAME_CONFIG.duel_enabled)
        config.write_text("[game]\nduel_enabled = true\n", encoding="utf-8")
        self.assertTrue(load_game_config(config).duel_enabled)
        config.write_text('[game]\nduel_enabled = "true"\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duel_enabled"):
            load_game_config(config)
        config.write_text("[game]\nsupport_minimum = 101\nsupport_maximum = 100\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "support minimum"):
            load_game_config(config)
        config.write_text("[game.drop_weights.qi]\nartifact = 50\nspirit = 30\nancient = 15\ntreasure = 4\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "drop_weights"):
            load_game_config(config)
        self.assertIs(load_game_config(), DEFAULT_GAME_CONFIG)
        self.assertEqual(
            load_game_config(Path(__file__).resolve().parents[1] / "xiuxian.example.toml"),
            DEFAULT_GAME_CONFIG,
        )

    def test_failed_v2_migration_rolls_back_and_retry_is_idempotent(self):
        db = self.store.db
        db.execute("DROP TABLE game_actions")
        db.execute("DROP TABLE game_supports")
        db.execute("DROP TABLE game_duels")
        db.execute("DROP TABLE game_items")
        db.execute("DROP TABLE game_players")
        db.execute("CREATE TABLE game_duels(duel_id TEXT)")
        db.execute("PRAGMA user_version=2")
        db.commit()
        self.store.close()
        with self.assertRaises(sqlite3.OperationalError):
            Store(self.path)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_players'").fetchone())
        db.execute("DROP TABLE game_duels")
        db.commit()
        db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
