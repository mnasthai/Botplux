from __future__ import annotations

import json
import unittest

from wechat_receiver.normalize import normalize


def incoming(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": "item",
        "source": "receive_batch",
        "session_id": "ignored-record-session",
        "seq": 7,
        "call_id": 3,
        "observed_unix_ms": 1_789_642_395_000,
        "msg_type": 1,
        "from": "room@chatroom",
        "to": "wxid_self",
        "content": "wxid_member:\nhello 😀\nsecond line",
        "msg_source": "<msgsource><atuserlist>wxid_self,wxid_other</atuserlist></msgsource>",
        "from_read": {"status": "ok"},
        "to_read": {"status": "ok"},
        "content_read": {"status": "ok"},
        "source_read": {"status": "ok"},
        "aux_read": {"status": "missing"},
        "raw_fields": {"9": "1789642395", "12": "18446744073709551614"},
    }
    record.update(overrides)
    return record


class NormalizeTests(unittest.TestCase):
    def test_group_message_strips_one_verified_member_prefix_and_parses_real_mention(self) -> None:
        message = normalize(incoming(), session_id="session-a", self_id="wxid_self", history_state="live_candidate")
        assert message is not None
        self.assertEqual("session-a:7", message.event_key)
        self.assertEqual("room@chatroom", message.conversation_id)
        self.assertEqual("wxid_member", message.sender_id)
        self.assertEqual("hello 😀\nsecond line", message.content)
        self.assertEqual("wxid_member:\nhello 😀\nsecond line", message.raw_content)
        self.assertEqual(("wxid_self", "wxid_other"), message.mentioned_ids)
        self.assertEqual("explicit_self", message.mention_state)
        self.assertEqual(1_789_642_395, message.message_time_candidate)
        self.assertEqual("18446744073709551614", message.message_id_candidate)
        self.assertEqual("live_candidate", message.history_state)
        self.assertEqual("unknown", message.delivery_status)
        self.assertEqual(message.to_dict(), json.loads(json.dumps(message.to_dict())))

    def test_nickname_text_and_incomplete_source_do_not_establish_mention(self) -> None:
        message = normalize(
            incoming(content="wxid_member:\n@机器人 hi", msg_source="<msgsource><atuserlist>wxid_self</atuserlist></msgsource>", source_read={"status": "truncated"}),
            session_id="s",
            self_id="wxid_self",
        )
        assert message is not None
        self.assertEqual((), message.mentioned_ids)
        self.assertEqual("unknown", message.mention_state)

    def test_dtd_and_nested_atuserlist_are_rejected(self) -> None:
        dtd = normalize(incoming(msg_source="<!DOCTYPE x [<!ENTITY a 'wxid_self'>]><msgsource><atuserlist>&a;</atuserlist></msgsource>"), session_id="s", self_id="wxid_self")
        nested = normalize(incoming(msg_source="<msgsource><outer><atuserlist>wxid_self</atuserlist></outer></msgsource>"), session_id="s", self_id="wxid_self")
        assert dtd is not None and nested is not None
        self.assertEqual("unknown", dtd.mention_state)
        self.assertIn("unsafe_msg_source_xml", dtd.quality_notes)
        self.assertEqual("unknown", nested.mention_state)

    def test_incomplete_content_is_preserved_raw_but_not_normalized(self) -> None:
        message = normalize(incoming(content="wxid_member:\npartial", content_read={"status": "truncated"}), session_id="s", self_id="wxid_self")
        assert message is not None
        self.assertEqual("wxid_member:\npartial", message.raw_content)
        self.assertIsNone(message.content)
        self.assertIsNone(message.sender_id)
        self.assertIn("content_not_complete", message.quality_notes)

    def test_without_self_id_direction_and_explicit_mention_remain_unknown(self) -> None:
        message = normalize(incoming(), session_id="s")
        assert message is not None
        self.assertEqual("unknown", message.direction)
        self.assertEqual("unknown", message.mention_state)
        self.assertEqual(("wxid_self", "wxid_other"), message.mentioned_ids)
        self.assertEqual("wxid_member", message.sender_id)

    def test_outbound_is_a_request_with_unknown_delivery_and_configured_sender(self) -> None:
        record = incoming(
            kind="outbound_item",
            source="send_request",
            from_read={"status": "not_read"},
            to="room@chatroom",
            content="send this",
            raw_fields={"4": "1789642395", "7": "100"},
        )
        record["from"] = ""
        message = normalize(record, session_id="s", self_id="wxid_self")
        assert message is not None
        self.assertEqual("outbound_request", message.direction)
        self.assertEqual("room@chatroom", message.conversation_id)
        self.assertEqual("wxid_self", message.sender_id)
        self.assertIsNone(message.message_id_candidate)
        self.assertEqual("unknown", message.delivery_status)

    def test_media_and_complete_reply_app_messages_have_specific_kinds(self) -> None:
        image = normalize(incoming(msg_type=3, content="wxid_member:\n<msg><img /></msg>"), session_id="s", self_id="wxid_self")
        emoji = normalize(incoming(msg_type=47, content="wxid_member:\n<msg><emoji /></msg>"), session_id="s", self_id="wxid_self")
        reply = normalize(
            incoming(msg_type=49, content="wxid_member:\n<msg><appmsg><type>57</type><refermsg><content>not extracted</content></refermsg></appmsg></msg>"),
            session_id="s",
            self_id="wxid_self",
        )
        assert image is not None and emoji is not None and reply is not None
        self.assertEqual(("image", None), (image.message_kind, image.app_message_type))
        self.assertEqual(("emoji", None), (emoji.message_kind, emoji.app_message_type))
        self.assertEqual(("app", 57), (reply.message_kind, reply.app_message_type))
        self.assertIn("app_message_type_reply", reply.quality_notes)

    def test_malformed_or_unsafe_app_xml_is_not_classified_as_app(self) -> None:
        malformed = normalize(incoming(msg_type=49, content="wxid_member:\n<msg><appmsg><type>57</type></msg>"), session_id="s", self_id="wxid_self")
        unsafe = normalize(
            incoming(msg_type=49, content="wxid_member:\n<!DOCTYPE msg [<!ENTITY x '57'>]><msg><appmsg><type>&x;</type></appmsg></msg>"),
            session_id="s",
            self_id="wxid_self",
        )
        assert malformed is not None and unsafe is not None
        self.assertEqual(("unknown", None), (malformed.message_kind, malformed.app_message_type))
        self.assertIn("invalid_app_message_xml", malformed.quality_notes)
        self.assertEqual(("unknown", None), (unsafe.message_kind, unsafe.app_message_type))
        self.assertIn("unsafe_app_message_xml", unsafe.quality_notes)

    def test_non_message_and_malformed_numeric_fields_do_not_raise_or_coerce(self) -> None:
        self.assertIsNone(normalize({"kind": "batch"}, session_id="s"))
        message = normalize(incoming(seq="7", call_id=True, observed_unix_ms=-1, msg_type="1", raw_fields={"9": "9" * 100_000, "12": 99}), session_id="s")
        assert message is not None
        self.assertIsNone(message.seq)
        self.assertIsNone(message.event_key)
        self.assertIsNone(message.call_id)
        self.assertIsNone(message.observed_at_ms)
        self.assertIsNone(message.message_type)
        self.assertIsNone(message.message_time_candidate)
        self.assertEqual("99", message.message_id_candidate)

    def test_missing_group_sender_does_not_become_incoming(self) -> None:
        message = normalize(incoming(content="message without sender prefix"), session_id="s", self_id="wxid_self")
        assert message is not None
        self.assertIsNone(message.sender_id)
        self.assertEqual("unknown", message.direction)


if __name__ == "__main__":
    unittest.main()
