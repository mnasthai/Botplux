from __future__ import annotations

import sqlite3
import unittest

from wechat_receiver.member_profiles import GroupMemberDirectory, GroupMemberProfile


class GroupMemberDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.directory = GroupMemberDirectory(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def profile(
        self, *, account="account-a", group="one@chatroom", member="wxid-alice", nickname="Alice", observed=100
    ) -> GroupMemberProfile:
        return GroupMemberProfile(account, group, member, nickname, observed)

    def test_profiles_are_isolated_by_account_and_group(self) -> None:
        self.directory.save(self.profile(account="account-a", group="one@chatroom", nickname="Alice"))
        self.directory.save(self.profile(account="account-a", group="two@chatroom", nickname="Alicia"))
        self.directory.save(self.profile(account="account-b", group="one@chatroom", nickname="Ally"))

        self.assertEqual(
            self.directory.lookup("account-a", "one@chatroom", "wxid-alice", now_ms=100, max_age_ms=0)
            .profile.group_nickname,
            "Alice",
        )
        self.assertEqual(
            self.directory.lookup("account-a", "two@chatroom", "wxid-alice", now_ms=100, max_age_ms=0)
            .profile.group_nickname,
            "Alicia",
        )
        self.assertEqual(
            self.directory.lookup("account-b", "one@chatroom", "wxid-alice", now_ms=100, max_age_ms=0)
            .profile.group_nickname,
            "Ally",
        )

    def test_empty_group_nickname_is_known_but_no_row_is_unknown(self) -> None:
        self.directory.save(self.profile(nickname=""))

        known_empty = self.directory.lookup("account-a", "one@chatroom", "wxid-alice", now_ms=100, max_age_ms=0)
        unknown = self.directory.lookup("account-a", "one@chatroom", "wxid-unknown", now_ms=100, max_age_ms=0)
        self.assertIsNotNone(known_empty)
        self.assertEqual(known_empty.profile.group_nickname, "")
        self.assertIsNone(unknown)

    def test_lookup_returns_stale_cached_profile(self) -> None:
        self.directory.save(self.profile(observed=100))
        fresh = self.directory.lookup("account-a", "one@chatroom", "wxid-alice", now_ms=150, max_age_ms=50)
        stale = self.directory.lookup("account-a", "one@chatroom", "wxid-alice", now_ms=151, max_age_ms=50)

        self.assertFalse(fresh.stale)
        self.assertTrue(stale.stale)
        self.assertEqual(stale.profile.group_nickname, "Alice")

    def test_newer_nickname_replaces_old_but_time_never_moves_back(self) -> None:
        self.assertTrue(self.directory.save(self.profile(nickname="Before", observed=100)))
        self.assertTrue(self.directory.save(self.profile(nickname="After", observed=200)))
        self.assertFalse(self.directory.save(self.profile(nickname="Old again", observed=199)))

        result = self.directory.lookup("account-a", "one@chatroom", "wxid-alice", now_ms=200, max_age_ms=0)
        self.assertEqual(result.profile.group_nickname, "After")
        self.assertEqual(result.profile.observed_at_ms, 200)

    def test_rejects_non_chatroom_ids_and_unverified_or_invalid_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "group_id"):
            GroupMemberProfile("account", "wxid-person", "wxid-member", "Alias", 1)
        with self.assertRaisesRegex(ValueError, "member_id"):
            GroupMemberProfile("account", "one@chatroom", "", "Alias", 1)
        with self.assertRaisesRegex(ValueError, "group_nickname"):
            GroupMemberProfile("account", "one@chatroom", "wxid", None, 1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "source"):
            GroupMemberProfile("account", "one@chatroom", "wxid", "Alias", 1, "native_contact")
        with self.assertRaisesRegex(ValueError, "observed_at_ms"):
            GroupMemberProfile("account", "one@chatroom", "wxid", "Alias", -1)

    def test_save_does_not_commit_caller_transaction_when_table_already_exists(self) -> None:
        self.directory.save(self.profile(member="wxid-existing"))

        self.db.execute("BEGIN")
        self.directory.save(self.profile(member="wxid-rolled-back"))

        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(
            self.directory.lookup("account-a", "one@chatroom", "wxid-rolled-back", now_ms=100, max_age_ms=0)
        )
        self.assertIsNotNone(
            self.directory.lookup("account-a", "one@chatroom", "wxid-existing", now_ms=100, max_age_ms=0)
        )


if __name__ == "__main__":
    unittest.main()
